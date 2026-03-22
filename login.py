from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import time
from pathlib import Path
from typing import Callable

try:
    from settings_manager import (
        DATA_GENERAL_DIR as RUNTIME_DATA_GENERAL_DIR,
        CONFIG_FILE as RUNTIME_CONFIG_PATH,
        CHROME_USER_DATA_ROOT as RUNTIME_CHROME_USER_DATA_ROOT,
    )
except Exception:
    RUNTIME_DATA_GENERAL_DIR = None
    RUNTIME_CONFIG_PATH = None
    RUNTIME_CHROME_USER_DATA_ROOT = None

from chrome import (
    WORKSPACE_DIR,
    FLOW_URL,
    PROFILE_NAME,
    open_profile_chrome,
    kill_profile_chrome,
)

DATA_GENERAL_DIR = Path(RUNTIME_DATA_GENERAL_DIR) if RUNTIME_DATA_GENERAL_DIR is not None else (WORKSPACE_DIR / "data_general")
CONFIG_PATH = Path(RUNTIME_CONFIG_PATH) if RUNTIME_CONFIG_PATH is not None else (DATA_GENERAL_DIR / "config.json")


def _log(message: str, logger: Callable[[str], None] | None = None) -> None:
    if callable(logger):
        try:
            logger(message)
            return
        except Exception:
            pass
    print(message)


def _is_stopped(stop_check: Callable[[], bool] | None) -> bool:
    try:
        return bool(stop_check()) if callable(stop_check) else False
    except Exception:
        return False


def _load_config() -> dict:
    try:
        if CONFIG_PATH.is_file():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_config(data: dict) -> None:
    DATA_GENERAL_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_profile_dir(profile_name: str | None = None) -> Path:
    default_root = Path(RUNTIME_CHROME_USER_DATA_ROOT) if RUNTIME_CHROME_USER_DATA_ROOT is not None else (WORKSPACE_DIR / "chrome_user_data")
    chrome_root = Path(os.getenv("CHROME_USER_DATA_ROOT", str(default_root)))
    chosen_profile = str(profile_name or "").strip()
    if not chosen_profile:
        try:
            from settings_manager import SettingsManager

            settings = SettingsManager.load_settings()
            if isinstance(settings, dict):
                chosen_profile = str(settings.get("current_profile") or "").strip()
        except Exception:
            chosen_profile = ""
    if not chosen_profile:
        chosen_profile = str(PROFILE_NAME or "").strip() or "PROFILE_1"
    if profile_name is not None:
        return chrome_root / chosen_profile
    return Path(os.getenv("CHROME_USER_DATA_DIR", str(chrome_root / chosen_profile)))


def _reset_profile_dir(profile_name: str | None = None) -> Path:
    profile_dir = _resolve_profile_dir(profile_name)
    try:
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:
        pass
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _is_chrome_running(debug_port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            return sock.connect_ex(("127.0.0.1", int(debug_port))) == 0
    except Exception:
        return False


def _pick_debug_port(start_port=9222, max_tries=10):
    port = int(start_port)
    for _ in range(int(max_tries)):
        if not _is_chrome_running(port):
            return port
        port += 1
    return int(start_port)


class LoginGuideDialog:
    REQUIRED_PROJECT_URL_PREFIX = "https://labs.google/fx/vi/tools/flow/project"

    @staticmethod
    def _setup_capture_listeners(page, log_callback=None):
        def _cb_log(message):
            _log(message, log_callback)

        capture_state = {
            "sessionId": None,
            "projectId": None,
            "access_token": None,
            "cookie": None,
        }

        submit_batch_captured = {"done": False}

        def on_request(request):
            url = request.url
            if ("https://labs.google/fx/" in url and not capture_state["cookie"]):
                try:
                    cookie_header = request.headers.get("cookie")
                except Exception:
                    cookie_header = None
                if cookie_header:
                    capture_state["cookie"] = cookie_header
            if ("https://labs.google/fx/api/trpc/general.submitBatchLog" in url
                    and not submit_batch_captured["done"]):
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = None
                if payload:
                    session_id = LoginGuideDialog._extract_session_id(payload)
                    if session_id:
                        capture_state["sessionId"] = session_id
                        submit_batch_captured["done"] = True
                        _cb_log(f"✅ [CAPTURE] sessionId: {session_id}")

            if ("https://labs.google/fx/api/trpc/project.createProject" in url
                    and not capture_state["projectId"]):
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = None
                if payload:
                    project_id = LoginGuideDialog._extract_project_id_from_payload(payload)
                    if project_id:
                        capture_state["projectId"] = project_id
                        _cb_log(f"✅ [CAPTURE] projectId(request): {project_id}")

            if ("https://labs.google/fx/_next/data" in url
                    and not capture_state["cookie"]):
                try:
                    cookie_header = request.headers.get("cookie")
                except Exception:
                    cookie_header = None
                if cookie_header:
                    capture_state["cookie"] = cookie_header

        def on_response(response):
            url = response.url
            if "https://labs.google/fx/api/trpc/project.createProject" in url and not capture_state["projectId"]:
                asyncio.create_task(
                    LoginGuideDialog._extract_project_id_from_response(response, capture_state, log_callback)
                )
            elif "https://labs.google/fx/_next/data" in url and not capture_state["access_token"]:
                asyncio.create_task(
                    LoginGuideDialog._extract_access_token_from_response(response, capture_state, log_callback)
                )
                if not capture_state.get("cookie"):
                    try:
                        req = response.request
                        cookie_header = req.headers.get("cookie") if req else None
                    except Exception:
                        cookie_header = None
                    if cookie_header:
                        capture_state["cookie"] = cookie_header

        page.on("request", on_request)
        page.on("response", on_response)

        return capture_state, on_request, on_response

    @staticmethod
    def _extract_session_id(payload):
        try:
            app_events = payload.get("json", {}).get("appEvents", [])
            for event in app_events:
                if event.get("event") == "PINHOLE_CREATE_NEW_PROJECT":
                    metadata = event.get("eventMetadata", {})
                    session_id = metadata.get("sessionId")
                    if session_id:
                        return session_id
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_project_id_from_payload(payload):
        try:
            return (
                payload.get("result", {})
                .get("data", {})
                .get("json", {})
                .get("result", {})
                .get("projectId")
            )
        except Exception:
            return None

    @staticmethod
    async def _extract_project_id_from_response(response, capture_state, log_callback=None):
        try:
            payload = await response.json()
        except Exception:
            return
        try:
            project_id = (
                payload.get("result", {})
                .get("data", {})
                .get("json", {})
                .get("result", {})
                .get("projectId")
            )
        except Exception:
            return
        if project_id:
            _log("✅ Đã nhận response project.createProject", log_callback)
            capture_state["projectId"] = project_id

    @staticmethod
    async def _extract_access_token_from_response(response, capture_state, log_callback=None):
        try:
            payload = await response.json()
        except Exception:
            return
        access_token = None
        try:
            access_token = (
                payload.get("pageProps", {})
                .get("session", {})
                .get("access_token")
            )
        except Exception:
            access_token = None
        if access_token:
            capture_state["access_token"] = access_token
            _log("✅ [CAPTURE] access_token(_next/data)", log_callback)

    @staticmethod
    def _is_capture_complete(capture_state):
        return all([
            capture_state.get("sessionId"),
            capture_state.get("projectId"),
            capture_state.get("access_token")
        ])

    @staticmethod
    def _missing_capture_fields(capture_state):
        missing = []
        if not capture_state.get("sessionId"):
            missing.append("sessionId")
        if not capture_state.get("projectId"):
            missing.append("projectId")
        if not capture_state.get("access_token"):
            missing.append("access_token")
        return ", ".join(missing)

    @staticmethod
    def _save_account_payload(username, capture_state, profile_dir: Path):
        config = _load_config()
        existing = config.get("account1", {}) if isinstance(config, dict) else {}
        existing = existing if isinstance(existing, dict) else {}

        preserved_email = str(existing.get("email") or username or "").strip()
        preserved_password = str(existing.get("password") or "")

        session_id = str(capture_state.get("sessionId") or existing.get("sessionId") or "").strip()
        project_id = str(capture_state.get("projectId") or existing.get("projectId") or "").strip()
        access_token = str(capture_state.get("access_token") or existing.get("access_token") or "").strip()
        cookie_value = capture_state.get("cookie")
        cookie_text = str(cookie_value).strip() if cookie_value is not None else str(existing.get("cookie") or "").strip()

        existing_url = str(existing.get("URL_GEN_TOKEN") or "").strip()
        if project_id:
            url_gen_token = f"{FLOW_URL}/project/{project_id}"
        else:
            url_gen_token = existing_url

        existing_profile_dir = str(existing.get("folder_user_data_get_token") or "").strip()
        profile_dir_value = str(profile_dir or "").strip() or existing_profile_dir

        account_payload = {
            "email": preserved_email,
            "password": preserved_password,
            "sessionId": session_id,
            "projectId": project_id,
            "access_token": access_token,
            "cookie": cookie_text,
            "folder_user_data_get_token": profile_dir_value,
            "URL_GEN_TOKEN": url_gen_token,
        }
        if isinstance(existing, dict) and existing.get("TYPE_ACCOUNT"):
            account_payload["TYPE_ACCOUNT"] = existing.get("TYPE_ACCOUNT")
        else:
            account_payload["TYPE_ACCOUNT"] = "ULTRA"
        if isinstance(existing, dict) and existing.get("cookie") and not account_payload.get("cookie"):
            account_payload["cookie"] = str(existing.get("cookie") or "")
        if account_payload.get("cookie") is None:
            account_payload["cookie"] = ""
        config["account1"] = account_payload
        _save_config(config)


async def login_veo3_auto(username, password, profile_name=None, log_callback=None, stop_check=None):
    from playwright.async_api import async_playwright

    node_opts = os.environ.get("NODE_OPTIONS", "").strip()
    if "--no-deprecation" not in node_opts:
        os.environ["NODE_OPTIONS"] = f"{node_opts} --no-deprecation".strip()

    def _cb_log(message):
        _log(message, log_callback)

    def _should_stop() -> bool:
        return _is_stopped(stop_check)

    async def _safe_goto(page_obj, url, label="", wait_until="domcontentloaded", timeout=30000):
        try:
            await page_obj.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as exc:
            note = f" ({label})" if label else ""
            _cb_log(f"⚠️  Page.goto timeout, bo qua{note}: {exc}")
            try:
                await page_obj.goto(url, wait_until="domcontentloaded", timeout=15000)
                return True
            except Exception:
                return False

    async def _click_with_delay(target, delay_seconds=2):
        await target.click()
        await asyncio.sleep(delay_seconds)

    async def _wait_for_ready(page_obj, selector, timeout=15000):
        await page_obj.wait_for_load_state("domcontentloaded")
        if selector:
            await page_obj.wait_for_selector(selector, timeout=timeout)

    async def _dismiss_chrome_signin_prompt(page_obj) -> bool:
        """Đóng popup 'Sign in to Chrome?' nếu xuất hiện để tránh kẹt flow auto-login."""
        selectors = [
            "button:has-text('Use Chrome without an account')",
            "button:has-text('Tiếp tục không cần tài khoản')",
            "button:has-text('Continue as guest')",
            "button:has-text('Không, cảm ơn')",
        ]
        for sel in selectors:
            try:
                btn = page_obj.locator(sel).first
                if await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.6)
                    _cb_log("✅ Đã đóng popup 'Sign in to Chrome'")
                    return True
            except Exception:
                continue
        return False

    async def _goto_and_wait_targets(
        page_obj,
        url: str,
        selectors: list[str],
        *,
        rounds: int = 4,
        per_round_timeout: int = 10000,
        label: str = "",
    ) -> bool:
        """Đi tới URL và chỉ thành công khi thấy 1 trong các target selector.

        Retry theo chu kỳ, mỗi vòng ~10s như yêu cầu.
        """
        for idx in range(max(1, int(rounds))):
            if _should_stop():
                return False

            ok = await _safe_goto(
                page_obj,
                url,
                label=label,
                timeout=max(10000, int(per_round_timeout)),
            )
            if ok:
                try:
                    await _dismiss_chrome_signin_prompt(page_obj)
                except Exception:
                    pass
                for selector in selectors:
                    try:
                        target = page_obj.locator(selector).first
                        if await target.is_visible():
                            return True
                    except Exception:
                        continue

                # Chờ thêm trong vòng hiện tại để target xuất hiện
                wait_steps = max(1, int(per_round_timeout // 1000))
                for _ in range(wait_steps):
                    if _should_stop():
                        return False
                    await asyncio.sleep(1)
                    try:
                        await _dismiss_chrome_signin_prompt(page_obj)
                    except Exception:
                        pass
                    for selector in selectors:
                        try:
                            target = page_obj.locator(selector).first
                            if await target.is_visible():
                                return True
                        except Exception:
                            continue

            if idx < rounds - 1:
                _cb_log(
                    f"⏳ Chưa sẵn sàng ở {label or url}. Retry {idx + 2}/{rounds} sau 10s..."
                )
                await asyncio.sleep(10)

        return False

    account_type_saved = False

    async def _detect_and_save_account_type():
        nonlocal account_type_saved
        if account_type_saved:
            return

        account_type = "ULTRA"
        try:
            btn = page.locator("//button[.//img[contains(@alt, 'Hình ảnh hồ sơ người dùng')]]")
            btn_count = 0
            for _ in range(5):
                try:
                    btn_count = await btn.count()
                except Exception:
                    btn_count = 0
                if btn_count > 0:
                    break
                await asyncio.sleep(1)

            if btn_count > 0:
                ultra_div = btn.locator("xpath=.//div[normalize-space()='ULTRA']")
                pro_div = btn.locator("xpath=.//div[normalize-space()='PRO']")
                ultra_count = 0
                pro_count = 0
                try:
                    ultra_count = await ultra_div.count()
                except Exception:
                    ultra_count = 0
                try:
                    pro_count = await pro_div.count()
                except Exception:
                    pro_count = 0

                if pro_count > 0:
                    account_type = "PRO"
                elif ultra_count > 0:
                    account_type = "ULTRA"
                else:
                    account_type = "NORMAL"
            else:
                account_type = "ULTRA"
        except Exception:
            account_type = "ULTRA"

        try:
            config = _load_config()
            if "account1" not in config:
                config["account1"] = {}
            config["account1"]["TYPE_ACCOUNT"] = account_type
            _save_config(config)
            _cb_log(f"✅ Lưu TYPE_ACCOUNT = {account_type}")
        except Exception as e:
            _cb_log(f"⚠️  Lỗi lưu TYPE_ACCOUNT: {e}")

        account_type_saved = True

    playwright = None
    browser = None
    page = None
    capture_state = None
    request_handler = None
    response_handler = None

    try:
        if _should_stop():
            return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}

        playwright = await async_playwright().start()
        profile_dir = _resolve_profile_dir(profile_name)
        try:
            _cb_log("🧹 Đóng Chrome profile do tool mở trước khi auto login...")
            kill_profile_chrome(profile_dir)
            await asyncio.sleep(1)
        except Exception as exc:
            _cb_log(f"⚠️ Không thể đóng Chrome profile cũ: {exc}")

        _reset_profile_dir(profile_name)
        opened = open_profile_chrome(profile_name=profile_name, url=FLOW_URL, language="en-US")
        debug_port = int((opened or {}).get("port") or _pick_debug_port(9222, 10))
        profile_dir = Path((opened or {}).get("profile_dir") or _resolve_profile_dir(profile_name))
        _cb_log(f"🚀 Đang mở Chrome (debug_port={debug_port})")
        await asyncio.sleep(2)

        for _ in range(10):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://localhost:{debug_port}",
                    timeout=5000,
                )
                break
            except Exception:
                await asyncio.sleep(1)

        if not browser:
            message = f"Không kết nối được Chrome qua CDP ({debug_port})"
            _cb_log(f"❌ {message}")
            return {"success": False, "already_logged_in": False, "message": message}
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        if context.pages:
            page = context.pages[0]
        else:
            page = await context.new_page()

        entered_flow = await _goto_and_wait_targets(
            page,
            FLOW_URL,
            selectors=[
                "button:has-text('Tạo bằng Flow')",
                "button:has-text('Create with Flow')",
                "button:has-text('Dự án mới')",
                "button:has-text('New project')",
            ],
            rounds=4,
            per_round_timeout=10000,
            label="flow",
        )
        if not entered_flow:
            return {
                "success": False,
                "already_logged_in": False,
                "message": "Không vào được trang Flow sau nhiều lần thử.",
            }

        _cb_log("🔍 [Step 1] Kiểm tra nút 'Tạo bằng Flow'...")
        found_create_btn = False
        for attempt in range(4):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            for check in range(4):
                if _should_stop():
                    return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
                try:
                    btn = page.locator("//button[.//span[normalize-space()='Tạo bằng Flow' or normalize-space()='Create with Flow']]").first
                    if await btn.is_visible():
                        found_create_btn = True
                        await _click_with_delay(btn)
                        await _wait_for_ready(
                            page,
                            'input[type="email"][aria-label*="Email"]',
                            timeout=15000,
                        )
                        break
                except Exception:
                    pass
                if check < 3:
                    await asyncio.sleep(1)
            if found_create_btn:
                break
            if attempt < 3:
                await asyncio.sleep(1)
                ok_flow_retry = await _goto_and_wait_targets(
                    page,
                    FLOW_URL,
                    selectors=[
                        "button:has-text('Tạo bằng Flow')",
                        "button:has-text('Create with Flow')",
                    ],
                    rounds=1,
                    per_round_timeout=10000,
                    label="flow retry",
                )
                if not ok_flow_retry:
                    _cb_log("⚠️ Flow retry chưa thấy nút 'Tạo bằng Flow', tiếp tục vòng kiểm tra...")

        if not found_create_btn:
            message = "Không tìm thấy nút 'Tạo bằng Flow' sau 4 lần retry"
            return {"success": False, "already_logged_in": False, "message": message}

        _cb_log("🔍 [Step 2] Tìm email input...")
        email_input = None
        for attempt in range(4):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            try:
                email_input = page.locator('input[type="email"][aria-label*="Email"]').first
                if await email_input.is_visible():
                    break
            except Exception:
                pass
            if attempt < 3:
                await asyncio.sleep(1)

        if not email_input or not (await email_input.is_visible()):
            entered_signin = await _goto_and_wait_targets(
                page,
                "https://accounts.google.com/signin",
                selectors=['input[type="email"][aria-label*="Email"]', 'input[type="email"]'],
                rounds=4,
                per_round_timeout=10000,
                label="google signin",
            )
            if not entered_signin:
                return {
                    "success": False,
                    "already_logged_in": False,
                    "message": "Không vào được trang Google Signin sau nhiều lần thử.",
                }
            for attempt in range(4):
                if _should_stop():
                    return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
                try:
                    email_input = page.locator('input[type="email"][aria-label*="Email"]').first
                    if await email_input.is_visible():
                        break
                except Exception:
                    pass
                if attempt < 3:
                    await asyncio.sleep(1)
            if not email_input or not (await email_input.is_visible()):
                return {"success": False, "already_logged_in": False, "message": "Không tìm thấy email input"}

        _cb_log(f"📝 [Step 3] Nhập email: {username}")
        await email_input.fill("")
        await email_input.type(username, delay=2)
        await asyncio.sleep(2)

        next_btn = None
        for _ in range(3):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            next_btn = await page.query_selector('button:has-text("Next")')
            if next_btn:
                await _click_with_delay(next_btn)
                await _wait_for_ready(page, 'input[type="password"][aria-label*="password"]', timeout=15000)
                break
            await asyncio.sleep(1)
        if not next_btn:
            return {"success": False, "already_logged_in": False, "message": "Không tìm thấy nút Next sau email"}

        _cb_log("🔍 [Step 4] Tìm password input...")
        password_input = None
        for attempt in range(5):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            try:
                password_input = await page.query_selector('input[type="password"][aria-label*="password"]')
                if password_input:
                    break
            except Exception:
                pass
            if attempt < 4:
                await asyncio.sleep(1)
        if not password_input:
            return {"success": False, "already_logged_in": False, "message": "Không tìm thấy password input"}

        _cb_log("📝 [Step 5] Nhập password...")
        await password_input.fill("")
        await password_input.type(password, delay=2)
        await asyncio.sleep(2)

        _cb_log("🔍 [Step 6] Tìm nút Next (password)...")
        next_btn = None
        ok_next = 0
        for attempt in range(3):
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            try:
                next_btn = await page.query_selector('button:has-text("Next")')
                if next_btn:
                    ok_next = 1
                    await _click_with_delay(next_btn)
                    await _wait_for_ready(page, 'button:has-text("Dự án mới")', timeout=15000)
                    break
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(1)
        if ok_next == 0:
            return {"success": False, "already_logged_in": False, "message": "Không tìm thấy nút Next (password)"}

        _cb_log("🔍 [Step 7] Bấm nút 'Dự án mới'...")
        found_project_btn = False
        start_time = time.monotonic()
        while time.monotonic() - start_time < 30:
            if _should_stop():
                return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
            await asyncio.sleep(1)
            try:
                project_btn = await page.query_selector('button:has-text("Dự án mới")')
                if project_btn:
                    await _detect_and_save_account_type()
                    if capture_state is None:
                        capture_state, request_handler, response_handler = (
                            LoginGuideDialog._setup_capture_listeners(page, log_callback=log_callback)
                        )
                    await _click_with_delay(project_btn)
                    await asyncio.sleep(2)
                    found_project_btn = True
                    break
            except Exception:
                continue

        if not found_project_btn:
            _cb_log("⚠️ Không tìm thấy nút 'Dự án mới' sau 30s, thử xử lý popup onboarding...")
            dialog_locator = None
            try:
                dialogs = page.locator('[role="dialog"]')
                dialog_count = await dialogs.count()
                preferred_dialog = None
                free_credits_dialog = None
                for idx in range(dialog_count):
                    dlg = dialogs.nth(idx)
                    if await dlg.is_visible():
                        dialog_locator = dlg
                        if await dlg.locator('text=Trải nghiệm').first.is_visible():
                            preferred_dialog = dlg
                            break
                        if free_credits_dialog is None:
                            try:
                                if await dlg.locator('text=Free credits').first.is_visible():
                                    free_credits_dialog = dlg
                            except Exception:
                                pass
                if preferred_dialog is not None:
                    dialog_locator = preferred_dialog

                if preferred_dialog is None and free_credits_dialog is not None:
                    try:
                        start_btn = free_credits_dialog.locator('button:has-text("Bắt đầu")').first
                        if await start_btn.is_visible():
                            _cb_log("✅ Popup Free credits: bấm 'Bắt đầu'")
                            await _click_with_delay(start_btn)
                            await _wait_for_ready(page, 'button:has-text("Tiếp theo")', timeout=15000)
                    except Exception:
                        pass
            except Exception:
                dialog_locator = None

            try:
                if dialog_locator is not None:
                    next_step_btn = dialog_locator.locator('button:has-text("Tiếp theo")').first
                else:
                    next_step_btn = page.locator('button:has-text("Tiếp theo")').first
                if await next_step_btn.is_visible():
                    _cb_log("✅ Popup: bấm 'Tiếp theo'")
                    await _click_with_delay(next_step_btn)
                    await _wait_for_ready(page, 'button:has-text("Tiếp tục")', timeout=15000)
            except Exception:
                pass

            try:
                if dialog_locator is not None:
                    dialog_el = await dialog_locator.element_handle()
                    if dialog_el:
                        await page.evaluate(
                            """(dialog) => {
                                const candidates = Array.from(dialog.querySelectorAll('*')).filter(el => {
                                    const style = window.getComputedStyle(el);
                                    const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                                    return scrollable && el.scrollHeight > el.clientHeight;
                                });
                                if (candidates.length) {
                                    candidates.forEach(el => { el.scrollTop = el.scrollHeight; });
                                } else {
                                    dialog.scrollTop = dialog.scrollHeight;
                                }
                            }""",
                            dialog_el,
                        )
                else:
                    await page.evaluate(
                        """() => {
                            const candidates = Array.from(document.querySelectorAll('*')).filter(el => {
                                const style = window.getComputedStyle(el);
                                const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
                                return scrollable && el.scrollHeight > el.clientHeight;
                            });
                            candidates.forEach(el => { el.scrollTop = el.scrollHeight; });
                            window.scrollTo(0, document.body.scrollHeight);
                        }"""
                    )
                    await asyncio.sleep(1)
            except Exception:
                pass

            try:
                update_scope = dialog_locator if dialog_locator is not None else page
                update_text = update_scope.locator('text=Flow 2/3/2026 Update').first
                if await update_text.is_visible():
                    start_update_btn = update_scope.locator('button:has-text("Bắt đầu")').first
                    if await start_update_btn.is_visible():
                        _cb_log("✅ Popup update: bấm 'Bắt đầu'")
                        await _click_with_delay(start_update_btn)
                        await asyncio.sleep(1)
            except Exception:
                pass

            try:
                if dialog_locator is not None:
                    continue_btn = dialog_locator.locator('button:has-text("Tiếp tục")').first
                else:
                    continue_btn = page.locator('button:has-text("Tiếp tục")').first
                if await continue_btn.is_visible():
                    _cb_log("✅ Popup: bấm 'Tiếp tục'")
                    await _click_with_delay(continue_btn)
                    await _wait_for_ready(page, 'button:has-text("Dự án mới")', timeout=15000)
            except Exception:
                pass

            for _ in range(10):
                if _should_stop():
                    return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
                await asyncio.sleep(0.5)
                try:
                    project_btn = await page.query_selector('button:has-text("Dự án mới")')
                    if project_btn:
                        await _detect_and_save_account_type()
                        if capture_state is None:
                            capture_state, request_handler, response_handler = (
                                LoginGuideDialog._setup_capture_listeners(page, log_callback=log_callback)
                            )
                        await _click_with_delay(project_btn)
                        found_project_btn = True
                        break
                except Exception:
                    continue

        if not found_project_btn:
            return {
                "success": False,
                "already_logged_in": False,
                "message": "Không tìm thấy nút 'Dự án mới'.",
            }

        _cb_log("🔍 [Step 8] Chờ lấy token...")
        total_wait_seconds = 60
        capture_ok = False
        try:
            for _ in range(total_wait_seconds):
                if _should_stop():
                    return {"success": False, "stopped": True, "already_logged_in": False, "message": "Đã dừng auto login."}
                await asyncio.sleep(1)
                if capture_state:
                    if not capture_state.get("cookie"):
                        try:
                            cookies = await page.context.cookies("https://labs.google/fx")
                            if cookies:
                                parts = []
                                for item in cookies:
                                    name = item.get("name")
                                    value = item.get("value")
                                    if name and value is not None:
                                        parts.append(f"{name}={value}")
                                if parts:
                                    capture_state["cookie"] = "; ".join(parts)
                        except Exception:
                            pass
                    LoginGuideDialog._save_account_payload(username, capture_state, profile_dir)
                if capture_state and LoginGuideDialog._is_capture_complete(capture_state):
                    LoginGuideDialog._save_account_payload(username, capture_state, profile_dir)
                    capture_ok = True
                    break
        finally:
            if page and request_handler:
                try:
                    page.off("request", request_handler)
                except Exception:
                    pass
            if page and response_handler:
                try:
                    page.off("response", response_handler)
                except Exception:
                    pass

        if not capture_ok:
            missing = LoginGuideDialog._missing_capture_fields(capture_state or {})
            message = f"Không tìm thấy token trong {total_wait_seconds}s. Thiếu: {missing}"
            return {"success": False, "already_logged_in": False, "message": message}

        _cb_log("✅✅✅ LOGIN THÀNH CÔNG!")
        return {
            "success": True,
            "already_logged_in": False,
            "message": "✅ Đã login thành công.",
        }

    except Exception as e:
        import traceback
        _cb_log(f"❌ Lỗi auto login: {e}")
        _cb_log(traceback.format_exc())
        return {"success": False, "already_logged_in": False, "message": str(e)}

    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright:
                await playwright.stop()
        except Exception:
            pass


def auto_login_veo3(
    username: str,
    password: str,
    profile_name: str | None = None,
    logger: Callable[[str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict:
    user = str(username or "").strip()
    pwd = str(password or "")
    if not user or not pwd:
        return {"success": False, "message": "Thiếu tài khoản hoặc mật khẩu."}

    if _is_stopped(stop_check):
        return {"success": False, "stopped": True, "message": "Đã dừng auto login."}

    data = _load_config()
    account = data.get("account1") if isinstance(data.get("account1"), dict) else {}
    account = dict(account or {})
    account["email"] = user
    account["password"] = pwd
    account.setdefault("sessionId", "")
    account.setdefault("projectId", "")
    account.setdefault("access_token", "")
    account.setdefault("cookie", "")
    account.setdefault("TYPE_ACCOUNT", "ULTRA")
    data["account1"] = account
    _save_config(data)

    try:
        return asyncio.run(
            login_veo3_auto(
                user,
                pwd,
                profile_name=profile_name,
                log_callback=logger,
                stop_check=stop_check,
            )
        )
    except Exception as exc:
        return {"success": False, "message": f"Không chạy được auto login: {exc}"}
