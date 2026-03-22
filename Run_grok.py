from __future__ import annotations
import sys
import os
from pathlib import Path
from branding_config import APP_VERSION, WINDOW_TITLE

def _ensure_project_root() -> None:
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    os.environ["PYTHONPATH"] = str(root)

def main() -> None:
    _ensure_project_root()
    print(f'[RUNNER] Start launcher for {WINDOW_TITLE} - {APP_VERSION}')
    
    # Bypass license check, run main app
    try:
        from main import main as app_main
        app_main()
    except Exception as e:
        print(f"[ERROR] Không thể khởi động ứng dụng: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
