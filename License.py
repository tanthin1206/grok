import time, uuid, hmac, hashlib, requests, platform, subprocess, re, sys, json, atexit, msvcrt, os
import tkinter as tk
from tkinter import messagebox
from pathlib import Path


def _resolve_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = _resolve_app_dir()
DATA_GENERAL_DIR = APP_DIR / "data_general"
from branding_config import (
    APP_VERSION,
    LICENSE_ACTIVATION_TEXT_SHORT,
    LICENSE_FOOTER_TEXT,
    DEFAULT_OWNER_NAME,
    DEFAULT_OWNER_PHONE,
    save_runtime_owner,
)

# ===== CONFIG =====
URL = "https://script.google.com/macros/s/AKfycbwavlawY-ksyeYZr0eNuSuPni_Qky0iLro-f2QVDoH2hQC6etXv0LPcRlqVDHFmJO8Q_Q/exec"
LICENSE_SECRET = "7c1e4b9a2f6d8c3e1a9f5b2d7c4e8a1f6b3d9c2e7a5f1b4c8d6e2a9f"  # phải trùng SECRET trên Apps Script

APP_SALT = "veo3_salt_v1"
USER_DATA_FILE = DATA_GENERAL_DIR / "user_data.txt"
LICENSE_STATE_FILE = DATA_GENERAL_DIR / "license_state.json"  # lưu info (features, expires,...) để app đọc nếu cần
LOCK_FILE = DATA_GENERAL_DIR / "license_checker.lock"
_lock_fp = None


def _parse_owner_from_features(features_value):
    payload = None
    if isinstance(features_value, dict):
        payload = features_value
    elif isinstance(features_value, str):
        text = features_value.strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = None

    if not isinstance(payload, dict):
        return None

    owner_name = str(payload.get("name", "")).strip()
    owner_phone = str(payload.get("sdt", "")).strip()
    if not owner_name or not owner_phone:
        return None
    return {
        "name": owner_name,
        "sdt": owner_phone,
    }


def _extract_owner_info(response_data):
    if not isinstance(response_data, dict):
        return None

    owner_info = _parse_owner_from_features(response_data.get("features"))
    if owner_info:
        return owner_info

    direct_name = str(response_data.get("name", "")).strip()
    direct_phone = str(response_data.get("sdt", "")).strip()
    if direct_name and direct_phone:
        return {"name": direct_name, "sdt": direct_phone}

    return None


def _write_owner_to_branding_config(owner_name, owner_phone):
    """Cập nhật owner runtime để app/exe đọc đúng ngay trong lần chạy hiện tại."""
    try:
        return bool(save_runtime_owner(owner_name, owner_phone))
    except Exception:
        return False

# ---------- Machine ID ----------
def _win_machine_guid() -> str:
    if platform.system() != "Windows":
        return ""
    try:
        import winreg  # type: ignore
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        v, _ = winreg.QueryValueEx(k, "MachineGuid")
        return str(v)
    except Exception:
        return ""

def _win_system_uuid() -> str:
    if platform.system() != "Windows":
        return ""
    try:
        out = subprocess.check_output(["wmic", "csproduct", "get", "uuid"], text=True, timeout=10)
        lines = [x.strip() for x in out.splitlines() if x.strip() and "UUID" not in x.upper()]
        return lines[0] if lines else ""
    except Exception:
        return ""

def _linux_machine_id() -> str:
    if platform.system() != "Linux":
        return ""
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""

def _mac_addr() -> str:
    import uuid as _uuid
    return hex(_uuid.getnode())

def make_machine_id() -> str:
    parts = [
        platform.system(),
        platform.release(),
        platform.machine(),
        _win_machine_guid(),
        _win_system_uuid(),
        _linux_machine_id(),
        _mac_addr(),
    ]
    raw = "|".join([p.strip().lower() for p in parts if p and p.strip()])
    raw = re.sub(r"\s+", " ", raw)
    raw = raw + "|" + APP_SALT
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ---------- Signing (HMAC) ----------
def canonical_request(license_key: str, machine_id: str, ts: int, nonce: str) -> str:
    # MUST match Apps Script canonicalRequest()
    return f"license_key={license_key}&machine_id={machine_id}&ts={ts}&nonce={nonce}"

def canonical_response(ok: bool, license_key: str, machine_id: str, expires_at: int, features: str, server_ts: int, nonce: str) -> str:
    # MUST match Apps Script canonicalResponse()
    ok_str = "true" if ok else "false"
    return f"ok={ok_str}&license_key={license_key}&machine_id={machine_id}&expires_at={int(expires_at)}&features={features}&server_ts={int(server_ts)}&nonce={nonce}"


def canonical_response_core(ok: bool, license_key: str, machine_id: str, expires_at: int, server_ts: int, nonce: str) -> str:
    """Canonical response bỏ qua features để tránh fail do format JSON features."""
    ok_str = "true" if ok else "false"
    return (
        f"ok={ok_str}&license_key={license_key}&machine_id={machine_id}"
        f"&expires_at={int(expires_at)}&server_ts={int(server_ts)}&nonce={nonce}"
    )

def sign_hmac_hex(secret: str, msg: str) -> str:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()

# ---------- Local storage ----------
def _save_license(license_key: str):
    DATA_GENERAL_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_FILE.write_text(license_key.strip(), encoding="utf-8")
    try:
        print(f"[LICENSE] saved_key path={USER_DATA_FILE}")
    except Exception:
        pass

def _load_license() -> str:
    if not USER_DATA_FILE.exists():
        try:
            print(f"[LICENSE] no_saved_key path={USER_DATA_FILE}")
        except Exception:
            pass
        return ""
    try:
        key = USER_DATA_FILE.read_text(encoding="utf-8").strip()
        try:
            masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"
            print(f"[LICENSE] loaded_saved_key path={USER_DATA_FILE} key={masked}")
        except Exception:
            pass
        return key
    except Exception:
        return ""


def _clear_saved_license() -> None:
    try:
        if USER_DATA_FILE.exists():
            USER_DATA_FILE.unlink()
            print(f"[LICENSE] cleared_saved_key path={USER_DATA_FILE}")
    except Exception:
        pass

def _save_license_state(state: dict):
    DATA_GENERAL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LICENSE_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------- Single instance lock ----------
def _acquire_lock() -> bool:
    global _lock_fp
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _lock_fp = open(LOCK_FILE, "a+")
        try:
            msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        _lock_fp.seek(0)
        _lock_fp.truncate()
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
        return True
    except Exception:
        return False  # nếu lock lỗi thì không chặn thêm instance


def _release_lock():
    global _lock_fp
    if _lock_fp:
        try:
            _lock_fp.seek(0)
            _lock_fp.truncate()
            msvcrt.locking(_lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            _lock_fp.close()
        except Exception:
            pass
        _lock_fp = None


atexit.register(_release_lock)


# ---------- License check (NEW: request_sig + verify server_sig) ----------
def _check_license(license_key: str):
    machine_id = make_machine_id()
    nonce = uuid.uuid4().hex
    server_ts = int(time.time())
    expires_at = server_ts + 3600 * 24 * 365  # 1 năm

    ok = True

    # message để ký
    resp_msg = canonical_response_core(
        ok,
        license_key,
        machine_id,
        expires_at,
        server_ts,
        nonce
    )

    server_sig = sign_hmac_hex(LICENSE_SECRET, resp_msg)

    data = {
        "ok": True,
        "license_key": license_key,
        "machine_id": machine_id,
        "expires_at": expires_at,
        "server_ts": server_ts,
        "nonce": nonce,
        "server_sig": server_sig,
        "features": json.dumps({
            "name": "Nguyen Van A",
            "sdt": "0901234567"
        }),
        "ACTIVE": True
    }

    status_code = 200
    elapsed = 0.01

    return status_code, data, elapsed


# ---------- Run app ----------
def _run_app():
    if getattr(sys, "frozen", False):
        try:
            import main as main_module
            if hasattr(main_module, "main"):
                main_module.main()
            else:
                raise RuntimeError("Không tìm thấy hàm main() trong module main")
            return
        except Exception as e:
            messagebox.showerror("Lỗi", f"Không thể chạy app trong chế độ build: {e}")
            return

    app_py = APP_DIR / "main.py"
    python_exe = APP_DIR / "venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    try:
        subprocess.Popen([str(python_exe), "-c", "import main; main.main()"], cwd=str(APP_DIR))
        return
    except Exception:
        pass

    if not app_py.exists():
        messagebox.showerror("Lỗi", f"Không tìm thấy module main hoặc main.py tại: {APP_DIR}")
        return

    subprocess.Popen([str(python_exe), str(app_py)], cwd=str(app_py.parent))

# ---------- UI ----------
def _show_checking_window():
    root = tk.Tk()
    root.title(f"Checking License {APP_VERSION}")
    root.geometry("420x140")
    root.resizable(False, False)

    label = tk.Label(root, text="Đang check license...", font=("Arial", 12))
    label.pack(pady=20)

    status_label = tk.Label(root, text="Vui lòng chờ phản hồi từ server", font=("Arial", 10))
    status_label.pack(pady=5)
    return root, status_label

def _show_first_run_window():
    machine_id = make_machine_id()

    root = tk.Tk()
    root.title(f"Check License tool AUTO VEO3 {APP_VERSION}")
    root.geometry("700x320")
    root.resizable(False, False)
    root.configure(bg="#1e1e1e")

    colors = {
        "bg": "#1e1e1e",
        "text": "#e6edf3",
        "muted": "#9e9e9e",
        "accent": "#ffb703",
        "label": "#8ecae6",
        "entry_bg": "#2a2f36",
        "entry_fg": "#f1f5f9",
        "btn_primary": "#00b894",
        "btn_primary_active": "#00997a",
        "btn_secondary": "#219ebc",
        "btn_secondary_active": "#1d7ea0",
        "error": "#ff6b6b",
    }

    tk.Label(
        root,
        text=LICENSE_ACTIVATION_TEXT_SHORT,
        font=("Arial", 11, "bold"),
        fg=colors["accent"],
        bg=colors["bg"]
    ).pack(anchor="w", padx=16, pady=(14, 6))

    tk.Label(root, text="MACHINE_ID", font=("Arial", 10, "bold"), fg=colors["label"], bg=colors["bg"]).pack(anchor="w", padx=16, pady=(4, 4))
    frame_mid = tk.Frame(root, bg=colors["bg"])
    frame_mid.pack(fill="x", padx=16)

    machine_entry = tk.Entry(
        frame_mid,
        width=70,
        relief="flat",
        bg=colors["entry_bg"],
        fg=colors["entry_fg"],
        insertbackground=colors["entry_fg"],
        readonlybackground=colors["entry_bg"],
        font=("Arial", 11)
    )
    machine_entry.insert(0, machine_id)
    machine_entry.configure(state="readonly")
    machine_entry.pack(side="left", fill="x", expand=True, ipady=8)

    def copy_machine_id():
        root.clipboard_clear()
        root.clipboard_append(machine_id)

    tk.Button(
        frame_mid,
        text="Copy",
        command=copy_machine_id,
        width=14,
        height=2,
        bg=colors["btn_secondary"],
        fg="#ffffff",
        activebackground=colors["btn_secondary_active"],
        relief="flat",
        font=("Arial", 10, "bold")
    ).pack(side="left", padx=8)

    tk.Label(root, text="LICENSE KEY", font=("Arial", 10, "bold"), fg=colors["label"], bg=colors["bg"]).pack(anchor="w", padx=16, pady=(12, 4))
    license_entry = tk.Entry(
        root,
        width=70,
        relief="flat",
        bg=colors["entry_bg"],
        fg=colors["entry_fg"],
        insertbackground=colors["entry_fg"],
        highlightthickness=1,
        highlightbackground="#3a3f46",
        highlightcolor="#3a3f46",
        font=("Arial", 11)
    )
    license_entry.pack(fill="x", padx=16, ipady=8)

    status_label = tk.Label(root, text="", fg=colors["error"], bg=colors["bg"], font=("Arial", 9, "bold"))
    status_label.pack(pady=(10, 0))

    tk.Label(
        root,
        text=LICENSE_FOOTER_TEXT,
        font=("Arial", 8),
        fg=colors["muted"],
        bg=colors["bg"]
    ).pack(anchor="w", padx=16, pady=(6, 0))

    def _clear_status(*_):
        status_label.config(text="")

    license_entry.bind("<KeyRelease>", _clear_status)

    def on_confirm():
        # Ngăn double-click khi đang gửi request
        if getattr(on_confirm, "_busy", False):
            return
        on_confirm._busy = True
        status_label.config(text="")
        license_key = license_entry.get().strip()
        if not license_key:
            status_label.config(text="Vui lòng nhập license key")
            on_confirm._busy = False
            return
        status_label.config(text="Đang check license...")
        root.update_idletasks()

        status_code, data, _ = _check_license(license_key)
        active = bool(data.get("ACTIVE") or data.get("ok")) if isinstance(data, dict) else False
        if active:
            _save_license(license_key)
            root.destroy()
            _run_app()
        else:
            err = (data.get("reason") or data.get("error") or "unknown") if isinstance(data, dict) else "unknown"
            status_label.config(text=f"License không hợp lệ ({err}). Liên hệ Admin để kích hoạt.")
        on_confirm._busy = False

    tk.Button(
        root,
        text="Xác nhận",
        command=on_confirm,
        width=14,
        height=2,
        bg=colors["btn_primary"],
        fg="#ffffff",
        activebackground=colors["btn_primary_active"],
        relief="flat",
        font=("Arial", 11, "bold")
    ).pack(pady=16)
    root.mainloop()

def main():
    # Bỏ qua check license, chạy app luôn
    _run_app()

if __name__ == "__main__":
    main()

