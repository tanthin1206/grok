from __future__ import annotations
import json
import shutil
import pathlib
from pathlib import Path
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QDialog, QGridLayout, QGroupBox, QHBoxLayout, 
                             QLabel, QMessageBox, QPushButton, QSizePolicy, QVBoxLayout, QWidget)
import grok_chrome_manager
from grok_chrome_manager import kill_profile_chrome, open_profile_chrome, resolve_profile_dir
import settings_manager
from settings_manager import DATA_GENERAL_DIR

GROK_CONFIG_PATH = DATA_GENERAL_DIR / 'grok_config.json'

class GrokSettingsTab(QWidget):
    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config
        self._load_grok_runtime_config()
        self._grok_runtime_cfg = self._load_grok_runtime_config()
        self._profile_popup = None
        self._profile_popup_status = None
        self._last_profile_dir = ''
        
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(12)
        
        box = QGroupBox('Cài đặt GROK')
        box.setStyleSheet('QGroupBox{font-weight:800;}')
        body = QVBoxLayout(box)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(10)
        
        combo_style = """
            QComboBox {
                border: 1px solid #7ea5f0;
                border-radius: 7px;
                padding: 4px 26px 4px 8px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #f8fbff, stop:1 #e6f0ff);
                color: #1e2f4f;
                font-weight: 600;
                min-height: 28px;
            }
            QComboBox::drop-down {
                width: 22px;
                border-left: 1px solid #bfd2f6;
                background: #dbe8ff;
                border-top-right-radius: 7px;
                border-bottom-right-radius: 7px;
            }
            QComboBox:disabled {
                color: #60708f;
                border: 1px solid #b7c6e4;
                background: #ecf2ff;
            }
            QComboBox::drop-down:disabled {
                background: #dde6f7;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #bcd0f4;
                selection-background-color: #dbe9ff;
                selection-color: #1f2d48;
                background: #f7fbff;
            }
        """
        
        setup_box = QGroupBox('Thiết lập video GROK')
        setup_box.setStyleSheet('QGroupBox{font-weight:700;}')
        setup_layout = QGridLayout(setup_box)
        setup_layout.setContentsMargins(10, 8, 10, 8)
        setup_layout.setHorizontalSpacing(10)
        setup_layout.setVerticalSpacing(8)
        
        def _new_combo() -> QComboBox:
            c = QComboBox()
            c.setFixedHeight(30)
            c.setFixedWidth(150)
            c.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            c.setMinimumContentsLength(6)
            c.setStyleSheet(combo_style)
            return c
            
        self.grok_account_type = _new_combo()
        self.grok_account_type.addItem('SUPER', 'SUPER')
        self.grok_account_type.addItem('NORMAL', 'NORMAL')
        
        current_type = 'SUPER'
        if getattr(config, 'grok_account_type', 'SUPER'):
            current_type = str(getattr(config, 'grok_account_type', 'SUPER')).strip().upper()
        if current_type == 'ULTRA':
            current_type = 'SUPER'
            
        idx_type = self.grok_account_type.findData(current_type if current_type == 'NORMAL' else 'SUPER')
        self.grok_account_type.setCurrentIndex(idx_type if idx_type >= 0 else 0)
        
        self.grok_video_length = _new_combo()
        self.grok_video_length.addItem('6 giây', 6)
        self.grok_video_length.addItem('10 giây', 10)
        
        cur_len = 6
        if getattr(config, 'grok_video_length_seconds', 6):
            cur_len = 6
        
        idx_len = 1 if cur_len == 10 else 0
        self.grok_video_length.setCurrentIndex(idx_len)
        
        self.grok_video_resolution = _new_combo()
        self.grok_video_resolution.addItem('480', '480p')
        self.grok_video_resolution.addItem('720', '720p')
        
        cur_res = '480p'
        if getattr(config, 'grok_video_resolution', '480p'):
            cur_res = '480p'
            
        self.grok_video_resolution.setCurrentIndex(1 if cur_res == '720p' else 0)
        
        self.grok_multi_video = _new_combo()
        for value in range(1, 21):
            self.grok_multi_video.addItem(str(value), value)
            
        cur_multi = int(self._grok_runtime_cfg.get('MULTI_VIDEO', getattr(config, 'grok_multi_video', 5))) or 5
        if cur_multi < 1: cur_multi = 1
        if cur_multi > 20: cur_multi = 20
        
        idx_multi = self.grok_multi_video.findData(cur_multi)
        self.grok_multi_video.setCurrentIndex(idx_multi if idx_multi >= 0 else 0)
        
        lb_account_type = QLabel('Loại tài khoản:')
        lb_account_type.setStyleSheet('font-weight:700; color:#1f2d48;')
        lb_length = QLabel('Thời gian video:')
        lb_length.setStyleSheet('font-weight:700; color:#1f2d48;')
        lb_resolution = QLabel('Chất lượng video:')
        lb_resolution.setStyleSheet('font-weight:700; color:#1f2d48;')
        lb_multi = QLabel('MULTI VIDEO:')
        lb_multi.setStyleSheet('font-weight:700; color:#1f2d48;')
        
        try:
            lb_account_type.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            lb_length.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            lb_resolution.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            lb_multi.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
            
            lb_account_type.setFixedWidth(115)
            lb_length.setFixedWidth(115)
            lb_resolution.setFixedWidth(115)
            lb_multi.setFixedWidth(115)
            
            setup_layout.addWidget(lb_account_type, 0, 0)
            setup_layout.addWidget(self.grok_account_type, 0, 1)
            setup_layout.addWidget(lb_multi, 0, 2)
            setup_layout.addWidget(self.grok_multi_video, 0, 3)
            
            setup_layout.addWidget(lb_length, 1, 0)
            setup_layout.addWidget(self.grok_video_length, 1, 1)
            setup_layout.addWidget(lb_resolution, 1, 2)
            setup_layout.addWidget(self.grok_video_resolution, 1, 3)
            
            setup_layout.setColumnStretch(0, 0)
            setup_layout.setColumnStretch(1, 0)
            setup_layout.setColumnStretch(2, 0)
            setup_layout.setColumnStretch(3, 0)
            setup_layout.setColumnMinimumWidth(1, 150)
            setup_layout.setColumnMinimumWidth(3, 150)
        except Exception:
            pass
            
        body.addWidget(setup_box)
        
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        
        self.btn_open_login = QPushButton('Mở Chrome để đăng nhập GROK')
        self.btn_open_login.setObjectName('Warning')
        self.btn_open_login.setFixedHeight(36)
        self.btn_open_login.clicked.connect(self._open_grok_profile)
        btn_row.addWidget(self.btn_open_login, 1)
        
        self.btn_delete_profile = QPushButton('Xóa Profile GROK')
        self.btn_delete_profile.setObjectName('Danger')
        self.btn_delete_profile.setFixedHeight(36)
        self.btn_delete_profile.clicked.connect(self._delete_profile)
        btn_row.addWidget(self.btn_delete_profile)
        
        body.addLayout(btn_row)
        
        self.grok_account_type.currentIndexChanged.connect(self._apply_account_constraints)
        self.grok_video_resolution.currentIndexChanged.connect(self._enforce_current_constraints)
        self.grok_video_length.currentIndexChanged.connect(self._enforce_current_constraints)
        self._apply_account_constraints()
        
        self.btn_save = QPushButton('Lưu cài đặt GROK')
        self.btn_save.setObjectName('Accent')
        self.btn_save.setFixedHeight(36)
        self.btn_save.clicked.connect(self._save)
        body.addWidget(self.btn_save, 0, Qt.AlignmentFlag.AlignHCenter)
        
        note = QLabel('<b>Hướng Dẫn:</b><br/>Bước 1: Bấm nút <b>Mở Chrome để đăng nhập GROK</b>, sau đó nhập tài khoản và mật khẩu để đăng nhập GROK trực tiếp trên web.<br/>Bước 2: Chọn <b>Loại Tài khoản</b> đã đăng nhập. Tài khoản thường chọn <b>NORMAL</b>, tài khoản SUPER chọn <b>SUPER</b> (chọn sai sẽ không chạy được).<br/>Bước 3: Cấu hình thời gian video <b>6s hoặc 10s</b> (tài khoản <b>SUPER</b> mới chọn được 10s).<br/>Bước 4: Chọn chất lượng video <b>480 hoặc 720</b> (tool đã tự upscale lên 720 mức cao nhất của GROK rồi).')
        note.setWordWrap(True)
        note.setStyleSheet('color:#334155; font-size:12px; line-height:1.45; background:#f7fbff; border:1px solid #c8d7f2; border-radius:8px; padding:10px;')
        body.addWidget(note)
        promo_notice = QLabel(
            '<b>THÔNG BÁO QUAN TRỌNG:</b><br/>'
            'Tài khoản grok chỉ cho tạo giới hạn <b>5 video/ngày</b>; '
            'để làm nhiều hơn hãy dùng tài khoản <b>super grok</b>.<br/>'
            '<b>Liên hệ ngay ZALO 0384220688</b> của admin để mua với giá ưu đãi nhé!'
        )
        promo_notice.setWordWrap(True)
        promo_notice.setStyleSheet(
            'color:#7a2e0b; font-size:12px; line-height:1.5; '
            'background:#fff4d6; border:2px solid #f59e0b; border-radius:10px; padding:12px; '
            'font-weight:700;'
        )
        body.addWidget(promo_notice)
        body.addStretch(1)
        root.addWidget(box, 1)

    def _save(self) -> None:
        try:
            account_type = str(self.grok_account_type.currentData()).strip().upper()
            if account_type == 'ULTRA':
                account_type = 'SUPER'
            if account_type == 'NORMAL':
                account_type = 'NORMAL'
            else:
                account_type = 'SUPER'
            
            setattr(self._cfg, 'grok_account_type', account_type)
            self._enforce_current_constraints()
            
            length_value = int(self.grok_video_length.currentData())
            if account_type == 'NORMAL':
                length_value = 6
            if length_value not in (6, 10):
                length_value = 6
            setattr(self._cfg, 'grok_video_length_seconds', length_value)
            
            resolution_value = str(self.grok_video_resolution.currentData()) or '480p'
            if account_type == 'NORMAL':
                resolution_value = '480p'
            if resolution_value not in ('480p', '720p'):
                resolution_value = '480p'
            setattr(self._cfg, 'grok_video_resolution', resolution_value)
            
            multi_video_value = int(self.grok_multi_video.currentData())
            if multi_video_value < 1: multi_video_value = 1
            if multi_video_value > 20: multi_video_value = 20
            setattr(self._cfg, 'grok_multi_video', multi_video_value)
            self._grok_runtime_cfg['MULTI_VIDEO'] = multi_video_value
            
            self._cfg.save()
            self._save_grok_runtime_config()
            
            if account_type == 'NORMAL':
                QMessageBox.information(self, 'Thông báo', 'Đã lưu cài đặt GROK.\nNORMAL: chỉ 480p + 6 giây và sẽ tự upscale khi video 480p.')
            else:
                QMessageBox.information(self, 'Thông báo', 'Đã lưu cài đặt GROK.')
                
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không lưu được cài đặt GROK: {exc}')

    def _is_normal_account(self) -> bool:
        val = str(self.grok_account_type.currentData()).strip().upper()
        if val == 'ULTRA':
            val = 'SUPER'
        return val == 'NORMAL'

    def _apply_account_constraints(self) -> None:
        is_normal = self._is_normal_account()
        self.grok_video_resolution.setEnabled(not is_normal)
        self.grok_video_length.setEnabled(not is_normal)
        
        if is_normal:
            idx_res = self.grok_video_resolution.findData('480p')
            if idx_res >= 0:
                self.grok_video_resolution.setCurrentIndex(idx_res)
            
            idx_len = self.grok_video_length.findData(6)
            if idx_len >= 0:
                self.grok_video_length.setCurrentIndex(idx_len)

    def _enforce_current_constraints(self) -> None:
        if self._is_normal_account():
            idx_res = self.grok_video_resolution.findData('480p')
            if idx_res >= 0 and self.grok_video_resolution.currentIndex() != idx_res:
                self.grok_video_resolution.setCurrentIndex(idx_res)
            
            idx_len = self.grok_video_length.findData(6)
            if idx_len >= 0 and self.grok_video_length.currentIndex() != idx_len:
                self.grok_video_length.setCurrentIndex(idx_len)

    def _load_grok_runtime_config(self) -> dict:
        try:
            if GROK_CONFIG_PATH.is_file():
                raw = GROK_CONFIG_PATH.read_text(encoding='utf-8')
                if isinstance(json.loads(raw), dict):
                    data = dict(json.loads(raw))
                else:
                    data = {}
            else:
                data = {}
        except Exception:
            data = {}
            
        try:
            value = int(data.get('MULTI_VIDEO', 5)) or 5
            if value < 1: value = 1
            if value > 20: value = 20
            data['MULTI_VIDEO'] = value
            setattr(self._cfg, 'grok_multi_video', value)
        except Exception:
            value = 5
            
        return data

    def _save_grok_runtime_config(self) -> None:
        try:
            GROK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(GROK_CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._grok_runtime_cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _profile_dir(self) -> Path:
        return resolve_profile_dir()

    def _open_grok_profile(self) -> None:
        try:
            self._save()
            
            opened = open_profile_chrome(url='https://grok.com/')
            if opened.get('profile_dir'):
                self._last_profile_dir = self._profile_dir()
                self._show_profile_popup(
                    self._last_profile_dir,
                    str(opened.get('host') or '127.0.0.1'),
                    int(opened.get('port') or 0)
                )
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không mở được Chrome GROK: {exc}')

    def _show_profile_popup(self, profile_dir: str, host: str, port: int) -> None:
        if self._profile_popup is not None:
            self._profile_popup.close()
            
        popup = QDialog(self)
        popup.setWindowTitle('GROK Profile')
        popup.setMinimumWidth(560)
        popup.setModal(False)
        
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        
        title = QLabel('Đã mở Chrome GROK')
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
        btn_row.addStretch(1)
        
        btn_close_chrome = QPushButton('Tắt Chrome GROK')
        btn_close_chrome.setObjectName('Danger')
        btn_close_chrome.setFixedHeight(36)
        btn_close_chrome.clicked.connect(self._close_profile_chrome)
        btn_row.addWidget(btn_close_chrome)
        
        btn_close = QPushButton('Đóng')
        btn_close.setObjectName('Warning')
        btn_close.setFixedHeight(36)
        btn_close.clicked.connect(lambda: popup.close())
        btn_row.addWidget(btn_close)
        
        layout.addLayout(btn_row)
        
        self._profile_popup = popup
        self._profile_popup_status = status
        
        setattr(self._profile_popup, '_profile_popup_info', info)
        
        info = getattr(self._profile_popup, '_profile_popup_info', None)
        if isinstance(info, QLabel):
            info.setText(f"Chrome GROK đang mở để đăng nhập.\nCDP: {host}:{port}\nProfile: {profile_dir}\n\nSau khi login xong bạn có thể để mở, hoặc bấm 'Tắt Chrome GROK'.")
            
        if isinstance(self._profile_popup_status, QLabel):
            self._profile_popup_status.setText('Trạng thái: Chrome GROK đang chạy')
            
        self._profile_popup.show()
        self._profile_popup.raise_()
        self._profile_popup.activateWindow()

    def _close_profile_chrome(self) -> None:
        try:
            if self._last_profile_dir:
                profile_dir = self._profile_dir()
                kill_profile_chrome(profile_dir)
                
                if isinstance(self._profile_popup_status, QLabel):
                    self._profile_popup_status.setText('Trạng thái: Đã tắt Chrome GROK')
        except Exception as exc:
            QMessageBox.warning(self, 'Cảnh báo', f'Không tắt được Chrome GROK: {exc}')

    def _delete_profile(self) -> None:
        p = self._profile_dir()
        if not p.exists():
            QMessageBox.information(self, 'Thông báo', 'Profile GROK không tồn tại.')
            return
            
        if QMessageBox.question(self, 'Xác nhận', f'Bạn chắc chắn muốn xóa profile GROK?\n{p}', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
            
        try:
            kill_profile_chrome(p)
            shutil.rmtree(p, ignore_errors=True)
            QMessageBox.information(self, 'Thông báo', 'Đã xóa profile GROK.')
        except Exception as exc:
            QMessageBox.critical(self, 'Lỗi', f'Không xóa được profile GROK: {exc}')
