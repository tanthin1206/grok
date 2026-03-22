from __future__ import annotations

import os
import socket
import subprocess
import time
import asyncio
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.async_api import Browser, BrowserContext, Error, async_playwright

from settings_manager import BASE_DIR


GROK_URL = "https://grok.com/"
GROK_CHROME_LANGUAGE = os.getenv("GROK_CHROME_LANGUAGE", "vi")
CDP_HOST = os.getenv("GROK_CDP_HOST", os.getenv("CDP_HOST", "127.0.0.1"))
CDP_PORT = int(os.getenv("GROK_CDP_PORT", os.getenv("CDP_PORT", "9223")))
PROFILE_NAME = os.getenv("GROK_PROFILE_NAME", os.getenv("PROFILE_NAME", "PROFILE_1"))
GROK_USER_DATA_ROOT = Path(
    os.getenv("GROK_CHROME_USER_DATA_ROOT", str(Path(BASE_DIR) / "chrome_user_data_grok"))
)

GROK_WORKFLOW_CHROME_EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-popup-blocking",
    "--mute-audio",
    "--window-size=800,600",
    "--blink-settings=imagesEnabled=false",
    "--disable-features=Translate,BackForwardCache",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def _win_hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"startupinfo": si, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    except Exception:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _find_chrome_exe() -> str:
    custom = os.getenv("CHROME_EXE_PATH")
    if custom and Path(custom).exists():
        return custom
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    raise FileNotFoundError("Không tìm thấy chrome.exe. Hãy set CHROME_EXE_PATH.")


def resolve_profile_dir(profile_name: str | None = None) -> Path:
    name = str(profile_name or PROFILE_NAME).strip() or "PROFILE_1"
    root = GROK_USER_DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def _is_cdp_ready(host: str, port: int) -> bool:
    try:
        with urlopen(f"http://{host}:{int(port)}/json/version", timeout=1.5) as response:
            return int(response.status or 0) == 200
    except (URLError, OSError):
        return False
    except Exception:
        return False


def _can_bind(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, int(port)))
        return True
    except Exception:
        return False


def _pick_port(host: str, start_port: int, tries: int = 50) -> int:
    base = int(start_port or CDP_PORT)
    for p in range(base, base + int(tries or 1)):
        if _is_cdp_ready(host, p):
            continue
        if _can_bind(host, p):
            return p
    raise RuntimeError(f"Không tìm được port CDP trống từ {base}")


def _find_running_cdp_port_for_user_data(user_data_dir: Path) -> int | None:
    if os.name != "nt":
        return None
    target = str(Path(user_data_dir).resolve()).lower().replace("/", "\\")
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True,
            text=True,
            check=False,
            **_win_hidden_kwargs(),
        )
        text = str(proc.stdout or "")
    except Exception:
        return None

    import re

    for line in text.splitlines():
        cmd = str(line or "").strip()
        if not cmd:
            continue
        low = cmd.lower().replace("/", "\\")
        if "--remote-debugging-port=" not in low or "--user-data-dir=" not in low:
            continue
        if target not in low:
            continue
        m = re.search(r"--remote-debugging-port=(\d+)", low)
        if not m:
            continue
        try:
            return int(m.group(1))
        except Exception:
            continue
    return None


def _wait_cdp(host: str, port: int, timeout_seconds: int = 30) -> bool:
    deadline = time.time() + max(1, int(timeout_seconds or 1))
    while time.time() < deadline:
        if _is_cdp_ready(host, int(port)):
            return True
        time.sleep(0.35)
    return False


def _kill_chrome_for_user_data(user_data_dir: Path) -> None:
    if os.name != "nt":
        return
    target = str(Path(user_data_dir).resolve())
    ps = "\n".join(
        [
            "$target = '" + target.replace("'", "''") + "'",
            "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object {",
            "  $_.CommandLine -and ($_.CommandLine -like '*--user-data-dir*') -and ($_.CommandLine -like ('*' + $target + '*'))",
            "}",
            "foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue } catch {} }",
            "Start-Sleep -Milliseconds 800",
            "foreach ($p in $procs) {",
            "  try { if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } } catch {}",
            "}",
        ]
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_win_hidden_kwargs(),
        )
    except Exception:
        pass


def _bring_chrome_window_to_front(user_data_dir: Path) -> None:
    if os.name != "nt":
        return
    target = str(Path(user_data_dir).resolve())
    ps = "\n".join(
        [
            "$target = '" + target.replace("'", "''") + "'",
            "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object {",
            "  $_.CommandLine -and ($_.CommandLine -like '*--user-data-dir*') -and ($_.CommandLine -like ('*' + $target + '*'))",
            "}",
            "if (-not ('WinApi' -as [type])) {",
            "  Add-Type -TypeDefinition @\"",
            "using System;",
            "using System.Runtime.InteropServices;",
            "public static class WinApi {",
            "  [DllImport(\"user32.dll\")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);",
            "  [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr hWnd);",
            "  [DllImport(\"user32.dll\")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);",
            "}",
            "\"@ | Out-Null",
            "}",
            "$handled = $false",
            "foreach ($p in $procs) {",
            "  try {",
            "    $gp = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue",
            "    if ($null -eq $gp) { continue }",
            "    $h = $gp.MainWindowHandle",
            "    if ($h -eq 0) { continue }",
            "    [WinApi]::ShowWindowAsync([IntPtr]$h, 9) | Out-Null",
            "    Start-Sleep -Milliseconds 120",
            "    [WinApi]::SetWindowPos([IntPtr]$h, [IntPtr]::Zero, 40, 40, 1280, 860, 0x0040) | Out-Null",
            "    [WinApi]::SetForegroundWindow([IntPtr]$h) | Out-Null",
            "    $handled = $true",
            "    break",
            "  } catch {}",
            "}",
            "if (-not $handled) {",
            "  try {",
            "    $shell = New-Object -ComObject WScript.Shell",
            "    $shell.AppActivate('Chrome') | Out-Null",
            "  } catch {}",
            "}",
        ]
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_win_hidden_kwargs(),
        )
    except Exception:
        pass


def open_profile_chrome(profile_name: str | None = None, url: str = GROK_URL) -> dict:
    profile_dir = resolve_profile_dir(profile_name)
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = _pick_port(CDP_HOST, CDP_PORT)
    chrome_exe = _find_chrome_exe()

    cmd = [
        chrome_exe,
        f"--remote-debugging-port={int(port)}",
        f"--remote-debugging-address={CDP_HOST}",
        f"--user-data-dir={str(profile_dir)}",
        f"--lang={GROK_CHROME_LANGUAGE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--remote-allow-origins=*",
        "--window-size=1280,860",
        str(url or GROK_URL),
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"success": True, "profile_dir": str(profile_dir), "port": int(port), "host": CDP_HOST}


def kill_profile_chrome(profile_path: str | Path) -> None:
    try:
        _kill_chrome_for_user_data(Path(profile_path))
    except Exception:
        pass


@dataclass
class ChromeSession:
    playwright: any
    browser: Browser
    context: BrowserContext
    user_data_dir: Path

    async def close(self) -> None:
        try:
            await asyncio.wait_for(self.browser.close(), timeout=2.5)
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.playwright.stop(), timeout=2.5)
        except Exception:
            pass
        try:
            kill_profile_chrome(self.user_data_dir)
        except Exception:
            pass


async def open_chrome_session(
    host: str,
    port: int,
    user_data_dir: Path,
    start_url: str = GROK_URL,
    cdp_wait_seconds: int = 30,
    offscreen: bool = True,
) -> ChromeSession:
    user_data_dir = Path(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    use_host = str(host or CDP_HOST).strip() or CDP_HOST
    use_port = int(port or CDP_PORT)
    running_port = _find_running_cdp_port_for_user_data(user_data_dir)
    if isinstance(running_port, int) and running_port > 0:
        use_port = running_port
    cdp_already_ready = _is_cdp_ready(use_host, use_port)
    if (not cdp_already_ready) and (not _can_bind(use_host, use_port)):
        use_port = _pick_port(use_host, use_port)
        cdp_already_ready = _is_cdp_ready(use_host, use_port)

    if not cdp_already_ready:
        chrome_exe = _find_chrome_exe()
        cmd = [
            chrome_exe,
            f"--remote-debugging-port={int(use_port)}",
            f"--remote-debugging-address={use_host}",
            f"--user-data-dir={str(user_data_dir)}",
            f"--lang={GROK_CHROME_LANGUAGE}",
            "--remote-allow-origins=*",
            *GROK_WORKFLOW_CHROME_EXTRA_ARGS,
        ]
        if offscreen:
            cmd.extend([
                "--window-position=-32000,-32000",
                "--start-minimized",
            ])
        cmd.append(str(start_url or GROK_URL))
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not _wait_cdp(use_host, use_port, timeout_seconds=cdp_wait_seconds):
        raise RuntimeError(f"Chrome CDP chưa sẵn sàng tại {use_host}:{use_port}")

    pw = await async_playwright().start()
    browser: Browser | None = None
    last_exc: Exception | None = None
    for _ in range(5):
        try:
            browser = await pw.chromium.connect_over_cdp(f"http://{use_host}:{int(use_port)}")
            break
        except Error as exc:
            last_exc = exc
            await __import__("asyncio").sleep(0.35)
    if browser is None:
        try:
            await pw.stop()
        except Exception:
            pass
        raise RuntimeError(f"Không kết nối được CDP {use_host}:{use_port}: {last_exc}")

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return ChromeSession(playwright=pw, browser=browser, context=context, user_data_dir=user_data_dir)
