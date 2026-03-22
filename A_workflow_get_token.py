import asyncio
import os
import sys
import time
import socket
import json
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from playwright.async_api import async_playwright
from settings_manager import SettingsManager, WORKFLOWS_DIR, PROJECT_DATA_FILE

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

try:
    from chrome_process_manager import ChromeProcessManager
except ModuleNotFoundError:
    import importlib.util

    def _load_module(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(f"Khong the load {name} tu {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    ChromeProcessManager = _load_module("chrome_process_manager", APP_ROOT / "chrome_process_manager.py").ChromeProcessManager

BLOCK_KEYWORDS = [
    "batchAsyncGenerateVideoText",
    "batchAsyncGenerateVideoStartImage",
]
RECAPTCHA_SITE_KEY = "k=6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

DEBUG = False  # ✅ Set DEBUG=True để show log chi tiết
hide_window_config = True  # ✅ Set True để ẩn cửa sổ Chrome khi không headless

# DEBUG = True  # ✅ Set DEBUG=True để show log chi tiết
# hide_window_config = False  # ✅ Set True để ẩn cửa sổ Chrome khi không headless


try:
    _node_opts = os.environ.get("NODE_OPTIONS", "").strip()
    if "--no-deprecation" not in _node_opts:
        os.environ["NODE_OPTIONS"] = f"{_node_opts} --no-deprecation".strip()
except Exception:
    pass



# DEBUG = True  # ✅ Set DEBUG=True để show log chi tiết
# hide_window_config = False  # ✅ Set True để ẩn cửa sổ Chrome khi không headless


CHROME_EXTRA_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-popup-blocking",
    "--mute-audio",
    "--window-size=800,600",

    # 🚀 tối ưu nặng
    "--blink-settings=imagesEnabled=false",
    "--disable-features=Translate,BackForwardCache",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


def _is_recaptcha_reload(url: str) -> bool:
    return "/recaptcha/enterprise/reload" in url and RECAPTCHA_SITE_KEY in url


def _extract_recaptcha_token(text: str):
    marker = '["rresp","'
    start = text.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = text.find('"', start)
    if end == -1:
        return None
    return text[start:end]


class TokenCollector:
    def __init__(
        self,
        project_url,
        chrome_userdata_root=None,
        profile_name=None,
        debug_port=9222,
        headless=False,
        hide_window=hide_window_config,
        token_timeout=60,
        idle_timeout=150,
        log_callback=None,
        stop_check=None,
        clear_data_interval=1,
        keep_chrome_open=False,
        close_chrome_after_token=False,
        mode=None,
    ):
        self.project_url = project_url
        self.chrome_userdata_root = chrome_userdata_root
        self.profile_name = profile_name
        self.debug_port = debug_port
        self.headless = headless
        self.hide_window = hide_window
        self.token_timeout = token_timeout
        self.idle_timeout = idle_timeout
        self.log_callback = log_callback
        self.stop_check = stop_check
        self.clear_data_interval = clear_data_interval
        self.keep_chrome_open = bool(keep_chrome_open)
        self.close_chrome_after_token = bool(close_chrome_after_token)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._token_future = None
        self._last_token_ts = time.time()
        # Khởi tạo timestamp để tránh reload/clear ngay lần chạy đầu
        self._last_page_reload_ts = time.time()
        self._idle_task = None
        self._idle_closed = False
        self._getting_token = False
        self._restart_lock = asyncio.Lock()
        self._trigger_retry_max = 2
        self._flow_url = "https://labs.google/fx/vi/tools/flow"
        self._routes_applied = False
        self._close_task = None
        self._close_after_token_delay = 10
        self._last_restart_ts = 0.0
        self._min_restart_interval_s = 25.0
        self.mode = (mode or "video").lower()
        self._mode_locked = bool(mode)
        self._apply_mode_from_test_json()
        self._configure_mode_flags()
        self._login_required = False
        self._chrome_userdata_dir = ""

    async def __aenter__(self):
        await self._start_browser()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # ✅ KHÔNG tự động đóng Chrome khi thoát context
        # Chrome chỉ đóng khi workflow gọi close_after_workflow() hoặc STOP
        await self._stop_idle_watchdog()

    def _log(self, message: str):
        """Log message quan trọng (luôn show)"""
        if callable(self.log_callback):
            try:
                self.log_callback(message)
                return
            except Exception:
                pass
        try:
            ChromeProcessManager.log(message)
        except Exception:
            pass
    
    def _debug_log(self, message: str):
        """Log chi tiết (chỉ show nếu DEBUG=True)"""
        if not DEBUG:
            return
        self._log(message)

    def _normalize_text(self, text: str) -> str:
        """Loại bỏ dấu/ khoảng thừa để so khớp run_mode ổn định."""
        if not isinstance(text, str):
            return ""
        normalized = unicodedata.normalize("NFD", text)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return " ".join(normalized.lower().split())

    def _is_image_run_mode(self, run_mode: str) -> bool:
        """Quyết định có phải chế độ tạo ảnh hay không từ run_mode test.json."""
        normalized = self._normalize_text(run_mode)
        if not normalized:
            return False
        return any(keyword in normalized for keyword in ("tao anh", "tao hinh anh"))

    def _apply_mode_from_test_json(self):
        """Override mode dựa trên run_mode trong test.json của current_project."""
        if self._mode_locked:
            return
        prev_mode = self.mode
        try:
            config = SettingsManager.load_config()
            current_project = config.get("current_project") if isinstance(config, dict) else None
            if not current_project:
                return
            test_file = WORKFLOWS_DIR / str(current_project) / "test.json"
            if not test_file.exists():
                return
            with open(test_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            run_mode = str(data.get("run_mode", ""))
            decided_mode = "generate_image" if self._is_image_run_mode(run_mode) else "video"
            if decided_mode != self.mode:
                self.mode = decided_mode
                self._debug_log(f"⚙️ Mode từ test.json ('{run_mode}') -> {self.mode}")
        except Exception:
            pass

    def _configure_mode_flags(self, force_reset: bool = False):
        """Cập nhật cờ sẵn sàng và block list theo mode hiện tại."""
        if self.mode == "generate_image":
            if "batchGenerateImages" not in BLOCK_KEYWORDS:
                BLOCK_KEYWORDS.append("batchGenerateImages")
            self._image_mode_ready = False
            self._video_mode_ready = False
        else:
            self.mode = "video"
            # Khi chuyển về video, luôn ép chọn lại video và không giả định image đã sẵn sàng
            self._image_mode_ready = False
            self._video_mode_ready = False

    def _should_stop(self) -> bool:
        try:
            if callable(self.stop_check):
                return bool(self.stop_check())
        except Exception:
            return False
        return False

    def _is_port_open(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                return sock.connect_ex(("127.0.0.1", int(port))) == 0
        except Exception:
            return False

    def _is_cdp_available(self, port: int) -> bool:
        """Return True if a Chrome DevTools Protocol HTTP endpoint responds on this port."""
        try:
            with urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=0.5) as resp:
                if getattr(resp, "status", 200) != 200:
                    return False
                raw = resp.read() or b""
            data = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
            return isinstance(data, dict) and (
                bool(data.get("webSocketDebuggerUrl"))
                or bool(data.get("Browser"))
                or bool(data.get("User-Agent"))
            )
        except Exception:
            return False

    def _find_running_cdp_port_for_user_data(self, user_data_dir: str) -> int | None:
        """Find an active Chrome CDP port that belongs to the given --user-data-dir (Windows)."""
        target = str(user_data_dir or "").strip()
        if os.name != "nt" or not target:
            return None
        try:
            target_norm = str(Path(target).resolve()).lower().replace("/", "\\")
        except Exception:
            target_norm = target.lower().replace("/", "\\")

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
            )
            output = str(proc.stdout or "")
        except Exception:
            return None

        for line in output.splitlines():
            cmd = str(line or "").strip().lower().replace("/", "\\")
            if not cmd:
                continue
            if "--remote-debugging-port=" not in cmd or "--user-data-dir=" not in cmd:
                continue
            if target_norm not in cmd:
                continue
            m = re.search(r"--remote-debugging-port=(\d+)", cmd)
            if not m:
                continue
            try:
                return int(m.group(1))
            except Exception:
                continue
        return None

    def _port_belongs_to_expected_profile(self, port: int, user_data_dir: str) -> bool:
        expected_port = self._find_running_cdp_port_for_user_data(user_data_dir)
        return isinstance(expected_port, int) and int(expected_port) == int(port)

    def _ensure_debug_port(self):
        """Nếu port đang bị chiếm bởi process khác (không phải CDP), tự chọn port khác."""
        try:
            port = int(self.debug_port)
        except Exception:
            port = 9222
            self.debug_port = port

        if self._is_port_open(port) and not self._is_cdp_available(port):
            new_port = self._find_free_port(port + 1)
            self._log(f"⚠️ Port {port} đang bị chiếm (không phải Chrome CDP), đổi sang port {new_port}")
            self.debug_port = new_port

    def _wait_for_cdp_ready(self, port: int, timeout_s: float = 8.0) -> bool:
        deadline = time.time() + max(0.5, float(timeout_s))
        while time.time() < deadline:
            if self._should_stop():
                return False
            if self._is_cdp_available(port):
                return True
            time.sleep(0.3)
        return False

    async def _start_browser(self):
        if self._should_stop():
            self._debug_log("🛑 TokenCollector nhận tín hiệu stop")
            return
        chrome_userdata_root = self.chrome_userdata_root
        if not chrome_userdata_root:
            chrome_userdata_root = os.path.join(os.path.dirname(__file__), "chrome_userdata_test")
            os.makedirs(chrome_userdata_root, exist_ok=True)
        self._chrome_userdata_dir = str(chrome_userdata_root)

        expected_port = self._find_running_cdp_port_for_user_data(self._chrome_userdata_dir)
        if isinstance(expected_port, int) and expected_port > 0 and expected_port != int(self.debug_port):
            self._debug_log(
                f"🔗 Phát hiện Chrome đúng profile tại CDP port {expected_port}, chuyển từ {self.debug_port}"
            )
            self.debug_port = int(expected_port)

        # ✅ Tránh crash khi port debug bị chiếm bởi app khác (không phải Chrome/CDP)
        self._ensure_debug_port()

        # Dùng CDP check thay vì chỉ check socket open để tránh nhầm port của service khác.
        cdp_running = self._is_cdp_available(self.debug_port)
        if cdp_running and (not self._port_belongs_to_expected_profile(self.debug_port, self._chrome_userdata_dir)):
            new_port = self._find_free_port(int(self.debug_port) + 1)
            self._log(
                f"⚠️ CDP port {self.debug_port} đang thuộc Chrome profile khác, chuyển sang port {new_port} cho profile hiện tại"
            )
            self.debug_port = int(new_port)
            cdp_running = False

        if cdp_running:
            if self.headless:
                if ChromeProcessManager._current_chrome_pid and ChromeProcessManager.is_process_alive(
                    ChromeProcessManager._current_chrome_pid
                ):
                    self._debug_log("🛑 Chrome do tool mở đang chạy. Đóng để mở headless...")
                    try:
                        ChromeProcessManager.close_chrome_gracefully()
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                else:
                    new_port = self._find_free_port(self.debug_port + 1)
                    self._debug_log(f"⚠️  Port {self.debug_port} đang có Chrome khác, dùng port {new_port} cho headless")
                    self.debug_port = new_port
                    cdp_running = False
            else:
                # Quan trọng: không mở thêm tab mới khi Chrome/CDP đã chạy.
                # Việc mở URL ở đây sẽ nhân nhiều tab Flow và gây nháy cửa sổ.
                # Điều hướng sẽ được xử lý sau bằng page.goto trên tab hiện có.
                self._debug_log("🔄 Reuse Chrome/CDP hiện tại, không mở tab mới")

        # ✅ Nếu cần mở Chrome mới: đảm bảo port chưa bị chiếm trước khi launch
        if not cdp_running:
            if self._is_port_open(self.debug_port):
                new_port = self._find_free_port(self.debug_port + 1)
                self._log(f"⚠️ Port {self.debug_port} đã đang dùng, chọn port {new_port} để mở Chrome")
                self.debug_port = new_port

            result = ChromeProcessManager.open_chrome_with_url(
                chrome_userdata_root,
                self.project_url,
                debug_port=self.debug_port,
                profile_name=self.profile_name,
                headless=self.headless,
                hide_window=self.hide_window,
                extra_args=CHROME_EXTRA_ARGS
            )
            
            # ✅ CHECK: Chrome khởi động thất bại?
            if result is None:
                self._log("❌ Chrome không thể khởi động!")
                return

            # ✅ WAIT để CDP sẵn sàng (đừng chỉ check port open)
            if not self._wait_for_cdp_ready(self.debug_port, timeout_s=10.0):
                self._debug_log(f"❌ Chrome không sẵn sàng CDP sau khi khởi động (port {self.debug_port})")
                return
        else:
            # ✅ Chrome đã chạy trên port này; vẫn chờ CDP ready nếu vừa mở/đang khởi tạo
            if not self._wait_for_cdp_ready(self.debug_port, timeout_s=5.0):
                self._debug_log(f"❌ CDP không sẵn sàng trên port {self.debug_port}")
                return

        try:
            self.playwright = await async_playwright().start()
        except Exception as e:
            self._log(f"❌ Lỗi khởi động Playwright: {e}")
            return
        
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                f"http://localhost:{self.debug_port}"
            )
        except Exception as e:
            self._log(f"❌ Lỗi kết nối CDP port {self.debug_port}: {e}")
            try:
                await self.playwright.stop()
            except:
                pass
            return
        
        try:
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        except Exception as e:
            self._debug_log(f"❌ Lỗi tạo context/page: {e}")
            try:
                await self.browser.close()
            except:
                pass
            try:
                await self.playwright.stop()
            except:
                pass
            return

        await self._ensure_request_blocking()

        self.page.on("response", self._on_response)

        await self._start_idle_watchdog()
        self._video_mode_ready = False

    async def _apply_request_blocking(self):
        if not self.context or not self.page:
            return
        try:
            urls_to_block = [
                "*aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText*",
                "*aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage*",
            ]
            if self.mode == "generate_image":
                urls_to_block.append("*flowMedia:batchGenerateImages*")
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send("Network.enable")
            await cdp.send(
                "Network.setBlockedURLs",
                {
                    "urls": urls_to_block
                },
            )
        except Exception:
            pass

    async def _ensure_request_blocking(self):
        if not self.context or not self.page:
            return
        await self._apply_request_blocking()
        if not self._routes_applied:
            try:
                await self.context.route("**/*", self._route_handler)
                self._routes_applied = True
            except Exception:
                pass

    async def _is_page_accessible(self):
        try:
            url = self.page.url if self.page else ""
        except Exception:
            url = ""
        return bool(url) and self._is_project_url(url)

    def _mode_check_xpath(self, mode: str) -> str:
        mapping = {
            "video": ".//button[@aria-haspopup='menu' and contains(normalize-space(.), 'Video')]",
            "generate_image": ".//button[@aria-haspopup='menu' and (contains(normalize-space(.), 'Nano Banana') or contains(normalize-space(.), 'Imagen'))]",
        }
        return mapping.get(mode, "")

    def _mode_tab_xpath(self, mode: str) -> str:
        mapping = {
            "video": ".//button[@type='button' and @role='tab' and contains(normalize-space(.), 'Video')]",
            "generate_image": ".//button[@type='button' and @role='tab' and contains(normalize-space(.), 'Image')]",
        }
        return mapping.get(mode, "")

    async def _first_visible_locator(self, xpath: str, timeout_ms: int = 2000):
        if not self.page or not xpath:
            return None
        locator = self.page.locator(f"xpath={xpath}")
        deadline = time.time() + (max(200, timeout_ms) / 1000.0)
        while time.time() < deadline:
            try:
                count = await locator.count()
                for idx in range(count):
                    candidate = locator.nth(idx)
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                await self.page.wait_for_timeout(120)
            except Exception:
                break
        return None

    async def _detect_current_mode(self, timeout_ms: int = 1800, preferred_mode: str = ""):
        if not self.page:
            return "", ""

        modes = ["video", "generate_image"]
        pref = str(preferred_mode or "").strip().lower()
        if pref in modes:
            modes = [pref] + [m for m in modes if m != pref]

        # Ưu tiên mode mong muốn trước để tránh chờ timeout không cần thiết.
        per_try_timeout = max(300, int(timeout_ms / max(1, len(modes))))
        for mode_name in modes:
            btn = await self._first_visible_locator(self._mode_check_xpath(mode_name), timeout_ms=per_try_timeout)
            if btn is None:
                continue
            try:
                text = (await btn.inner_text()) or ""
            except Exception:
                text = ""
            return mode_name, " ".join(text.split()).strip()

        return "", ""

    async def _is_mode_button_active(self, mode: str, timeout_ms: int = 2000) -> bool:
        if not self.page:
            return False
        xpath = self._mode_check_xpath(mode)
        if not xpath:
            return False
        visible_btn = await self._first_visible_locator(xpath, timeout_ms=max(300, timeout_ms))
        return bool(visible_btn)

    async def _verify_mode_selection(self, mode: str, timeout_ms: int = 2000) -> bool:
        current_mode, current_text = await self._detect_current_mode(timeout_ms=timeout_ms, preferred_mode=mode)
        if current_mode == mode:
            self._log(f"✅ [{mode}] Xác nhận đúng mode")
            return True
        if current_mode:
            self._log(f"ℹ️ Mode hiện tại: {current_mode}")
        else:
            self._log("⚠️ Không xác định được mode hiện tại từ nút kiểm tra")
        return False

    async def _wait_web_stable_by_create_button(self, timeout_ms: int = 30000) -> bool:
        if not self.page:
            return False
        create_btn_xpath = (
            ".//button[i[normalize-space()='arrow_forward'] and "
            "(span[normalize-space()='Tạo'] or span[normalize-space()='Create'] or pan[normalize-space()='Create'])]"
        )
        self._log("⏳waiting web stable")
        deadline = time.time() + (max(1000, timeout_ms) / 1000.0)
        while time.time() < deadline:
            if self._should_stop():
                return False
            btn = await self._first_visible_locator(create_btn_xpath, timeout_ms=300)
            if btn is not None:
                self._log("✅ Web Stabled")
                return True
            await asyncio.sleep(0.15)
        self._log("⚠️ Quá thời gianwaiting web stable: chưa thấy nút Tạo/Create")
        return False

    async def _click_with_fallback(self, locator, label: str = "element") -> bool:
        if not self.page or locator is None:
            return False
        try:
            await locator.click(timeout=1200)
            return True
        except Exception:
            pass
        try:
            await locator.click(timeout=1200, force=True)
            return True
        except Exception:
            pass
        try:
            await locator.evaluate("el => el.click()")
            return True
        except Exception as e:
            self._debug_log(f"⚠️ Click fallback thất bại ({label}): {e}")
            return False

    async def _is_mode_menu_open(self) -> bool:
        if not self.page:
            return False
        try:
            video_tab = self.page.locator(f"xpath={self._mode_tab_xpath('video')}")
            image_tab = self.page.locator(f"xpath={self._mode_tab_xpath('generate_image')}")

            video_count = await video_tab.count()
            for idx in range(video_count):
                try:
                    if await video_tab.nth(idx).is_visible():
                        return True
                except Exception:
                    pass

            image_count = await image_tab.count()
            for idx in range(image_count):
                try:
                    if await image_tab.nth(idx).is_visible():
                        return True
                except Exception:
                    pass
        except Exception:
            return False
        return False

    async def _close_mode_menu_by_active_mode(self, mode: str) -> bool:
        if not self.page:
            return False
        mode_xpath = self._mode_check_xpath(mode)
        if not mode_xpath:
            return False

        # Nếu menu đang đóng sẵn thì coi như thành công
        if not await self._is_mode_menu_open():
            return True

        for attempt in range(1, 4):
            mode_btn = await self._first_visible_locator(mode_xpath, timeout_ms=1500)
            if mode_btn is None:
                return False

            clicked = await self._click_with_fallback(mode_btn, label=f"close mode menu {mode}")
            if not clicked:
                continue

            await self.page.wait_for_timeout(250)
            if not await self._is_mode_menu_open():
                return True

            self._debug_log(f"⚠️ [{mode}] Menu còn mở sau khi click nút mode (lần {attempt}), thử đóng lại")

        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(200)
            return not await self._is_mode_menu_open()
        except Exception:
            return False

    async def _switch_to_mode(self, target_mode: str, precheck: bool = True) -> bool:
        if not self.page:
            return False
        target_tab_xpath = self._mode_tab_xpath(target_mode)

        if not target_tab_xpath:
            return False

        if precheck and await self._verify_mode_selection(target_mode, timeout_ms=1600):
            return True

        max_mode_attempts = 3
        for attempt in range(1, max_mode_attempts + 1):
            try:
                detected_mode, detected_text = await self._detect_current_mode(timeout_ms=1200)
                if not detected_mode:
                    self._log(f"⚠️ [{target_mode}] Không detect được mode hiện tại để mở menu (lần {attempt})")
                    continue
                if detected_mode == target_mode:
                    self._log(f"✅ [{target_mode}] Detect lại đã đúng mode, bỏ qua bước chuyển")
                    return True

                opener_xpath = self._mode_check_xpath(detected_mode)
                self._debug_log(
                    f"🔁 [{target_mode}] Lần {attempt}/{max_mode_attempts}: đang ở {detected_mode} ('{detected_text}'), bấm nút mode hiện tại để mở chọn mode"
                )
                opener = await self._first_visible_locator(opener_xpath, timeout_ms=2200)
                if opener is None:
                    self._debug_log(f"⚠️ [{target_mode}] Không tìm thấy nút mode để mở menu (lần {attempt})")
                    continue

                clicked = await self._click_with_fallback(opener, label="mode opener")
                if not clicked:
                    self._debug_log(f"⚠️ [{target_mode}] Không click được nút mode hiện tại (lần {attempt})")
                    continue

                await self.page.wait_for_timeout(250)
                target_tab = await self._first_visible_locator(target_tab_xpath, timeout_ms=3000)
                if target_tab is None:
                    self._debug_log(f"⚠️ [{target_mode}] Không thấy tab chọn mode sau khi mở menu (lần {attempt})")
                    continue

                clicked_tab = await self._click_with_fallback(target_tab, label=f"tab {target_mode}")
                if not clicked_tab:
                    self._debug_log(f"⚠️ [{target_mode}] Không click được tab chọn mode (lần {attempt})")
                    continue

                self._log(f"🖱️ [{target_mode}] Đã bấm tab chuyển mode")
                await self.page.wait_for_timeout(1200)

                if await self._verify_mode_selection(target_mode, timeout_ms=3000):
                    closed = await self._close_mode_menu_by_active_mode(target_mode)
                    if closed:
                        self._log(f"🧹 [{target_mode}] Đã đóng menu mode sau khi xác nhận đúng mode")
                    else:
                        self._log(f"⚠️ [{target_mode}] Đã đổi mode đúng nhưng chưa đóng được menu")
                    return True

                self._debug_log(
                    f"🔁 [{target_mode}] Lần {attempt}/{max_mode_attempts}: đã click chuyển mode nhưng verify chưa đúng"
                )
            except Exception as e:
                self._debug_log(f"⚠️ [{target_mode}] Lỗi khi chuyển mode (lần {attempt}): {e}")

        return False

    async def _switch_to_image_mode(self, skip_initial_verify: bool = False):
        if not self.page:
            return False
        self._debug_log("🔍 [IMG] Kiểm tra/chuyển về chế độ ảnh theo nút mode + tab")
        if (not skip_initial_verify) and await self._verify_mode_selection("generate_image", timeout_ms=1500):
            self._image_mode_ready = True
            return True

        ok = await self._switch_to_mode("generate_image", precheck=not skip_initial_verify)
        self._image_mode_ready = bool(ok)
        if not ok:
            self._debug_log("⚠️ [IMG] Chuyển mode ảnh thất bại sau khi đã thử verify lại")
        return ok

    async def _switch_to_video_mode(self, skip_initial_verify: bool = False):
        if not self.page:
            return False
        self._debug_log("🔍 [VID] Kiểm tra/chuyển về chế độ video theo nút mode + tab")
        if (not skip_initial_verify) and await self._verify_mode_selection("video", timeout_ms=1500):
            self._video_mode_ready = True
            return True

        ok = await self._switch_to_mode("video", precheck=not skip_initial_verify)
        self._video_mode_ready = bool(ok)
        if ok:
            self._log("✅ Đã bật chế độ Video")
            return True

        return await self._prompt_login_and_stop(
            "Không thể chuyển sang chế độ Video bằng nút mode/tab"
        )

    async def _fill_prompt_input_text(self, text: str) -> bool:
        if not self.page:
            return False

        candidates = [
            ("textarea#PINHOLE_TEXT_AREA_ELEMENT_ID", "textarea"),
            ("textarea[placeholder*='Bạn muốn tạo gì']", "textarea"),
            ("textarea[placeholder*='What do you want']", "textarea"),
            ("div[contenteditable='true'][role='textbox']", "contenteditable"),
            ("div[contenteditable='true'][aria-label*='prompt']", "contenteditable"),
        ]

        for selector, input_type in candidates:
            try:
                locator = self.page.locator(selector).first
                await locator.wait_for(state="visible", timeout=500)
                if input_type == "textarea":
                    await locator.fill(text)
                    try:
                        await locator.dispatch_event("change")
                    except Exception:
                        pass
                else:
                    await locator.click()
                    try:
                        await self.page.keyboard.press("Control+A")
                        await self.page.keyboard.press("Backspace")
                    except Exception:
                        pass
                    await self.page.keyboard.type(text, delay=20)

                self._debug_log(f"✓ Đã nhập text bằng selector: {selector}")
                return True
            except Exception:
                continue

        return False


    async def _close_browser(self):
        try:
            if self.context:
                await self.context.unroute("**/*", self._route_handler)
        except Exception:
            pass
        self._routes_applied = False
        try:
            if self.page:
                self.page.off("response", self._on_response)
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    async def _recover_page_in_current_chrome(self) -> bool:
        """Reconnect Playwright/CDP to existing Chrome without closing/reopening Chrome window."""
        if self._should_stop():
            return False
        if not self._is_cdp_available(self.debug_port):
            return False
        if not self._port_belongs_to_expected_profile(self.debug_port, self._chrome_userdata_dir):
            self._debug_log("⚠️ Bỏ qua recover: CDP hiện tại không thuộc profile đang dùng")
            return False
        try:
            await self._close_browser()
        except Exception:
            pass
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.connect_over_cdp(
                f"http://localhost:{self.debug_port}"
            )
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self._ensure_request_blocking()
            try:
                self.page.on("response", self._on_response)
            except Exception:
                pass
            return bool(self.page and not self.page.is_closed())
        except Exception as exc:
            self._debug_log(f"⚠️ Recover page qua CDP thất bại: {exc}")
            return False

    async def _restart_browser_if_needed(self):
        if self._should_stop():
            return False
        
        # ✅ Kiểm tra Chrome có thực sự cần restart không
        chrome_running = self._is_cdp_available(self.debug_port)
        page_ok = self.page and not self.page.is_closed()
        
        # Chỉ restart nếu Chrome thực sự không chạy hoặc page bị đóng
        if not chrome_running or not page_ok:
            self._debug_log(f"🔍 Cần recover/restart: chrome_running={chrome_running}, page_ok={page_ok}, _idle_closed={self._idle_closed}")
            if chrome_running and not page_ok:
                recovered = await self._recover_page_in_current_chrome()
                if recovered:
                    self._debug_log("✅ Recover page thành công, không cần restart Chrome")
                    return True
            await self.restart_browser()
        elif self._idle_closed:
            # Chrome vẫn chạy nhưng flag bị set -> chỉ reset flag, không restart
            self._idle_closed = False
            self._debug_log("🔄 Reset _idle_closed, Chrome vẫn chạy tốt")
        
        return bool(self.page)

    async def _close_after_token(self):
        # ✅ KHÔNG tự động đóng Chrome sau khi lấy token
        # Chrome chỉ đóng khi workflow gọi close_after_workflow() hoặc STOP
        pass

    async def close_after_workflow(self):
        try:
            if self._close_task and not self._close_task.done():
                self._close_task.cancel()
        except Exception:
            pass
        try:
            await self._close_browser()
        except Exception:
            pass
        try:
            ChromeProcessManager.close_chrome_gracefully()
        except Exception:
            pass
        self._idle_closed = True

    async def restart_browser(self):
        if self._should_stop():
            return False
        async with self._restart_lock:
            if self._should_stop():
                return False
            now = time.time()
            since_last = now - float(self._last_restart_ts or 0.0)
            if since_last < self._min_restart_interval_s:
                self._debug_log(
                    f"⏱️ Bỏ qua full restart (mới {since_last:.1f}s), thử recover page để tránh nháy Chrome"
                )
                if await self._recover_page_in_current_chrome():
                    return True
            self._log("⏳ Dang khoi dong lai Chrome de lay token, vui long doi...")
            self._idle_closed = False
            if self.mode == "generate_image":
                self._image_mode_ready = False
            self._video_mode_ready = False
            try:
                await self._stop_idle_watchdog()
            except Exception:
                pass
            try:
                await self._close_browser()
            except Exception:
                pass
            try:
                ChromeProcessManager.close_chrome_gracefully()
            except Exception:
                pass
            await self._start_browser()
            self._last_restart_ts = time.time()
            return bool(self.page)

    async def _start_idle_watchdog(self):
        if self.keep_chrome_open:
            return
        if self._idle_task:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_task = loop.create_task(self._idle_watchdog())

    async def _stop_idle_watchdog(self):
        if not self._idle_task:
            return
        try:
            self._idle_task.cancel()
        except Exception:
            pass
        self._idle_task = None

    async def _idle_watchdog(self):
        # ✅ KHÔNG tự động đóng Chrome khi idle
        # Watchdog chỉ dùng để detect STOP signal
        while True:
            if self._should_stop():
                # Khi STOP -> đóng Chrome
                self._debug_log("🛑 Watchdog: STOP signal -> đóng Chrome")
                self._idle_closed = True
                try:
                    await self._close_browser()
                except Exception:
                    pass
                try:
                    ChromeProcessManager.close_chrome_gracefully()
                except Exception:
                    pass
                return
            # Chờ và check STOP mỗi 5 giây
            for _ in range(5):
                if self._should_stop():
                    break
                await asyncio.sleep(1)

    async def _route_handler(self, route, request):
        if any(keyword in request.url for keyword in BLOCK_KEYWORDS):
            self._debug_log(f"🛑 Blocked generate request: {request.url}")
            await route.fulfill(
                status=403,
                content_type="application/json",
                body='{"error":{"code":403,"message":"blocked for debug"}}',
            )
        else:
            await route.continue_()

    async def _on_response(self, response):
        if not _is_recaptcha_reload(response.url):
            return
        try:
            text = await response.text()
        except Exception:
            return
        token_value = _extract_recaptcha_token(text)
        if token_value and self._token_future and not self._token_future.done():
            self._token_future.set_result(token_value)

    async def _clear_site_storage(self):
        if not self.page:
            return
        origin = None
        current_url = self.page.url if self.page else ""
        if current_url:
            parsed = urlparse(current_url)
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        if not origin:
            return
        try:
            self._debug_log("🧹 Clear site storage...")
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send(
                "Storage.clearDataForOrigin",
                {
                    "origin": origin,
                    "storageTypes": ",".join(
                        [
                            "local_storage",
                            "session_storage",
                            "indexeddb",
                            "cache_storage",
                            "service_workers",
                            "websql",
                            "file_systems",
                            "shared_storage",
                        ]
                    ),
                },
            )
            try:
                await cdp.send("Storage.clearTrustTokens")
            except Exception:
                pass
            await cdp.send("Network.clearBrowserCache")
            self._debug_log("🔄 Reload page after clear storage...")
            await self.page.reload(wait_until="domcontentloaded")
            self._image_mode_ready = False
            self._video_mode_ready = False
        except Exception:
            pass

    async def _reload_project_page(self):
        if not self.page or self.page.is_closed():
            return False
        try:
            if not self._is_project_url(self.page.url or ""):
                await self._ensure_project_url()
            self._debug_log("🔄 Reload project page...")
            await self.page.reload(wait_until="domcontentloaded", timeout=20000)
            await self._ensure_request_blocking()
            self._last_page_reload_ts = time.time()
            if self.mode == "generate_image":
                self._image_mode_ready = False
            self._video_mode_ready = False
            return True
        except Exception:
            return False

    async def _trigger_token(self, clear_storage=False):
        if self._login_required:
            return False
        for attempt in range(1, self._trigger_retry_max + 1):
            if self._should_stop():
                return False
            if not self._is_project_url(self.project_url):
                try:
                    await self._ensure_project_url()
                except Exception:
                    return False
            ok = await self._trigger_token_once(clear_storage=clear_storage)
            if ok:
                return True
            if self._login_required:
                return False
            if attempt < self._trigger_retry_max:
                self._log("⚠️ Lỗi lấy token, thử lại...")
        return False

    async def _trigger_token_once(self, clear_storage=False, _has_reloaded=False):
        if self._should_stop():
            self._debug_log("🛑 TokenCollector dừng trong _trigger_token")
            return False
        if not self.page or self.page.is_closed():
            self._debug_log("❌ Page không tồn tại trong _trigger_token")
            await self.restart_browser()
            if not self.page or self.page.is_closed():
                return False
        
        try:
            current_url = ""
            try:
                current_url = self.page.url or ""
            except Exception:
                current_url = ""

            if not self._is_project_url(current_url):
                await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=20000)
                if not self._is_project_url(self.page.url):
                    await self._ensure_project_url()
            if clear_storage:
                if self._should_stop():
                    return False
                self._debug_log("🧹 Clear storage requested")
                await self._clear_site_storage()
                # ✅ Break storage clear wait into 0.1s chunks
                for _ in range(int(self.clear_data_interval * 10)):
                    if self._should_stop():
                        return False
                    await asyncio.sleep(0.1)
        except Exception as e:
            self._log(f"❌ Lỗi navigate/clear storage: {e}")
            if "Target page, context or browser has been closed" in str(e):
                await self.restart_browser()
                if not self.page or self.page.is_closed():
                    return False
                try:
                    if not self._is_project_url(self.project_url):
                        await self._ensure_project_url()
                    await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e2:
                    self._log(f"❌ Lỗi navigate lại sau restart: {e2}")
                    return False
                if clear_storage:
                    if self._should_stop():
                        return False
                    await self._clear_site_storage()
                    for _ in range(int(self.clear_data_interval * 10)):
                        if self._should_stop():
                            return False
                        await asyncio.sleep(0.1)
            else:
                try:
                    await self._ensure_project_url()
                    await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    return False
        
        prev_mode = self.mode
        self._apply_mode_from_test_json()
        if self.mode != prev_mode:
            self._configure_mode_flags(force_reset=True)

        web_stable = await self._wait_web_stable_by_create_button(timeout_ms=10000)
        if not web_stable:
            max_reload_attempts = 2
            for reload_attempt in range(1, max_reload_attempts + 1):
                if reload_attempt == 1:
                    self._log("🔄 Web UInStabled in 10s, reload lại trang (lần 1/2)...")
                else:
                    self._log("🔄 Sau reload lần 1, web UInStabled in 20s, reload lại lần 2...")
                try:
                    await self.page.reload(wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    self._log(f"⚠️ Reload web thất bại (lần {reload_attempt}): {e}")
                    continue

                web_stable = await self._wait_web_stable_by_create_button(timeout_ms=20000)
                if web_stable:
                    break

            if not web_stable:
                self._log("❌ Reload 2 lần vẫn chưa ổn định để lấy token")
                return False
        detected_mode, detected_text = await self._detect_current_mode(timeout_ms=2000, preferred_mode=self.mode)
        if detected_mode:
            self._log(f"ℹ️ Mode detect : {detected_mode}")
        else:
            self._log("⚠️ Web Stabled nhưng chưa detect được mode từ nút kiểm tra")

        if self.mode == "generate_image":
            if detected_mode == "generate_image":
                image_mode_ok = True
                self._log("✅ [generate_image] Xác nhận đúng mode")
            else:
                image_mode_ok = await self._verify_mode_selection("generate_image", timeout_ms=1500)
            if image_mode_ok:
                self._image_mode_ready = True
            else:
                self._image_mode_ready = False
                self._debug_log("🔍 [IMG] Chưa sẵn sàng image mode, bắt đầu chọn")
                ok = await self._switch_to_image_mode(skip_initial_verify=True)
                if not ok:
                    return False
        else:
            if detected_mode == "video":
                video_mode_ok = True
                self._log("✅ [video] Xác nhận đúng mode")
            else:
                video_mode_ok = await self._verify_mode_selection("video", timeout_ms=1500)
            if video_mode_ok:
                self._video_mode_ready = True
            else:
                self._video_mode_ready = False
                self._debug_log("🔍 [VID] Chưa sẵn sàng video mode, bắt đầu chọn")
                ok = await self._switch_to_video_mode(skip_initial_verify=True)
                if not ok:
                    return False

        self._log("➡️ Correctly mode, next step")

        try:
            # ✅ Chờ textarea với check stop flag mỗi 0.5s
            timeout_start = time.time()
            textarea_timeout = max(self.token_timeout, 60)
            last_wait_log_ts = 0.0
            while time.time() - timeout_start < textarea_timeout:
                if self._should_stop():
                    self._log("🛑 Stop detected khi chờ textarea")
                    return False
                try:
                    ok = await self._fill_prompt_input_text("a")
                    if ok:
                        await asyncio.sleep(0.5)
                        self._debug_log("✓ Đã nhập text xong")
                        break
                    if (time.time() - last_wait_log_ts) >= 5:
                        waited = int(time.time() - timeout_start)
                        self._log(f"⏳ Đang chờ ô nhập text... ({waited}s)")
                        last_wait_log_ts = time.time()
                except Exception:
                    await asyncio.sleep(0.5)
            else:
                self._log(f"⚠️  Timeout chờ textarea ({textarea_timeout}s)")
                return False
        except Exception as e:
            self._log(f"⚠️  Lỗi nhập text: {e}")
            return False

        # Retry logic để tìm button
        max_button_retries = 3
        for btn_attempt in range(max_button_retries):
            if self._should_stop():
                self._log("🛑 Stop detected khi tìm button")
                return False
            try:
                self._debug_log(f"🔍 Tìm button 'Tạo' (lần {btn_attempt + 1}/{max_button_retries})...")
                # ✅ Chờ button với check stop flag mỗi 0.5s
                timeout_start = time.time()
                while time.time() - timeout_start < 10:
                    if self._should_stop():
                        self._log("🛑 Stop detected khi chờ button")
                        return False
                    try:
                        create_button = (
                            self.page.locator("button:has-text('Tạo')")
                            .filter(has_not_text="Trình tạo cảnh") \
                            .filter(has_not_text="Không tạo được")
                            .last
                        )
                        await create_button.wait_for(state="visible", timeout=500)
                        await create_button.click()
                        return True
                    except Exception:
                        await asyncio.sleep(0.5)
                else:
                    self._log(f"⚠️ Lần {btn_attempt + 1} tìm button timeout")
                    if btn_attempt < max_button_retries - 1:
                        self._debug_log(f"⏳ Chờ 2 giây rồi retry...")
                        # ✅ Break 2 second wait into 0.2s chunks
                        for _ in range(10):
                            if self._should_stop():
                                self._log("🛑 Stop detected khi chờ retry button")
                                return False
                            await asyncio.sleep(0.2)
            except Exception as e:
                self._log(f"⚠️ Lần {btn_attempt + 1} tìm button thất bại: {e}")
                if btn_attempt < max_button_retries - 1:
                    self._debug_log(f"⏳ Chờ 2 giây rồi retry...")
                    # ✅ Break 2 second wait into 0.2s chunks
                    for _ in range(10):
                        if self._should_stop():
                            self._log("🛑 Stop detected khi chờ retry button")
                            return False
                        await asyncio.sleep(0.2)
        
        if not await self._verify_project_accessible():
            return False

        if not _has_reloaded:
            self._log("🔄 Không thấy nút sau 3 lần, reload trang và thử lại...")
            if await self._reload_project_page():
                return await self._trigger_token_once(clear_storage=clear_storage, _has_reloaded=True)

        self._log(f"❌ Không Lấy được token sau {max_button_retries} lần thử")
        return False

    async def get_token(self, clear_storage=False, token_timeout_override=None):
        if self._should_stop():
            self._log("🛑 TokenCollector dừng trước khi get_token")
            return None
        
        # ✅ Reset _idle_closed nếu Chrome vẫn chạy để tránh restart không cần thiết
        if self._idle_closed and self.page and not self.page.is_closed():
            self._idle_closed = False
            self._debug_log("🔄 Reset _idle_closed vì Chrome vẫn chạy")
        
        self._login_required = False
        effective_timeout = self.token_timeout if token_timeout_override is None else max(1, token_timeout_override)
        idle_elapsed = time.time() - self._last_token_ts
        # Khởi động: không ép clear/reload nếu chưa cần
        extended_timeout_needed = bool(clear_storage)
        self._getting_token = True
        self._last_token_ts = time.time()
        try:
            if not await self._restart_browser_if_needed():
                self._log("⚠️  Khong the khoi dong lai Chrome de lay token")
                return None
            await self._ensure_request_blocking()
            # Chỉ reload khi đã chạy trước đó và thực sự idle lâu
            if (
                idle_elapsed >= 60
                and self._last_page_reload_ts > 0
                and (time.time() - self._last_page_reload_ts) >= 30
            ):
                self._debug_log("🔄 Idle lâu, reload page trước khi lấy token")
                extended_timeout_needed = True
                await self._reload_project_page()
            if extended_timeout_needed:
                effective_timeout = max(effective_timeout, 60)
                self._debug_log(f"⏱️ Dùng extended token timeout {effective_timeout}s")
            trigger_start_time = time.time()
            loop = asyncio.get_running_loop()
            self._token_future = loop.create_future()
            try:
                ok = await self._trigger_token(clear_storage=clear_storage)
                if not ok:
                    if self._login_required:
                        return None
                    self._log("⚠️ Trigger token thất bại, reload page và thử lại 1 lần")
                    extended_timeout_needed = True
                    await self._reload_project_page()
                    if extended_timeout_needed:
                        effective_timeout = max(effective_timeout, 60)
                    ok = await self._trigger_token(clear_storage=clear_storage)
                    if not ok:
                        if self._login_required:
                            return None
                        return None
            except Exception:
                return None
            try:
                # Timeout từ lúc bấm button (start của _trigger_token), không phải từ đầu get_token
                elapsed = time.time() - trigger_start_time
                remaining_timeout = max(1, effective_timeout - elapsed)
                token = await asyncio.wait_for(self._token_future, timeout=remaining_timeout)
                if token:
                    self._log("✅ TokenCollector: lấy token thành công")
                    # ✅ KHÔNG tự động đóng Chrome sau khi lấy token
                    # Chrome chỉ đóng khi workflow gọi close_after_workflow()
                return token
            except asyncio.TimeoutError:
                self._log(f"⚠️  TokenCollector: timeout lấy token ({effective_timeout}s)")
                return None
            except Exception:
                self._log("⚠️  TokenCollector: lỗi lấy token")
                return None
        finally:
            self._getting_token = False

    def _is_project_url(self, url: str) -> bool:
        return isinstance(url, str) and "https://labs.google/fx/vi/tools/flow/project/" in url

    def _save_project_url(self, url: str):
        try:
            config = SettingsManager.load_config()
            account = config.get("account1", {}) if isinstance(config, dict) else {}
            account["URL_GEN_TOKEN"] = url
            config["account1"] = account
            SettingsManager.save_config(config)
        except Exception:
            pass

    def _save_project_url_to_test_json(self, url: str):
        try:
            config = SettingsManager.load_config()
            current_project = config.get("current_project") if isinstance(config, dict) else None
            if not current_project:
                return
            project_dir = WORKFLOWS_DIR / current_project
            project_dir.mkdir(parents=True, exist_ok=True)
            project_file = project_dir / PROJECT_DATA_FILE
            data = {}
            if project_file.exists():
                try:
                    with open(project_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data["URL_GEN_TOKEN"] = url
            with open(project_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _show_account_error_popup(self, message: str):
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Lỗi tài khoản", message)
            root.destroy()
        except Exception:
            pass

    async def _prompt_login_and_stop(self, reason: str):
        """Stop token flow when session likely logged out; close Chrome and notify user."""
        self._login_required = True
        self._log(reason)
        self._show_account_error_popup(
            f"{reason}\nVui lòng đăng nhập lại tài khoản VEO3 rồi bấm Lấy token."
        )
        try:
            await self.close_after_workflow()
        except Exception:
            pass
        return False

    async def _verify_project_accessible(self):
        """Check project link/session; if unreachable, ask user to re-login and stop."""
        if self._should_stop():
            return False
        if not self.project_url or not self._is_project_url(self.project_url):
            return await self._prompt_login_and_stop("URL dự án không hợp lệ hoặc thiếu.")
        if not self.page or self.page.is_closed():
            await self.restart_browser()
            if not self.page or self.page.is_closed():
                return await self._prompt_login_and_stop("Không mở được Chrome để kiểm tra dự án.")
        try:
            await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            self._log(f"⚠️ Không mở được trang dự án: {e}")
        try:
            current_url = self.page.url or ""
        except Exception:
            current_url = ""
        if not self._is_project_url(current_url):
            return await self._prompt_login_and_stop(
                "Không truy cập được trang dự án. Có thể đã bị đăng xuất khỏi VEO3."
            )
        return True

    async def _ensure_project_url(self):
        if not self.page:
            return

        await self._ensure_account_logged_in()

        if self._is_project_url(self.project_url):
            for _ in range(2):
                if self._should_stop():
                    return
                try:
                    await self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
                if self._is_project_url(self.page.url):
                    return
                await asyncio.sleep(1.5)

        await self._create_new_project_and_save_url()

    async def _ensure_account_logged_in(self):
        for attempt in range(2):
            if self._should_stop():
                return
            try:
                await self.page.goto(self._flow_url, wait_until="networkidle", timeout=20000)
            except Exception:
                pass
            await asyncio.sleep(2 + attempt)
            try:
                project_btn = self.page.locator("button:has-text('Dự án mới')").first
                if await project_btn.is_visible():
                    return
            except Exception:
                pass
            if attempt == 0:
                await asyncio.sleep(2)

        message = "Tài khoản lỗi hoặc chưa đăng nhập. Vui lòng đăng nhập lại."
        self._log(f"❌ {message}")
        self._show_account_error_popup(message)
        raise RuntimeError("ACCOUNT_NOT_LOGGED_IN")

    async def _create_new_project_and_save_url(self):
        try:
            await self.page.goto(self._flow_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        try:
            project_btn = self.page.locator("button:has-text('Dự án mới')").first
            if await project_btn.is_visible():
                await project_btn.click()
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                for _ in range(20):
                    url = self.page.url or ""
                    if self._is_project_url(url):
                        self._save_project_url(url)
                        self._save_project_url_to_test_json(url)
                        self._debug_log(f"✅ Lưu URL_GEN_TOKEN mới: {url}")
                        self.project_url = url
                        return
                    await asyncio.sleep(0.5)
        except Exception:
            pass

        self._log("❌ ERROR URL GEN TOKEN không hợp lệ")
        try:
            ChromeProcessManager.close_chrome_gracefully()
        except Exception:
            pass
        raise RuntimeError("ERROR URL GEN TOKEN không hợp lệ")

    def _find_free_port(self, start_port):
        port = start_port
        for _ in range(20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", port))
                    return port
                except OSError:
                    port += 1
        return start_port
