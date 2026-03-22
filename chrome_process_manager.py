import subprocess
import platform
import os
import time
import shutil
import json
import asyncio
from pathlib import Path
from settings_manager import DATA_GENERAL_DIR


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


def _run_silent(cmd, shell: bool = False) -> None:
    try:
        subprocess.run(
            cmd,
            shell=bool(shell),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **_win_hidden_kwargs(),
        )
    except Exception:
        pass


class ChromeProcessManager:
    """Quản lý Chrome process"""
    
    # Lưu PID của Chrome process đang chạy
    _current_chrome_pid = None
    # Callback để log lên app UI
    _log_callback = None
    # Lưu tên profile Chrome để biết là Chrome nào được khởi động
    _chrome_profile_name = None
    
    @staticmethod
    def set_log_callback(callback):
        """Set callback function để log lên app UI
        
        Args:
            callback: function(message) - để append log vào app
        """
        ChromeProcessManager._log_callback = callback
    
    @staticmethod
    def log(message):
        """Log message lên app UI"""
        try:
            if ChromeProcessManager._log_callback and callable(ChromeProcessManager._log_callback):
                ChromeProcessManager._log_callback(message)
            else:
                print(message)
        except Exception as e:
            # Fallback to print if callback fails
            print(f"{message} (callback error: {e})")
    
    @staticmethod
    def find_chrome_path():
        """Tìm đường dẫn Chrome trên hệ thống"""
        if platform.system() == "Windows":
            possible_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%PROGRAMFILES(x86)%\Google\Chrome\Application\chrome.exe"),
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    return path
            
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                chrome_path, _ = winreg.QueryValueEx(key, "")
                if os.path.exists(chrome_path):
                    return chrome_path
            except Exception as e:
                pass  # Silent fail for registry lookup
        
        elif platform.system() == "Darwin":
            possible_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    return path
        
        elif platform.system() == "Linux":
            result = subprocess.run(["which", "google-chrome"], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
            
            result = subprocess.run(["which", "chromium-browser"], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        
        return None
    
    @staticmethod
    def is_chrome_running(debug_port=9222):
        """Kiểm tra xem Chrome có đang chạy với debug port không"""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', debug_port))
            sock.close()
            return result == 0
        except:
            return False

    @staticmethod
    def is_running_with_profile(profile_name, debug_port=9222):
        """Kiểm tra Chrome đang chạy có đúng profile hay không"""
        if not profile_name:
            return False
        if not ChromeProcessManager.is_chrome_running(debug_port):
            return False
        if not ChromeProcessManager._current_chrome_pid:
            return False
        if not ChromeProcessManager.is_process_alive(ChromeProcessManager._current_chrome_pid):
            return False
        return ChromeProcessManager._chrome_profile_name == profile_name
    
    @staticmethod
    def is_process_alive(pid):
        """Kiểm tra xem process có còn sống không (Windows only)"""
        if pid is None:
            return False
        
        try:
            if platform.system() == "Windows":
                # Dùng tasklist để kiểm tra
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    shell=False,
                    **_win_hidden_kwargs(),
                )
                return str(pid) in result.stdout
            else:
                # Unix-like systems
                os.kill(pid, 0)  # Signal 0 chỉ kiểm tra, không kill
                return True
        except (ProcessLookupError, OSError):
            return False
        except:
            return False
    
    @staticmethod
    def kill_chrome_process(pid=None):
        """Tắt Chrome process theo PID hoặc tất cả Chrome process"""
        try:
            if pid:
                # Đóng process cụ thể theo kiểu graceful trước, force nếu cần
                if platform.system() == "Windows":
                    _run_silent(["taskkill", "/PID", str(pid), "/T"])
                    time.sleep(0.8)
                    if ChromeProcessManager.is_process_alive(pid):
                        _run_silent(["taskkill", "/PID", str(pid), "/T", "/F"])
                    ChromeProcessManager.log(f" {pid} đã tắt")
                elif platform.system() == "Darwin":
                    os.system(f"kill {pid}")
                    time.sleep(0.6)
                    if ChromeProcessManager.is_process_alive(pid):
                        os.system(f"kill -9 {pid}")
                    ChromeProcessManager.log(f" {pid} đã tắt")
                elif platform.system() == "Linux":
                    os.system(f"kill {pid}")
                    time.sleep(0.6)
                    if ChromeProcessManager.is_process_alive(pid):
                        os.system(f"kill -9 {pid}")
                    ChromeProcessManager.log(f"{pid} đã tắt")
            else:
                # Đóng tất cả Chrome process theo kiểu graceful trước
                if platform.system() == "Windows":
                    _run_silent(["taskkill", "/IM", "chrome.exe"])
                    time.sleep(1.0)
                    _run_silent(["taskkill", "/F", "/IM", "chrome.exe"])
                    ChromeProcessManager.log("✓ Tất cả Chrome process đã tắt")
                elif platform.system() == "Darwin":
                    os.system("pkill Chrome")
                    time.sleep(0.8)
                    os.system("pkill -9 Chrome")
                    ChromeProcessManager.log("✓ Tất cả Chrome process đã tắt")
                elif platform.system() == "Linux":
                    os.system("pkill chrome")
                    time.sleep(0.8)
                    os.system("pkill -9 chrome")
                    ChromeProcessManager.log("✓ Tất cả Chrome process đã tắt")
        except Exception as e:
            ChromeProcessManager.log(f"⚠️  Lỗi tắt Chrome: {e}")
    
    @staticmethod
    def kill_chrome():
        """Tắt Chrome process đang lưu"""
        if ChromeProcessManager._current_chrome_pid:
            # Kiểm tra xem process có còn sống không
            if ChromeProcessManager.is_process_alive(ChromeProcessManager._current_chrome_pid):
                ChromeProcessManager.kill_chrome_process(ChromeProcessManager._current_chrome_pid)
            else:
                ChromeProcessManager.log(f"ℹ️  Chrome process {ChromeProcessManager._current_chrome_pid} đã tắt rồi")
            
            ChromeProcessManager._current_chrome_pid = None
            ChromeProcessManager._chrome_profile_name = None
        else:
            ChromeProcessManager.log("ℹ️  Không có Chrome process nào đang lưu")

    @staticmethod
    def close_chrome_gracefully(timeout=5, stop_check=None):
        """Đóng Chrome nhẹ nhàng để giữ user data (fallback force kill)."""
        if not ChromeProcessManager._current_chrome_pid:
            ChromeProcessManager.log("ℹ️  Không có  process nào đang lưu")
            return

        pid = ChromeProcessManager._current_chrome_pid

        try:
            if platform.system() == "Windows":
                _run_silent(["taskkill", "/PID", str(pid), "/T"])
            else:
                os.system(f"kill {pid}")
        except Exception:
            pass

        # Chờ đóng
        waited = 0.0
        interval = 0.2
        while waited < timeout:
            try:
                if callable(stop_check) and stop_check():
                    ChromeProcessManager.log("🛑 STOP nhận được, Thoát luồng lấy token")
                    break
            except Exception:
                pass

            if not ChromeProcessManager.is_process_alive(pid):
                ChromeProcessManager.log(f" {pid} đã tắt")
                ChromeProcessManager._current_chrome_pid = None
                ChromeProcessManager._chrome_profile_name = None
                return
            time.sleep(interval)
            waited += interval

        # Fallback force kill
        ChromeProcessManager.kill_chrome_process(pid)
        ChromeProcessManager._current_chrome_pid = None
        ChromeProcessManager._chrome_profile_name = None
    
    @staticmethod
    def clean_chrome_cache(chrome_userdata_root, profile_name=None):
        """Xóa cache từ Chrome user data folder - giữ lại login data
        
        Args:
            chrome_userdata_root: Đường dẫn folder user data (1 tầng)
            profile_name: Tên profile (nếu có)
        """
        try:
            chrome_userdata_path = chrome_userdata_root
            if profile_name:
                chrome_userdata_path = str(Path(chrome_userdata_root) / profile_name)

            ChromeProcessManager.log(f"🔍 Checking cache path: {chrome_userdata_path}")
            
            # ✅ XÓA TẤT CẢ FOLDER CHỨA TỪ "CACHE"
            if not os.path.exists(chrome_userdata_path):
                ChromeProcessManager.log(f"⚠️  Path không tồn tại: {chrome_userdata_path}")
                return False
            
            deleted_count = 0
            items = os.listdir(chrome_userdata_path)
            ChromeProcessManager.log(f"📂 Found {len(items)} items in {chrome_userdata_path}")
            
            for item in items:
                # Kiểm tra nếu tên chứa "cache" (không phân biệt hoa thường)
                if "cache" in item.lower():
                    path = os.path.join(chrome_userdata_path, item)
                    if os.path.isdir(path):
                        try:
                            shutil.rmtree(path, ignore_errors=True)
                            ChromeProcessManager.log(f"  ✓ Xóa: {item}")
                            deleted_count += 1
                        except Exception as e:
                            ChromeProcessManager.log(f"  ⚠️  Lỗi xóa {item}: {e}")
                    elif os.path.isfile(path):
                        try:
                            os.remove(path)
                            ChromeProcessManager.log(f"  ✓ Xóa: {item}")
                            deleted_count += 1
                        except Exception as e:
                            ChromeProcessManager.log(f"  ⚠️  Lỗi xóa {item}: {e}")
            
            # ✅ Xóa các files không quan trọng (giữ lại login data)
            SAFE_DELETE_FILES = [
                "History",
                "History-journal",
                "Visited Links",
                "Top Sites",
            ]
            
            for file in SAFE_DELETE_FILES:
                path = os.path.join(chrome_userdata_path, file)
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        ChromeProcessManager.log(f"  ✓ Xóa: {file}")
                        deleted_count += 1
                    except Exception as e:
                        ChromeProcessManager.log(f"  ⚠️  Lỗi xóa {file}: {e}")
            
            ChromeProcessManager.log(f"✅ Xóa cache xong ({deleted_count} items), login data được giữ lại")
            return True
        except Exception as e:
            ChromeProcessManager.log(f"❌ Lỗi xóa cache: {e}")
            import traceback
            ChromeProcessManager.log(traceback.format_exc())
            return False
    
    @staticmethod
    def _migrate_legacy_profile_dir(chrome_userdata_root, profile_name):
        """Migrate legacy 1-tier profile data to 2-tier structure.

        Legacy layout: <root>/<profile>/Default/*
        New layout:    <root>/<profile>/*
        """
        try:
            if not chrome_userdata_root or not profile_name:
                return

            profile_dir = Path(chrome_userdata_root) / profile_name
            legacy_dir = profile_dir / "Default"

            if not legacy_dir.exists() or not legacy_dir.is_dir():
                return

            # Only migrate if new layout looks empty (no Cookies/Preferences)
            has_new_cookies = (profile_dir / "Cookies").exists()
            has_new_prefs = (profile_dir / "Preferences").exists()
            has_old_cookies = (legacy_dir / "Cookies").exists()
            has_old_prefs = (legacy_dir / "Preferences").exists()

            if (has_new_cookies or has_new_prefs) or not (has_old_cookies or has_old_prefs):
                return

            ChromeProcessManager.log(
                f"🔁 Migrate legacy profile data: {legacy_dir} -> {profile_dir}"
            )

            for item in legacy_dir.iterdir():
                target = profile_dir / item.name
                if target.exists():
                    continue
                try:
                    shutil.move(str(item), str(target))
                except Exception:
                    pass

            # Cleanup empty legacy folder
            try:
                if legacy_dir.exists() and not any(legacy_dir.iterdir()):
                    legacy_dir.rmdir()
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def open_chrome(chrome_userdata_root, debug_port=9222, headless=False, profile_name=None, restore_url="https://labs.google/fx/vi/tools/flow"):
        """
        Mở Chrome với user data folder
        
        Args:
            chrome_userdata_root: Đường dẫn folder user data (1 tầng)
            debug_port: Port cho debug (CDP)
            headless: Chạy ở chế độ headless
            profile_name: Tên profile Chrome (để lưu cho sau này)
        
        Returns:
            Chrome process
        """
        try:
            # ✅ Chỉ tắt Chrome do tool bật (theo PID). Không tắt Chrome khác.
            if ChromeProcessManager.is_chrome_running(debug_port):
                if (ChromeProcessManager._current_chrome_pid
                        and ChromeProcessManager.is_process_alive(ChromeProcessManager._current_chrome_pid)):
                    ChromeProcessManager.log("⚠️  Chrome do tool bật đang chạy, tắt để xóa lịch sử...")
                    ChromeProcessManager.kill_chrome()
                    time.sleep(1)
                else:
                    ChromeProcessManager.log(
                        "⚠️  Debug port đang được dùng bởi Chrome khác. Không tắt Chrome khác."
                    )
                    return None

            # ✅ Xóa user-data tầng 2 + tạo lại (nếu có profile)
            # (Tạm thời comment để không xóa sạch profile)
            # if profile_name:
            #     try:
            #         from settings_manager import SettingsManager
            #         SettingsManager.delete_profile_files(profile_name)
            #         ChromeProcessManager.log("⏳ Đợi xóa user-data tầng 2 xong (2s)...")
            #         time.sleep(2)
            #         SettingsManager.create_chrome_userdata_folder(profile_name)
            #     except Exception as e:
            #         ChromeProcessManager.log(f"⚠️  Lỗi reset user-data tầng 2 ({profile_name}): {e}")

            # Tìm Chrome
            chrome_path = ChromeProcessManager.find_chrome_path()
            if not chrome_path:
                ChromeProcessManager.log("❌ Không tìm thấy Chrome trên hệ thống!")
                return None
            
            # Xây dựng command (1 tầng: user data dir duy nhất)
            cmd = [chrome_path, f"--user-data-dir={chrome_userdata_root}"]
            
            if debug_port:
                cmd.append(f"--remote-debugging-port={debug_port}")
            
            # 🎯 Không hiện First Run UI
            cmd.append("--no-first-run")
            cmd.append("--no-default-browser-check")
            cmd.append("--lang=en-US")
            
            # 🎯 Set kích thước cố định nhỏ hơn (1200x800)
            cmd.append("--window-size=1200,800")
            
            if not headless:
                cmd.append("https://labs.google/fx/vi/tools/flow")
            else:
                cmd.append("--headless=new")
            
            ChromeProcessManager.log(f"🚀 Khởi động Chrome (debug_port={debug_port}, headless={headless}, profile={profile_name})...")
            
            try:
                popen_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                }

                process = subprocess.Popen(
                    cmd,
                    **popen_kwargs,
                )
            except Exception as popen_err:
                ChromeProcessManager.log(f"❌ Lỗi Popen: {popen_err}")
                return None
            
            # ✅ CHECK NGAY SAU KHI START: Chrome có chạy được không?
            time.sleep(1)
            if process.poll() is not None:
                # Process đã exit
                stdout, stderr = process.communicate()
                error_msg = stderr.decode('utf-8', errors='ignore') if stderr else "Unknown error"
                ChromeProcessManager.log(f"❌ Chrome exit ngay sau khi start! Error:\n{error_msg[:500]}")
                return None
            
            # Lưu PID + tên profile
            ChromeProcessManager._current_chrome_pid = process.pid
            ChromeProcessManager._chrome_profile_name = profile_name or "single"
            
            ChromeProcessManager.log(f"✓ Chrome khởi động thành công (PID: {process.pid}, Profile: {ChromeProcessManager._chrome_profile_name})")
            
            return process
        
        except Exception as e:
            ChromeProcessManager.log(f"❌ Lỗi khởi động Chrome: {e}")
            return None
    
    @staticmethod
    def open_url_in_running_chrome(url, debug_port=9222):
        """Mở URL trong Chrome đang chạy (không tắt Chrome)
        
        Args:
            url: URL cần mở
            debug_port: Debug port của Chrome đang chạy
        """
        try:
            # Kiểm tra xem Chrome có đang chạy không
            if not ChromeProcessManager.is_chrome_running(debug_port):
                ChromeProcessManager.log(f"❌ Chrome không đang chạy trên port {debug_port}")
                return False
            
            # Sử dụng Playwright để mở URL trong tab hiện tại
            import asyncio
            import threading
            from playwright.async_api import async_playwright
            
            async def open_url():
                try:
                    async with async_playwright() as p:
                        # Kết nối tới Chrome đang chạy qua CDP
                        browser = await p.chromium.connect_over_cdp(f"http://localhost:{debug_port}")
                        
                        # Lấy tất cả pages (tabs) đang mở
                        pages = browser.contexts[0].pages if browser.contexts else []
                        
                        if pages:
                            # Nếu có tab, navigate tab đầu tiên (hiện tại)
                            page = pages[0]
                            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            ChromeProcessManager.log(f"✓ Mở URL trong tab hiện tại: {url}")
                        else:
                            # Nếu không có tab, tạo tab mới
                            context = browser.contexts[0] if browser.contexts else await browser.new_context()
                            page = await context.new_page()
                            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                            ChromeProcessManager.log(f"✓ Mở URL trong tab mới: {url}")
                        
                        return True
                except Exception as e:
                    ChromeProcessManager.log(f"⚠️  Lỗi mở URL: {e}")
                    return False
            
            def _run_async_in_new_loop():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(open_url())
                finally:
                    loop.close()

            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop and running_loop.is_running():
                result_holder = {}

                def _runner():
                    result_holder["result"] = _run_async_in_new_loop()

                worker = threading.Thread(target=_runner, daemon=True)
                worker.start()
                worker.join()
                return result_holder.get("result", False)

            return _run_async_in_new_loop()
        
        except Exception as e:
            ChromeProcessManager.log(f"❌ Lỗi: {e}")
            return False

    @staticmethod
    
    @staticmethod
    def open_chrome_with_url(chrome_userdata_root, url, debug_port=9222, profile_name=None, headless=False, hide_window=False, extra_args=None):
        """Mở Chrome + mở URL
        
        Nếu Chrome đã chạy, mở URL trong instance đó.
        Nếu chưa, tạo Chrome mới với URL.
        
        Args:
            chrome_userdata_root: Đường dẫn folder user data (root)
            url: URL cần mở
            debug_port: Debug port
            profile_name: Tên profile Chrome
            headless: Chạy Chrome ẩn (headless)
            hide_window: Đưa window ra ngoài màn hình (chỉ áp dụng khi headless=False)
        """
        try:
            # ✅ Nếu Chrome đang chạy, chỉ mở URL trong instance đó
            if ChromeProcessManager.is_chrome_running(debug_port):
                opened = ChromeProcessManager.open_url_in_running_chrome(url, debug_port=debug_port)
                if opened:
                    ChromeProcessManager.log("✅ Mở URL trong Chrome đang chạy")
                    return True

            # ✅ Xóa user-data tầng 2 + tạo lại (nếu có profile)
            # (Tạm thời comment để không xóa sạch profile)
            # if profile_name:
            #     try:
            #         from settings_manager import SettingsManager
            #         SettingsManager.delete_profile_files(profile_name)
            #         ChromeProcessManager.log("⏳ Đợi xóa user-data tầng 2 xong (2s)...")
            #         time.sleep(2)
            #         SettingsManager.create_chrome_userdata_folder(profile_name)
            #     except Exception as e:
            #         ChromeProcessManager.log(f"⚠️  Lỗi reset user-data tầng 2 ({profile_name}): {e}")
            
            # Nếu chưa chạy, tạo Chrome mới
            chrome_path = ChromeProcessManager.find_chrome_path()
            if not chrome_path:
                ChromeProcessManager.log("❌ Không tìm thấy Chrome!")
                return None
            
            cmd = [
                chrome_path,
                f"--user-data-dir={chrome_userdata_root}",
                f"--remote-debugging-port={debug_port}",
            ]
            
            extra_args = extra_args or []
            has_window_size = any(str(arg).startswith("--window-size") for arg in extra_args)

            if headless:
                # ✅ Flags cần thiết cho headless mode (đặc biệt trên Windows)
                cmd.append("--headless=new")
                cmd.append("--no-sandbox")
                cmd.append("--disable-gpu")
                cmd.append("--disable-dev-shm-usage")
                cmd.append("--disable-sync")
                cmd.append("--disable-default-apps")
                cmd.append("--window-size=1200,800")
            elif hide_window:
                # ✅ Non-headless nhưng ẩn window (đưa ra ngoài màn hình)
                cmd.append("--window-position=-32000,-32000")
                cmd.append("--disable-infobars")
                cmd.append("--disable-blink-features=AutomationControlled")
            else:
                # ✅ Non-headless, không ẩn - đặt kích thước cố định nếu không có window-size custom
                if not has_window_size:
                    cmd.append("--window-size=1100,700")
            
            cmd.append("--lang=en-US")
            cmd.append("--disable-extensions")
            cmd.append("--disable-plugins")
            cmd.extend(extra_args)
            cmd.append(url)
            
            
            try:
                popen_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                }

                process = subprocess.Popen(
                    cmd,
                    **popen_kwargs,
                )
            except Exception as popen_err:
                ChromeProcessManager.log(f"❌ Lỗi Popen: {popen_err}")
                return None
            
            # ✅ CHECK NGAY SAU KHI START: Chrome có chạy được không?
            time.sleep(1)
            if process.poll() is not None:
                # Process đã exit
                stdout, stderr = process.communicate()
                error_msg = stderr.decode('utf-8', errors='ignore') if stderr else "Unknown error"
                ChromeProcessManager.log(f"❌ Chrome exit ngay sau khi start! Error:\n{error_msg[:500]}")
                return None
            
            # Lưu PID + tên profile
            ChromeProcessManager._current_chrome_pid = process.pid
            ChromeProcessManager._chrome_profile_name = profile_name or "single"
            
            return process
        
        except Exception as e:
            ChromeProcessManager.log(f"❌ Lỗi: {e}")
            return None
    
    @staticmethod
    def reset_user_data(profile_name, settings_manager=None):
        """Reset user data cho profile (xóa + tạo mới + open chrome + auto login VEO3)
        
        Chức năng:
        1. Kiểm tra Chrome bật bởi tool → tắt đi
        2. Xóa folder userdata cũ
        3. Tạo lại folder userdata
        4. Mở Chrome với folder mới
        5. Auto login VEO3 với tk/mk từ list_profile.json (veo3_accounts)
        
        Args:
            profile_name: Tên profile (VD: "new")
            settings_manager: SettingsManager instance (dùng để lấy userdata path)
        """
        try:
            # Import SettingsManager nếu không được truyền vào
            if settings_manager is None:
                from settings_manager import SettingsManager
                settings_manager = SettingsManager
            
            # ✅ BƯỚC 1: KIỂM TRA CHROME VÀ TẮT
            
            # Lấy PID từ ChromeProcessManager
            if ChromeProcessManager._current_chrome_pid:
                ChromeProcessManager.kill_chrome()
                time.sleep(1)  # Đợi chrome close

            
            # ✅ BƯỚC 2: XÓA FOLDER USERDATA CŨ
            profile_dir = Path(settings_manager.get_chrome_profile_dir(profile_name))
            
            if profile_dir.exists():
                shutil.rmtree(profile_dir)

            # ✅ BƯỚC 3: TẠO LẠI FOLDER
            profile_dir.mkdir(parents=True, exist_ok=True)
            
            # ✅ BƯỚC 4: MỞ CHROME VỚI FOLDER MỚI
            ChromeProcessManager.log(f"🌐 Mở Chrome với user data: {profile_dir}...")
            ChromeProcessManager.open_chrome(str(profile_dir), debug_port=9222, profile_name=None)
            
            # ✅ BƯỚC 5: AUTO LOGIN VEO3
            
            # Load VEO3 accounts từ list_profile.json
            try:
                # Tìm list_profile.json từ thư mục TUAN_ANH
                profile_file = DATA_GENERAL_DIR / "list_profile.json"
                with open(profile_file, 'r', encoding='utf-8') as f:
                    profile_data = json.load(f)
                
                veo3_accounts = profile_data.get('veo3_accounts', [])
                
                if veo3_accounts:
                    # Lấy account đầu tiên (nếu có nhiều)
                    first_account = veo3_accounts[0]
                    
                    # Parse user|pass với xử lý khoảng trắng
                    parts = first_account.split('|')
                    if len(parts) >= 2:
                        username = parts[0].strip()
                        password = parts[1].strip()
                        
                        ChromeProcessManager.log(f"📊 Tài khoản: {username}")
                        ChromeProcessManager.log(f"⏳ Đang login VEO3...")
                        
                        # Gọi hàm auto login
                        from login_guide import LoginGuideDialog
                        login_dialog = LoginGuideDialog(profile_name)
                        
                        # Chạy hàm async login_veo3_auto
                        import asyncio
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            result = loop.run_until_complete(
                                login_dialog.login_veo3_auto(username, password, profile_name)
                            )
                            success = bool(result.get("success")) if isinstance(result, dict) else False
                            already_logged_in = bool(result.get("already_logged_in")) if isinstance(result, dict) else False
                            
                            if success:
                                if already_logged_in:
                                    ChromeProcessManager.log(f"✅ VEO3 đã login sẵn")
                                else:
                                    ChromeProcessManager.log(f"✅ Auto-login VEO3 thành công!")
                            else:
                                ChromeProcessManager.log(f"❌ Auto-login VEO3 lỗi!")
                        finally:
                            loop.close()
                        
                    else:
                        ChromeProcessManager.log(f"❌ Format tài khoản sai: {first_account}")
                else:
                    ChromeProcessManager.log(f"⚠️  Không tìm tài khoản VEO3 trong list_profile.json")
                    
            except Exception as e:
                ChromeProcessManager.log(f"⚠️  Lỗi load VEO3 accounts: {e}")
                import traceback
                ChromeProcessManager.log(f"📋 {traceback.format_exc()[:300]}")
            
        except Exception as e:
            ChromeProcessManager.log(f"❌ Lỗi reset user data: {e}")
            import traceback
            ChromeProcessManager.log(f"⚠️ {traceback.format_exc()[:200]}")
