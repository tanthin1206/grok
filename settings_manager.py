from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_bundle_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        try:
            return Path(str(meipass)).resolve()
        except Exception:
            pass
    return _resolve_base_dir()


BASE_DIR = _resolve_base_dir()
BUNDLE_DIR = _resolve_bundle_dir()
DATA_GENERAL_DIR = BASE_DIR / "data_general"
WORKFLOWS_DIR = BASE_DIR / "Workflows"
PROJECT_DATA_FILE = "test.json"
SETTINGS_FILE = DATA_GENERAL_DIR / "settings.json"
CONFIG_FILE = DATA_GENERAL_DIR / "config.json"
CHROME_USER_DATA_ROOT = BASE_DIR / "chrome_user_data"


def get_icon_path(name: str) -> str:
    filename = str(name or "").strip()
    if not filename:
        return ""
    candidates = [
        BASE_DIR / "icons" / filename,
        BUNDLE_DIR / "icons" / filename,
    ]
    for path in candidates:
        try:
            if path.is_file():
                return str(path)
        except Exception:
            continue
    return ""


class SettingsManager:
    @staticmethod
    def load_config() -> dict:
        try:
            if CONFIG_FILE.exists():
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    @staticmethod
    def save_config(config: dict) -> None:
        DATA_GENERAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config or {}, f, ensure_ascii=False, indent=2)

    @staticmethod
    def load_settings() -> dict:
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"current_profile": os.getenv("PROFILE_NAME", "PROFILE_1")}

    @staticmethod
    def create_chrome_userdata_folder(profile_name: str | None = None) -> str:
        profile = str(profile_name or os.getenv("PROFILE_NAME", "PROFILE_1")).strip() or "PROFILE_1"
        path = CHROME_USER_DATA_ROOT / profile
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


for _p in (DATA_GENERAL_DIR, WORKFLOWS_DIR, CHROME_USER_DATA_ROOT):
    try:
        _p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
