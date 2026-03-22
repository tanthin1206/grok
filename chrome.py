from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

try:
    from settings_manager import BASE_DIR as RUNTIME_BASE_DIR, CHROME_USER_DATA_ROOT as RUNTIME_CHROME_USER_DATA_ROOT
except Exception:
    RUNTIME_BASE_DIR = None
    RUNTIME_CHROME_USER_DATA_ROOT = None

WORKSPACE_DIR = Path(RUNTIME_BASE_DIR) if RUNTIME_BASE_DIR is not None else Path(__file__).resolve().parent

CDP_HOST = os.getenv("CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))
PROFILE_NAME = os.getenv("PROFILE_NAME", "PROFILE_1")
FLOW_URL = "https://labs.google/fx/vi/tools/flow"

CHROME_EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-default-apps",
    "--remote-allow-origins=*",
    "--window-size=1200,800",
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


def get_chrome_executable_path() -> str:
    custom_path = os.getenv("CHROME_EXE_PATH")
    if custom_path and Path(custom_path).exists():
        return custom_path

    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError("Không tìm thấy chrome.exe. Set CHROME_EXE_PATH nếu cần.")


def can_bind_port(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


def is_cdp_ready(cdp_url: str) -> bool:
    try:
        with urlopen(f"{cdp_url}/json/version", timeout=1.5) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def wait_for_cdp(cdp_url: str, timeout_seconds: int = 30) -> bool:
    deadline = time.time() + int(timeout_seconds)
    while time.time() < deadline:
        if is_cdp_ready(cdp_url):
            return True
        time.sleep(0.5)
    return False


def pick_cdp_port_for_new_session(host: str, start_port: int, max_tries: int = 40) -> int:
    base = int(start_port or 9223)
    for p in range(base, base + int(max_tries)):
        cdp_url = f"http://{host}:{p}"
        if is_cdp_ready(cdp_url):
            continue
        if can_bind_port(host, p):
            return p
    raise RuntimeError(f"Không tìm được port CDP trống từ {base} (tries={max_tries}).")


def _kill_chrome_using_user_data_dir(user_data_dir: Path) -> None:
    try:
        if os.name != "nt":
            return
        target = str(Path(user_data_dir).resolve())
        ps = "\n".join([
            "$target = '" + target.replace("'", "''") + "'",
            "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object {",
            "  $_.CommandLine -and ($_.CommandLine -like '*--user-data-dir*') -and ($_.CommandLine -like ('*' + $target + '*'))",
            "}",
            "foreach ($p in $procs) {",
            "  try { Stop-Process -Id $p.ProcessId -ErrorAction SilentlyContinue } catch {}",
            "}",
            "Start-Sleep -Milliseconds 900",
            "foreach ($p in $procs) {",
            "  try {",
            "    $alive = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue",
            "    if ($alive) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }",
            "  } catch {}",
            "}",
        ])
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_win_hidden_kwargs(),
        )
    except Exception:
        pass


def ensure_profile_dir(user_data_dir: Path) -> Path:
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return user_data_dir


def start_chrome_debug(
    chrome_exe: str,
    host: str,
    port: int,
    user_data_dir: Path,
    url: str = FLOW_URL,
    offscreen: bool = False,
    language: str | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    ensure_profile_dir(user_data_dir)

    cmd: list[str] = [
        chrome_exe,
        f"--remote-debugging-port={int(port)}",
        f"--remote-debugging-address={host}",
        f"--user-data-dir={str(user_data_dir)}",
    ]
    if offscreen:
        cmd.append("--window-position=-32000,-32000")
        cmd.append("--start-minimized")
    merged_args: list[str] = list(CHROME_EXTRA_ARGS)
    lang = str(language or "").strip()
    if lang:
        merged_args.append(f"--lang={lang}")
        merged_args.append(f"--accept-lang={lang},en")
    if isinstance(extra_args, list) and extra_args:
        merged_args.extend([str(arg) for arg in extra_args if str(arg or "").strip()])
    cmd.extend(merged_args)
    cmd.append(str(url or FLOW_URL))
    # IMPORTANT: launch Chrome visibly for profile open / auto-login UX.
    # Do not use CREATE_NO_WINDOW or SW_HIDE here, otherwise Chrome may not
    # appear on desktop/taskbar on Windows.
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def resolve_profile_dir(profile_name: str | None = None) -> Path:
    default_root = Path(RUNTIME_CHROME_USER_DATA_ROOT) if RUNTIME_CHROME_USER_DATA_ROOT is not None else (WORKSPACE_DIR / "chrome_user_data")
    chrome_root = Path(os.getenv("CHROME_USER_DATA_ROOT", str(default_root)))
    chosen_profile = str(profile_name or PROFILE_NAME).strip() or "PROFILE_1"
    if profile_name is not None:
        return chrome_root / chosen_profile
    return Path(os.getenv("CHROME_USER_DATA_DIR", str(chrome_root / chosen_profile)))


def _resolve_profile_dir(profile_name: str | None = None) -> Path:
    return resolve_profile_dir(profile_name)


def reset_chrome_user_data(profile_name: str | None = None) -> Path:
    profile_dir = _resolve_profile_dir(profile_name)
    try:
        _kill_chrome_using_user_data_dir(profile_dir)
        time.sleep(0.8)
    except Exception:
        pass

    try:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:
        pass

    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def open_profile_chrome(profile_name: str | None = None, url: str = FLOW_URL, language: str | None = None) -> dict:
    profile_dir = _resolve_profile_dir(profile_name)
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = pick_cdp_port_for_new_session(CDP_HOST, CDP_PORT)
    chrome_exe = get_chrome_executable_path()
    start_chrome_debug(
        chrome_exe=chrome_exe,
        host=CDP_HOST,
        port=port,
        user_data_dir=profile_dir,
        url=url,
        offscreen=False,
        language=language,
    )
    return {
        "success": True,
        "profile_dir": str(profile_dir),
        "port": int(port),
    }


def kill_profile_chrome(profile_path: str | Path) -> None:
    _kill_chrome_using_user_data_dir(Path(profile_path))
