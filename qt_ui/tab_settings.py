from __future__ import annotations
import os
import shutil
import threading
import json
from datetime import datetime
import pathlib
from pathlib import Path
import urllib.request
from urllib.request import urlopen
import urllib.error
from urllib.error import URLError
from PyQt6.QtCore import QTimer, Qt, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (QWidget, QFormLayout, QLineEdit, QHBoxLayout, QLabel, 
                             QPushButton, QComboBox, QMessageBox, QGroupBox, QVBoxLayout, 
                             QPlainTextEdit, QSizePolicy, QDialog, QTextEdit)
import chrome
from chrome import kill_profile_chrome, open_profile_chrome, resolve_profile_dir
import settings_manager
from settings_manager import SettingsManager, BASE_DIR
import login
from login import auto_login_veo3

class _AutoLoginWorker(QObject):
    log = pyqtSignal(str)
    result = pyqtSignal(dict)
    finished = pyqtSignal()

    def __init__(self, username: str, password: str, profile_name: str):
        super().__init__()
        self._username = str(username).strip() if username else ''
        self._password = str(password) if password else ''
        self._profile_name = str(profile_name).strip() if profile_name else 'PROFILE_1'
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            result = auto_login_veo3(
                self._username,
                self._password,
                profile_name=self._profile_name,
                logger=self._emit_log,
                stop_check=self._stop_event.is_set
            )
            
            if isinstance(result, dict):
                pass
            else:
                result = {'success': False, 'message': 'Kết quả auto login không hợp lệ.'}
                
            if self._stop_event.is_set() and not bool(result.get('stopped')):
                result = {'success': False, 'stopped': True, 'message': 'Đã dừng auto login.'}
                
            self.result.emit(result)
            self.finished.emit()
        except Exception as exc:
            self.result.emit({'success': False, 'message': f'Lỗi auto login: {exc}'})
            self.finished.emit()

    def _emit_log(self, message: str) -> None:
        self.log.emit(str(message) if message else '')

class SettingsTab(QWidget):
    REQUIRED_PROJECT_URL_PREFIX = 'https://labs.google/fx/vi/tools/flow/project/'

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config
        self.setObjectName('SettingsTab')
        self.setStyleSheet("""
            QWidget#SettingsTab QComboBox#SettingsCombo {
                font-size: 12px;
                min-height: 30px;
                padding: 4px 8px;
            }
            QWidget#SettingsTab QComboBox#SettingsCombo QAbstractItemView {
                font-size: 12px;
                outline: none;
            }
            QWidget#SettingsTab QComboBox#SettingsCombo QAbstractItemView::item {
                min-height: 30px;
                padding: 4px 8px;
            }
            """)
        
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(14)
        
        left_box = QGroupBox('App Settings')
        left_box.setStyleSheet('QGroupBox{font-weight:800;}')
        left = QVBoxLayout(left_box)
        left.setContentsMargins(10, 10, 10, 10)
        left.setSpacing(10)
        
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        
        def int_edit(min_v: int, max_v: int, val: int, width: int = 70) -> QLineEdit:
            e = QLineEdit(str(int(val)))
            e.setValidator(QIntValidator(int(min_v), int(max_v), e))
            e.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            e.setFixedWidth(int(width))
            e.setFixedHeight(34)
            return e
            
        def combo(items: list[str], cur: str) -> QComboBox:
            c = QComboBox()
            c.setObjectName('SettingsCombo')
            c.addItems(items)
            c.setCurrentText(str(cur))
            c.setFixedWidth(90)
            c.setFixedHeight(34)
            return c
            
        output_cur = 1
        if getattr(config, 'output_count', 1):
            output_cur = 1
        if output_cur < 1: output_cur = 1
        if output_cur > 4: output_cur = 4
        
        self.output_count = combo(['1', '2', '3', '4'], str(output_cur))
        form.addRow('Số đầu ra:', self.output_count)
        
        self.multi_video = int_edit(1, 20, int(getattr(config, 'multi_video', 3)) or 3)
        form.addRow('MULTI_VIDEO:', self.multi_video)
        
        self.wait_gen_video = int_edit(0, 999, int(getattr(config, 'wait_gen_video', 15)) or 15)
        form.addRow('WAIT_GEN_VIDEO:', self.wait_gen_video)
        
        self.wait_gen_image = int_edit(0, 999, int(getattr(config, 'wait_gen_image', 15)) or 15)
        form.addRow('WAIT_GEN_IMAGE:', self.wait_gen_image)
        
        self.retry_with_error = int_edit(0, 99, int(getattr(config, 'retry_with_error', 3)) or 3)
        form.addRow('RETRY_WITH_ERROR:', self.retry_with_error)
        
        self.CLEAR_DATA_IMAGE = int_edit(0, 999, int(getattr(config, 'CLEAR_DATA_IMAGE', 11)) or 11)
        form.addRow('CLEAR_DATA_IMAGE:', self.CLEAR_DATA_IMAGE)
        
        self.clear_data = int_edit(0, 999, int(getattr(config, 'clear_data', 5)) or 5)
        form.addRow('CLEAR_DATA:', self.clear_data)
        
        self.clear_data_wait = int_edit(0, 999, int(getattr(config, 'clear_data_wait', 4)) or 4)
        form.addRow('CLEAR_DATA_WAIT:', self.clear_data_wait)
        
        self.wait_resend_video = int_edit(0, 999, int(getattr(config, 'wait_resend_video', 10)) or 10)
        form.addRow('WAIT_RESEND_VIDEO:', self.wait_resend_video)
        
        self.download_mode = combo(['720', '1080', '2K', '4K'], str(getattr(config, 'download_mode', '720')) or '720')
        form.addRow('Download Mode:', self.download_mode)
        
        token_cur = str(getattr(config, 'token_option', 'Option2')) or 'Option2'
        token_cur = token_cur.replace(' ', '')
        if token_cur not in {'Option1', 'Option2'}:
            token_cur = 'Option2'
        self.token_option = combo(['Option2', 'Option1'], token_cur)
        form.addRow('Token Option:', self.token_option)
        
        self.seed_mode = combo(['Random', 'Fixed'], str(getattr(config, 'seed_mode', 'Random')) or 'Random')
        form.addRow('Seed Mode:', self.seed_mode)
        
        self.seed_value = int_edit(0, 999999, int(getattr(config, 'seed_value', 9797)) or 9797)
        form.addRow('Seed Value:', self.seed_value)
        
        left.addLayout(form)
        left.addStretch(1)
        
        right_box = QGroupBox('Tài khoản VEO3')
        right_box.setStyleSheet('QGroupBox{font-weight:800;}')
        right = QVBoxLayout(right_box)
        right.setContentsMargins(10, 10, 10, 10)
        right.setSpacing(10)
        
        acct_form = QFormLayout()
        acct_form.setHorizontalSpacing(10)
        acct_form.setVerticalSpacing(8)
        
        self.veo3_user = QLineEdit(str(getattr(config, 'veo3_user', '') or getattr(config, 'USER', '') or ''))
        self.veo3_user.setFixedHeight(34)
        acct_form.addRow('TK:', self.veo3_user)
        
        pw_row = QHBoxLayout()
        self.veo3_pass = QLineEdit(str(getattr(config, 'veo3_pass', '') or getattr(config, 'PASS', '') or ''))
        self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.veo3_pass.setFixedHeight(34)
        
        self._pw_pinned_visible = False
        self._pw_hide_timer = QTimer(self)
        self._pw_hide_timer.setSingleShot(True)
        self._pw_hide_timer.timeout.connect(self._auto_hide_pw)
        self.veo3_pass.textEdited.connect(self._on_pw_edited)
        
        self.btn_eye = QPushButton('👁')
        self.btn_eye.setFixedSize(34, 34)
        self.btn_eye.setStyleSheet('font-size:13px;')
        self.btn_eye.clicked.connect(self._toggle_pw)
        
        pw_row.addWidget(self.veo3_pass, 1)
        pw_row.addWidget(self.btn_eye, 0)
        acct_form.addRow('MK:', pw_row)
        
        right.addLayout(acct_form)
        
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        
        self.btn_open_profile = QPushButton('Mở Profile')
        self.btn_open_profile.setObjectName('Warning')
        self.btn_open_profile.setFixedHeight(36)
        self.btn_open_profile.clicked.connect(self._open_profile)
        
        self.btn_delete_profile = QPushButton('Xóa Profile')
        self.btn_delete_profile.setObjectName('Danger')
        self.btn_delete_profile.setFixedHeight(36)
        self.btn_delete_profile.clicked.connect(self._delete_profile)
        
        btn_row.addWidget(self.btn_open_profile)
        btn_row.addWidget(self.btn_delete_profile)
        right.addLayout(btn_row)
        
        keys_title = QLabel('Gemini API Keys (mỗi dòng 1 key):\nAPI key chỉ dùng cho tính năng tạo video từ Ý Tưởng. Nếu không dùng tính năng này có thể bỏ qua API KEY.')
        keys_title.setStyleSheet('QLabel{font-weight:800;}')
        keys_title.setWordWrap(True)
        right.addWidget(keys_title)
        
        self.gemini_api_keys = QPlainTextEdit()
        self.gemini_api_keys.setPlainText(str(getattr(config, 'gemini_api_keys', '') or ''))
        self.gemini_api_keys.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.gemini_api_keys.setFixedHeight(150)
        right.addWidget(self.gemini_api_keys)
        
        self.btn_auto_login = QPushButton('AUTO Login TK Veo3')
        self.btn_auto_login.setObjectName('Orange')
        self.btn_auto_login.setFixedHeight(38)
        self.btn_auto_login.setFixedWidth(180)
        self.btn_auto_login.clicked.connect(self._auto_login_veo3)
        right.addWidget(self.btn_auto_login, 0, Qt.AlignmentFlag.AlignHCenter)
        
        self.btn_save = QPushButton('Lưu cài đặt')
        self.btn_save.setObjectName('Accent')
        self.btn_save.setFixedHeight(36)
        self.btn_save.setFixedWidth(180)
        self.btn_save.clicked.connect(self._save)
        right.addWidget(self.btn_save, 0, Qt.AlignmentFlag.AlignHCenter)
        
        right.addStretch(1)
        
        self._auto_login_thread = None
        self._auto_login_worker = None
        self._auto_login_popup = None
        self._auto_login_log = None
        self._auto_login_btn_close = None
        self._auto_login_stopped_by_user = False
        
        self._profile_popup = None
        self._profile_popup_status = None
        self._last_profile_dir = ''
        self._last_profile_cdp_host = '127.0.0.1'
        self._last_profile_cdp_port = 0
        
        root.addWidget(left_box, 2)
        root.addWidget(right_box, 3)

    def _toggle_pw(self) -> None:
        self._pw_pinned_visible = not self._pw_pinned_visible
        if self._pw_pinned_visible:
            self._pw_hide_timer.stop()
            self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Normal)
        else:
            self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)

    def _on_pw_edited(self, _text: str) -> None:
        if self._pw_pinned_visible:
            return
        self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Normal)
        self._pw_hide_timer.start(900)

    def _auto_hide_pw(self) -> None:
        if self._pw_pinned_visible:
            return
        self.veo3_pass.setEchoMode(QLineEdit.EchoMode.Password)

    def _save(self) -> None:
        def _as_int(e: QLineEdit, default: int = 0) -> int:
            if e.text():
                t = e.text().strip()
                try:
                    return int(t)
                except Exception:
                    return int(default)
            return int(default)

        try:
            setattr(self._cfg, 'multi_video', _as_int(self.multi_video, 1))
            
            output_cur = 1
            if self.output_count.currentText().strip():
                output_cur = int(self.output_count.currentText().strip())
            setattr(self._cfg, 'output_count', output_cur)
            
            setattr(self._cfg, 'wait_gen_video', _as_int(self.wait_gen_video, 15))
            setattr(self._cfg, 'wait_gen_image', _as_int(self.wait_gen_image, 15))
            setattr(self._cfg, 'retry_with_error', _as_int(self.retry_with_error, 3))
            setattr(self._cfg, 'CLEAR_DATA_IMAGE', _as_int(self.CLEAR_DATA_IMAGE, 11))
            setattr(self._cfg, 'clear_data', _as_int(self.clear_data, 5))
            setattr(self._cfg, 'clear_data_wait', _as_int(self.clear_data_wait, 4))
            setattr(self._cfg, 'wait_resend_video', _as_int(self.wait_resend_video, 10))
            
            setattr(self._cfg, 'download_mode', self.download_mode.currentText().strip() or '720')
            setattr(self._cfg, 'token_option', self.token_option.currentText().strip() or 'Option2')
            setattr(self._cfg, 'seed_mode', self.seed_mode.currentText().strip() or 'Random')
            setattr(self._cfg, 'seed_value', _as_int(self.seed_value, 9797))
            
            setattr(self._cfg, 'veo3_user', self.veo3_user.text().strip())
            setattr(self._cfg, 'veo3_pass', self.veo3_pass.text())
            setattr(self._cfg, 'gemini_api_keys', self.gemini_api_keys.toPlainText().strip())
            
            self._cfg.save()
            QMessageBox.information(self, 'Thông báo', 'Cấu hình đã được lưu.')
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không lưu được cấu hình: {exc}')

    def _profile_dir(self) -> Path:
        profile_name = self._current_profile_name()
        import chrome
        resolve_profile_dir = chrome.resolve_profile_dir
        return resolve_profile_dir(profile_name)

    def _current_profile_name(self) -> str:
        try:
            import settings_manager
            SettingsManager = settings_manager.SettingsManager
            settings = SettingsManager.load_settings()
            if isinstance(settings, dict):
                cur = str(settings.get('current_profile', '')).strip()
                if cur:
                    return cur
        except Exception:
            pass
        
        if os.getenv('PROFILE_NAME', 'PROFILE_1').strip():
            return os.getenv('PROFILE_NAME', 'PROFILE_1').strip()
        return 'PROFILE_1'

    def _open_profile(self) -> None:
        try:
            p = self._profile_dir()
            p.mkdir(parents=True, exist_ok=True)
            
            import chrome
            open_profile_chrome = chrome.open_profile_chrome
            import settings_manager
            SettingsManager = settings_manager.SettingsManager
            
            profile_name = self._current_profile_name()
            opened = open_profile_chrome(profile_name=profile_name, url='https://labs.google/fx/vi/tools/flow')
            
            host = os.getenv('CDP_HOST', '127.0.0.1')
            port = int(opened.get('port') or 0)
            pp = str(opened.get('profile_dir') or p)
            
            self._last_profile_dir = pp
            self._last_profile_cdp_host = host
            self._last_profile_cdp_port = port
            
            cfg = SettingsManager.load_config()
            if not isinstance(cfg, dict):
                cfg = {}
            
            if not isinstance(cfg.get('account1'), dict):
                account = {}
            else:
                account = cfg.get('account1')
                
            if not account:
                account = {}
                
            account['folder_user_data_get_token'] = pp
            cfg['account1'] = account
            SettingsManager.save_config(cfg)
            
            self._show_profile_popup(pp, host, port)
            
        except Exception as exc:
            if os.name == 'nt':
                try:
                    os.startfile(str(p))
                    return
                except Exception:
                    pass
            QMessageBox.information(self, 'Thông báo', f'Profile path: {p}')
            return
            
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không mở được profile: {exc}')

    def _show_profile_popup(self, profile_dir: str, host: str, port: int) -> None:
        if self._profile_popup is not None:
            self._profile_popup.close()
            
        popup = QDialog(self)
        popup.setWindowTitle('Profile Token')
        popup.setMinimumWidth(560)
        popup.setModal(False)
        
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        
        title = QLabel('Đã mở profile thành công')
        title.setStyleSheet('font-size:16px; font-weight:800; color:#1f2d48;')
        layout.addWidget(title)
        
        info = QLabel()
        info.setWordWrap(True)
        info.setStyleSheet('color:#334155; font-size:12px;')
        layout.addWidget(info)
        
        status = QLabel('')
        status.setWordWrap(True)
        status.setStyleSheet('color:#0f172a; font-size:12px; background:#eef5ff; border:1px solid #c8d7f2; border-radius:8px; padding:8px;')
        layout.addWidget(status)
        
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch(1)
        
        btn_save_token = QPushButton('Lưu Profile TOKEN')
        btn_save_token.setObjectName('Accent')
        btn_save_token.setFixedHeight(36)
        btn_save_token.clicked.connect(self._save_profile_token)
        btn_row.addWidget(btn_save_token)
        
        btn_close = QPushButton('Đóng')
        btn_close.setObjectName('Warning')
        btn_close.setFixedHeight(36)
        btn_close.clicked.connect(self._close_profile_popup_and_chrome)
        btn_row.addWidget(btn_close)
        
        layout.addLayout(btn_row)
        
        self._profile_popup = popup
        self._profile_popup_status = status
        
        setattr(self._profile_popup, '_profile_popup_info', info)
        
        info = getattr(self._profile_popup, '_profile_popup_info', None)
        if isinstance(info, QLabel):
            info.setText(f"Chrome profile đang mở.\nCDP: {host}:{port}\nProfile: {profile_dir}\n\nSau khi đăng nhập và tạo dự án mới, bấm 'Lưu Profile TOKEN' để lưu link project.")
            
        if isinstance(self._profile_popup_status, QLabel):
            self._profile_popup_status.setText('Trạng thái: Chưa lưu URL_GEN_TOKEN')
            
        self._profile_popup.show()
        self._profile_popup.raise_()
        self._profile_popup.activateWindow()

    def _fetch_project_url_from_cdp(self, host: str, port: int) -> str:
        if not host or int(port) <= 0:
            return ''
            
        try:
            url = f'http://{host}:{int(port)}/json/list'
            with urlopen(url, timeout=2) as response:
                raw = response.read()
                if not raw:
                    raw = b'[]'
                pages = json.loads(raw.decode('utf-8', 'ignore'))
                
            if not isinstance(pages, list):
                return ''
                
            for item in pages:
                if isinstance(item, dict):
                    url = str(item.get('url') or '').strip()
                    if url.startswith(self.REQUIRED_PROJECT_URL_PREFIX):
                        return url
            return ''
        except (URLError, OSError, ValueError):
            return ''
        except Exception:
            return ''

    def _close_profile_chrome(self) -> None:
        try:
            if self._last_profile_dir:
                profile_dir = self._profile_dir()
                if profile_dir:
                    import chrome
                    kill_profile_chrome = chrome.kill_profile_chrome
                    kill_profile_chrome(profile_dir)
        except Exception:
            pass

    def _close_profile_popup_and_chrome(self) -> None:
        if self._profile_popup:
            self._profile_popup.close()

    def _save_profile_token(self) -> None:
        try:
            profile_dir = self._profile_dir()
            if self._last_profile_cdp_host and self._last_profile_cdp_host == '127.0.0.1' and self._last_profile_cdp_port > 0:
                host = '127.0.0.1'
                port = self._last_profile_cdp_port
                project_url = self._fetch_project_url_from_cdp(host, port)
                
                if not project_url:
                    message = 'Bạn cần đăng nhập tải khoản và tạo 1 dự án mới.'
                    if isinstance(self._profile_popup_status, QLabel):
                        self._profile_popup_status.setText(f'Trạng thái: {message}')
                    QMessageBox.warning(self, 'Chưa có link project', message)
                    return
                    
                if self.REQUIRED_PROJECT_URL_PREFIX not in project_url:
                    message = "Link chưa hợp lệ. URL phải chứa 'project'."
                    if isinstance(self._profile_popup_status, QLabel):
                        self._profile_popup_status.setText(f'Trạng thái: {message}')
                    QMessageBox.warning(self, 'Link chưa hợp lệ', message)
                    return
                    
                import settings_manager
                SettingsManager = settings_manager.SettingsManager
                cfg = SettingsManager.load_config()
                if not isinstance(cfg, dict):
                    cfg = {}
                    
                if not isinstance(cfg.get('account1'), dict):
                    account = {}
                else:
                    account = cfg.get('account1')
                    
                if not account:
                    account = {}
                    
                account['URL_GEN_TOKEN'] = project_url
                account['folder_user_data_get_token'] = str(profile_dir)
                cfg['account1'] = account
                SettingsManager.save_config(cfg)
                
                if isinstance(self._profile_popup_status, QLabel):
                    self._profile_popup_status.setText(f'Trạng thái: Đã lưu URL_GEN_TOKEN\n{project_url}')
                    
                QMessageBox.information(self, 'Lưu thành công', 'Đã lưu URL_GEN_TOKEN từ profile hiện tại. Chrome vẫn giữ mở để tránh mất dữ liệu profile.')
                
                if self._profile_popup:
                    self._profile_popup.close()
                    
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không thể lưu profile token: {exc}')

    def _auto_login_veo3(self) -> None:
        if QMessageBox.question(self, 'Xác nhận', 'Bạn có chắc muốn AUTO Login TK Veo3?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
            
        if self._auto_login_thread is not None:
            QMessageBox.information(self, 'Thông báo', 'Auto login đang chạy, vui lòng chờ hoặc bấm Dừng.')
            return
            
        user = self.veo3_user.text().strip()
        pwd = self.veo3_pass.text()
        
        if not user or not pwd:
            QMessageBox.warning(self, 'Lỗi', 'Vui lòng nhập đủ tài khoản và mật khẩu trước khi AUTO Login.')
            return
            
        profile_name = self._current_profile_name()
        setattr(self._cfg, 'veo3_user', user)
        setattr(self._cfg, 'veo3_pass', pwd)
        self._cfg.save()
        
        self._auto_login_stopped_by_user = False
        self._show_auto_login_popup(profile_name)
        self._append_auto_login_log('⏳ Bắt đầu auto login...')
        self._set_auto_login_button_busy(True)
        
        thread = QThread(self)
        worker = _AutoLoginWorker(user, pwd, profile_name)
        worker.moveToThread(thread)
        
        thread.started.connect(worker.run)
        worker.log.connect(self._append_auto_login_log)
        worker.result.connect(self._on_auto_login_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_auto_login_finished)
        
        self._auto_login_thread = thread
        self._auto_login_worker = worker
        thread.start()

    def _set_auto_login_button_busy(self, busy: bool) -> None:
        if busy:
            self.btn_auto_login.setEnabled(False)
            self.btn_auto_login.setText('⏳ Đang Auto Login...')
        else:
            self.btn_auto_login.setEnabled(True)
            self.btn_auto_login.setText('AUTO Login TK Veo3')

    def _show_auto_login_popup(self, profile_name: str) -> None:
        if self._auto_login_popup is not None:
            self._auto_login_popup.setWindowTitle('Auto Login VEO3')
            if self._auto_login_log:
                self._auto_login_log.clear()
            self._auto_login_popup.show()
            self._auto_login_popup.raise_()
            self._auto_login_popup.activateWindow()
            return
            
        dlg = QDialog(self)
        dlg.setWindowTitle('Auto Login VEO3')
        dlg.setMinimumSize(640, 420)
        
        layout = QVBoxLayout(dlg)
        
        title = QLabel(f'Đang auto login profile: {profile_name}')
        title.setStyleSheet('font-weight: 700;')
        layout.addWidget(title)
        
        log_view = QTextEdit()
        log_view.setReadOnly(True)
        log_view.setStyleSheet('background:#1e1e1e; color:#dcdcdc; border:1px solid #333;')
        layout.addWidget(log_view, 1)
        
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        
        btn_close = QPushButton('Đóng')
        btn_close.clicked.connect(dlg.close)
        btn_row.addWidget(btn_close)
        
        btn_stop = QPushButton('Dừng')
        btn_stop.setObjectName('Danger')
        btn_stop.clicked.connect(self._request_stop_auto_login)
        btn_row.addWidget(btn_stop)
        
        layout.addLayout(btn_row)
        
        self._auto_login_popup = dlg
        self._auto_login_log = log_view
        self._auto_login_btn_close = btn_close
        
        self._auto_login_popup.show()
        self._auto_login_popup.raise_()
        self._auto_login_popup.activateWindow()

    def _append_auto_login_log(self, message: str) -> None:
        if self._auto_login_log is None:
            return
        ts = datetime.now().strftime('%H:%M:%S')
        self._auto_login_log.append(f'[{ts}] {str(message) if message else ""}')

    def _request_stop_auto_login(self) -> None:
        self._auto_login_stopped_by_user = True
        self._append_auto_login_log('🛑 Đang dừng auto login...')
        self._set_auto_login_button_busy(False)
        
        if self._auto_login_worker:
            self._auto_login_worker.stop()
            
        try:
            import chrome
            kill_profile_chrome = chrome.kill_profile_chrome
            kill_profile_chrome(self._profile_dir())
        except Exception:
            pass
            
        if self._auto_login_popup:
            self._auto_login_popup.close()

    def _on_auto_login_result(self, result: dict) -> None:
        if self._auto_login_stopped_by_user:
            return
            
        ok = bool(result.get('success'))
        stopped = bool(result.get('stopped'))
        msg = str(result.get('message') or '')
        
        if stopped:
            QMessageBox.information(self, 'Thông báo', msg if msg else 'Đã dừng auto login.')
            return
            
        if ok:
            self._append_auto_login_log('✅ Auto login thành công. Chờ bạn xác nhận để đóng Chrome profile...')
            QMessageBox.information(self, 'Thông báo', msg if msg else '✅ Auto login thành công. Bấm OK để đóng Chrome profile.')
            
            try:
                from . import chrome
                kill_profile_chrome = chrome.kill_profile_chrome
                kill_profile_chrome(self._profile_dir())
                self._append_auto_login_log('🧹 Đã đóng Chrome profile sau khi bạn xác nhận.')
                
                if self._auto_login_popup:
                    self._auto_login_popup.close()
            except Exception as exc:
                self._append_auto_login_log(f'⚠️ Không thể đóng Chrome profile: {exc}')
        else:
            QMessageBox.warning(self, 'Lỗi', msg if msg else '❌ Auto login thất bại.')

    def _on_auto_login_finished(self) -> None:
        self._auto_login_thread = None
        self._auto_login_worker = None
        self._set_auto_login_button_busy(False)

    def _delete_profile(self) -> None:
        p = self._profile_dir()
        if not p.exists():
            QMessageBox.information(self, 'Thông báo', 'Profile không tồn tại.')
            return
            
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle('Xác nhận')
        msg.setText('Bạn chắc chắn muốn xóa Profile?\n(Chrome đang chạy với profile này có thể bị tắt)')
        msg.setInformativeText(str(p))
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        
        if msg.exec() != QMessageBox.StandardButton.Yes:
            return
            
        try:
            import chrome
            kill_profile_chrome = chrome.kill_profile_chrome
            kill_profile_chrome(p)
            
            shutil.rmtree(p, ignore_errors=True)
            QMessageBox.information(self, 'Thông báo', 'Đã xóa Profile.')
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không xóa được profile: {exc}')
