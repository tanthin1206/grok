from __future__ import annotations
import json
import os
import sys
import dataclasses
from dataclasses import dataclass
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (QApplication, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QMainWindow, QPushButton, QSplitter, 
                             QTabWidget, QVBoxLayout, QWidget, QMessageBox)
from . import status_panel
from .status_panel import StatusPanel
from . import tab_text_to_video
from .tab_text_to_video import TextToVideoTab
from . import tab_image_to_video
from .tab_image_to_video import ImageToVideoTab
from . import popup_theme
from .popup_theme import install_messagebox_theme
from . import tab_grok_settings
from .tab_grok_settings import GrokSettingsTab
import branding_config
from branding_config import WINDOW_TITLE
import settings_manager
from settings_manager import BASE_DIR, get_icon_path
import worker_run_workflow
from worker_run_workflow import WorkflowRunWorker, WorkflowQueueItem

_APP_ROOT = str(BASE_DIR)
DATA_GENERAL_DIR = os.path.join(_APP_ROOT, 'data_general')
CONFIG_PATH = os.path.join(DATA_GENERAL_DIR, 'config.json')
GEMINI_KEYS_PATH = os.path.join(DATA_GENERAL_DIR, 'gemini_api_key.txt')
OLD_CONFIG_PATH = os.path.join(_APP_ROOT, 'config.json')
DEFAULT_DOWNLOAD_DIR = os.path.join(_APP_ROOT, 'downloads')


_icon_cache: dict[str, QIcon] = {}

def icon(name: str) -> QIcon:
    if name in _icon_cache:
        return _icon_cache[name]
    path = get_icon_path(name)
    if os.path.isfile(path):
        ic = QIcon(path)
    else:
        ic = QIcon()
    _icon_cache[name] = ic
    return ic

def app_logo_icon() -> QIcon:
    icon_dir = os.path.join(_APP_ROOT, 'icons')
    candidates = ['app_icon.ico', 'app_icon.png', 'logo.ico', 'logo.png']
    for filename in candidates:
        path = get_icon_path(filename)
        if os.path.isfile(path):
            ic = QIcon(path)
            if not ic.isNull():
                return ic
    
    try:
        for filename in os.listdir(icon_dir):
            if filename.lower().endswith(('.ico', '.png', '.jpg', '.jpeg', '.bmp', '.svg')):
                path = os.path.join(icon_dir, filename)
                if os.path.isfile(path):
                    ic = QIcon(path)
                    if not ic.isNull():
                        return ic
    except Exception:
        pass
    return QIcon()

class _ClickPickLineEdit(QLineEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._picker = None

    def set_picker(self, fn) -> None:
        self._picker = fn

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and callable(self._picker):
            try:
                self._picker()
            except Exception:
                pass
        super().mousePressEvent(event)

@dataclass
class AppConfig:
    multi_video: int = 3
    output_count: int = 1
    video_aspect_ratio: str = '9:16'
    offscreen_chrome: bool = True
    video_output_dir: str = DEFAULT_DOWNLOAD_DIR
    wait_gen_video: int = 15
    wait_gen_image: int = 15
    retry_with_error: int = 3
    CLEAR_DATA_IMAGE: int = 11
    clear_data: int = 5
    clear_data_wait: int = 4
    wait_resend_video: int = 10
    download_mode: str = '720'
    token_option: str = 'Option2'
    seed_mode: str = 'Random'
    seed_value: int = 9797
    gemini_api_keys: str = ''
    grok_user: str = ''
    grok_pass: str = ''
    grok_account_type: str = 'SUPER'
    grok_multi_video: int = 5
    grok_video_length_seconds: int = 6
    grok_video_resolution: str = '480p'

    @staticmethod
    def _config_token_option_for_json(token_option: str) -> str:
        raw = str(token_option).replace(' ', '') if token_option else 'Option2'
        if raw == 'Option1':
            return 'Option 1'
        return 'Option 2'

    @staticmethod
    def _config_token_option_from_json(val: str | None) -> str:
        raw = str(val).strip() if val else 'Option2'
        raw = raw.replace(' ', '')
        if raw not in {'Option1', 'Option2'}:
            raw = 'Option2'
        return raw

    @classmethod
    def load(cls) -> 'AppConfig':
        cfg = cls()
        os.makedirs(DATA_GENERAL_DIR, exist_ok=True)

        load_path = CONFIG_PATH if os.path.isfile(CONFIG_PATH) else OLD_CONFIG_PATH
        try:
            with open(load_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {}

        if not isinstance(data, dict):
            data = {}

        try:
            cfg.multi_video = max(1, int(data.get('MULTI_VIDEO', cfg.multi_video) or cfg.multi_video))
        except Exception:
            pass

        try:
            cfg.output_count = max(1, min(4, int(data.get('OUTPUT_COUNT', cfg.output_count) or cfg.output_count)))
        except Exception:
            pass

        if str(data.get('VIDEO_ASPECT_RATIO', cfg.video_aspect_ratio)):
            cfg.video_aspect_ratio = str(data.get('VIDEO_ASPECT_RATIO', cfg.video_aspect_ratio))

        if str(data.get('VIDEO_OUTPUT_DIR', cfg.video_output_dir)):
            cfg.video_output_dir = str(data.get('VIDEO_OUTPUT_DIR', cfg.video_output_dir))

        if str(data.get('DOWNLOAD_MODE', cfg.download_mode)):
            cfg.download_mode = str(data.get('DOWNLOAD_MODE', cfg.download_mode))

        cfg.token_option = cls._config_token_option_from_json(data.get('TOKEN_OPTION'))

        if str(data.get('SEED_MODE', cfg.seed_mode)):
            cfg.seed_mode = str(data.get('SEED_MODE', cfg.seed_mode))

        try:
            cfg.seed_value = int(data.get('SEED_VALUE', cfg.seed_value) or cfg.seed_value)
        except Exception:
            pass

        cfg.offscreen_chrome = bool(data.get('offscreen_chrome', cfg.offscreen_chrome))

        if os.path.isfile(GEMINI_KEYS_PATH):
            try:
                with open(GEMINI_KEYS_PATH, 'r', encoding='utf-8') as kf:
                    cfg.gemini_api_keys = '\n'.join([ln.strip() for ln in kf.read().splitlines() if ln.strip()])
            except Exception:
                pass

        grok_account = data.get('grok_account') if isinstance(data.get('grok_account'), dict) else {}
        if grok_account:
            if str(grok_account.get('email')):
                cfg.grok_user = str(grok_account.get('email'))
            if str(grok_account.get('password')):
                cfg.grok_pass = str(grok_account.get('password'))

        raw_grok_type = str(
            data.get('GROK_ACCOUNT_TYPE')
            or (grok_account.get('type_account') if isinstance(grok_account, dict) else '')
            or (grok_account.get('TYPE_ACCOUNT') if isinstance(grok_account, dict) else '')
            or cfg.grok_account_type
        ).strip().upper()
        if raw_grok_type == 'ULTRA':
            raw_grok_type = 'SUPER'
        if raw_grok_type not in {'NORMAL', 'SUPER'}:
            raw_grok_type = 'SUPER'
        cfg.grok_account_type = raw_grok_type

        try:
            val = int(data.get('GROK_VIDEO_LENGTH_SECONDS', data.get('grok_video_length_seconds', cfg.grok_video_length_seconds)) or cfg.grok_video_length_seconds)
            cfg.grok_video_length_seconds = 6 if val not in {6, 10} else val
        except Exception:
            cfg.grok_video_length_seconds = 6

        res = str(data.get('GROK_VIDEO_RESOLUTION', data.get('grok_video_resolution', cfg.grok_video_resolution)) or cfg.grok_video_resolution)
        cfg.grok_video_resolution = res if res in {'480p', '720p'} else '480p'

        try:
            cfg.grok_multi_video = max(1, min(20, int(data.get('GROK_MULTI_VIDEO', data.get('grok_multi_video', cfg.grok_multi_video)) or cfg.grok_multi_video)))
        except Exception:
            pass

        if cfg.grok_account_type == 'NORMAL':
            cfg.grok_video_resolution = '480p'
            cfg.grok_video_length_seconds = 6

        if str(cfg.video_aspect_ratio) not in {'16:9', '9:16'}:
            cfg.video_aspect_ratio = '9:16'

        if not str(cfg.video_output_dir).strip():
            cfg.video_output_dir = DEFAULT_DOWNLOAD_DIR

        cfg.video_resolution = '480p'
        cfg.auto_upscale = True
        return cfg

    def save(self) -> None:
        os.makedirs(DATA_GENERAL_DIR, exist_ok=True)

        grok_account = {}
        if str(self.grok_user):
            grok_account['email'] = str(self.grok_user)
        if str(self.grok_pass):
            grok_account['password'] = str(self.grok_pass)

        grok_account_type = str(getattr(self, 'grok_account_type', 'SUPER')).strip().upper()
        if grok_account_type == 'ULTRA':
            grok_account_type = 'SUPER'
        if grok_account_type not in {'NORMAL', 'SUPER'}:
            grok_account_type = 'SUPER'

        grok_account['type_account'] = grok_account_type
        grok_account['TYPE_ACCOUNT'] = grok_account_type

        data = {
            'MULTI_VIDEO': int(self.multi_video) if self.multi_video else 1,
            'OUTPUT_COUNT': int(self.output_count) if self.output_count else 1,
            'VIDEO_ASPECT_RATIO': str(self.video_aspect_ratio) if self.video_aspect_ratio else '9:16',
            'VIDEO_OUTPUT_DIR': str(self.video_output_dir) if self.video_output_dir else DEFAULT_DOWNLOAD_DIR,
            'DOWNLOAD_MODE': str(self.download_mode) if self.download_mode else '720',
            'TOKEN_OPTION': self._config_token_option_for_json(self.token_option),
            'SEED_MODE': str(self.seed_mode) if self.seed_mode else 'Random',
            'SEED_VALUE': int(self.seed_value) if self.seed_value else 9797,
            'GROK_VIDEO_LENGTH_SECONDS': int(self.grok_video_length_seconds) if self.grok_video_length_seconds in (6, 10) else 6,
            'GROK_VIDEO_RESOLUTION': str(self.grok_video_resolution) if self.grok_video_resolution in ('480p', '720p') else '480p',
            'GROK_ACCOUNT_TYPE': grok_account_type,
            'GROK_MULTI_VIDEO': int(self.grok_multi_video) if self.grok_multi_video else 5,
            'grok_account': grok_account,
            'offscreen_chrome': bool(self.offscreen_chrome),
        }

        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        try:
            lines = [ln.strip() for ln in str(self.gemini_api_keys or '').splitlines() if ln.strip()]
            with open(GEMINI_KEYS_PATH, 'w', encoding='utf-8') as kf:
                if lines:
                    kf.write('\n'.join(lines) + '\n')
        except Exception:
            pass
def apply_style(app: QApplication) -> None:
    qss = """
    QWidget {
        font-family: Segoe UI;
        color: #1f2d48;
    }
    QMainWindow { background: #d7e5ff; }
    QWidget#AppRoot { background: #d7e5ff; }

    QLabel {
        font-size: 13px;
        color: #1f2d48;
        background: transparent;
    }

    QTabWidget::pane {
        border: 1px solid #9eb9ea;
        top: -1px;
        background: #d5e4ff;
        border-radius: 10px;
    }
    QGroupBox {
        border: 1px solid #a9c0ea;
        border-radius: 10px;
        margin-top: 8px;
        background: #d9e7ff;
        font-weight: 700;
        color: #1f2d48;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: #1f2d48;
        background: transparent;
    }
    QTabBar::tab {
        background: #bfd4fb;
        color: #233c6a;
        padding: 2px 8px;
        border: 1px solid #9eb9ea;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        margin-right: 4px;
        font-weight: 700;
        font-size: 12px;
        min-height: 22px;
    }
    QTabBar::tab:selected {
        background: #2f63d9;
        color: #ffffff;
        font-weight: 900;
        border-color: #1f4eb7;
    }
    QTabBar::tab:hover:!selected { background: #aac4f5; }

    QPushButton {
        padding: 6px 10px;
        border: 1px solid #c8d7f2;
        border-radius: 8px;
        background: #eaf2ff;
        color: #1f2d48;
        font-weight: 700;
        font-size: 13px;
        min-height: 32px;
    }
    QPushButton:hover { background: #dfeaff; }
    QPushButton:disabled {
        color: #9ca3af;
        background: #e5e7eb;
        border-color: #d1d5db;
    }
    QToolButton:disabled {
        color: #9ca3af;
        background: #e5e7eb;
        border-color: #d1d5db;
    }

    /* Top horizontal tool row (right panel) keeps compact size */
    QPushButton[topRow="true"] {
        padding: 2px 8px;
        font-size: 12px;
        min-height: 22px;
    }

    QPushButton#TopAction {
        background: #e0f2fe;
        border-color: #bae6fd;
        color: #0369a1;
        font-weight: 800;
    }
    QPushButton#TopAction:hover { background: #bae6fd; }

    QPushButton#DangerSoft {
        background: #fee2e2;
        border-color: #fecaca;
        color: #991b1b;
        font-weight: 800;
    }
    QPushButton#DangerSoft:hover { background: #fecaca; }

    QPushButton#Accent { background: #4a7cf3; border-color: #3a6be0; color: white; }
    QPushButton#Accent:hover { background: #3a6be0; }
    QPushButton#Success { background: #16a34a; border-color: #15803d; color: white; }
    QPushButton#Success:hover { background: #15803d; }
    QPushButton#Warning { background: #facc15; border-color: #eab308; color: #1f2d48; }
    QPushButton#Warning:hover { background: #eab308; }
    QPushButton#Orange { background: #f97316; border-color: #ea580c; color: white; }
    QPushButton#Orange:hover { background: #ea580c; }
    QPushButton#Danger { background: #dc2626; border-color: #b91c1c; color: white; }
    QPushButton#Danger:hover { background: #b91c1c; }

    /* Keep disabled state gray even for colored button variants */
    QPushButton#TopAction:disabled,
    QPushButton#DangerSoft:disabled,
    QPushButton#Accent:disabled,
    QPushButton#Success:disabled,
    QPushButton#Warning:disabled,
    QPushButton#Orange:disabled,
    QPushButton#Danger:disabled,
    QPushButton#Zalo:disabled {
        color: #9ca3af;
        background: #e5e7eb;
        border-color: #d1d5db;
    }

    QPushButton#Zalo {
        background: #e0f2fe;
        border-color: #bae6fd;
        color: #0369a1;
        font-weight: 800;
    }
    QPushButton#Zalo:hover { background: #bae6fd; }

    /* Bottom config row: scoped styling (independent from global controls) */
    QWidget#BottomCfgWrap {
        border: 1px solid #8eace3;
        border-radius: 8px;
        background: #c7dbff;
    }
    QWidget#BottomCfgWrap QLabel {
        border: none;
        background: transparent;
        font-size: 12px;
        color: #1f2d48;
        font-weight: 700;
    }
    QComboBox#BottomCfgCombo {
        border: 1px solid #6f93d8;
        border-bottom: 1px solid #6f93d8;
        border-radius: 8px;
        padding: 4px 8px;
        padding-right: 24px;
        background: #2a4f94;
        color: #f3f8ff;
        font-size: 12px;
        min-height: 32px;
    }
    QComboBox#BottomCfgCombo:hover {
        background: #3360b3;
        border-color: #8fb2f5;
    }
    QComboBox#BottomCfgCombo::drop-down {
        border-left: 1px solid #6f93d8;
        width: 20px;
        background: #2a4f94;
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
    }
    QComboBox#BottomCfgCombo::down-arrow {
        image: none;
        width: 0;
        height: 0;
        border-left: 5px solid transparent;
        border-right: 5px solid transparent;
        border-top: 6px solid #eaf2ff;
        margin-right: 5px;
    }
    QComboBox#BottomCfgCombo QAbstractItemView {
        background: #2a4f94;
        color: #f3f8ff;
        border: 1px solid #6f93d8;
        font-size: 12px;
        outline: none;
    }
    QComboBox#BottomCfgCombo QAbstractItemView::item {
        min-height: 30px;
        padding: 4px 8px;
    }
    QLineEdit#BottomCfgLine {
        border: 1px solid #6f93d8;
        border-radius: 8px;
        padding: 4px 8px;
        background: #2a4f94;
        color: #f3f8ff;
        font-size: 12px;
        min-height: 32px;
    }
    QLineEdit#BottomCfgLine:hover {
        background: #3360b3;
        border-color: #8fb2f5;
    }

    QLineEdit {
        border: 1px solid #c8d7f2;
        border-radius: 8px;
        padding: 8px;
        background: #f3f8ff;
        color: #1f2d48;
        selection-background-color: #2563eb;
        selection-color: #ffffff;
        font-size: 13px;
    }

    QComboBox {
        border: 1px solid #c8d7f2;
        border-radius: 8px;
        padding: 6px 8px;
        background: #f3f8ff;
        color: #1f2d48;
        font-size: 13px;
        min-height: 32px;
    }

    /* Prompts: make them clearly larger across all tabs */
    QPlainTextEdit {
        border: 1px solid #c8d7f2;
        border-radius: 8px;
        padding: 8px;
        background: #f1f7ff;
        color: #1f2d48;
        selection-background-color: #2563eb;
        selection-color: #ffffff;
        font-size: 14px;
    }

    QTableWidget {
        border: 1px solid #c8d7f2;
        border-radius: 10px;
        gridline-color: #d8deee;
        background: #f1f7ff;
        color: #1f2d48;
        selection-background-color: #dbeafe;
    }
    QHeaderView::section {
        background: #e8f1ff;
        color: #1f2d48;
        padding: 6px;
        border: 1px solid #c8d7f2;
        font-weight: 800;
    }

    QScrollBar:vertical { background: #eef3fb; width: 12px; margin: 0px; }
    QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 6px; min-height: 30px; }
    QScrollBar::handle:vertical:hover { background: #94a3b8; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #eef3fb; }
    """
    app.setStyleSheet(qss)

class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self._cfg = config
        self.setWindowTitle(str(WINDOW_TITLE) if WINDOW_TITLE else 'GROK TOOL')

        app_ic = app_logo_icon()
        if not app_ic.isNull():
            self.setWindowIcon(app_ic)

        root = QWidget()
        root.setObjectName('AppRoot')
        self.setCentralWidget(root)

        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(True)
        splitter.setHandleWidth(8)
        splitter.setOpaqueResize(True)
        root_layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 0, 0, 12)
        left_layout.setSpacing(6)

        self.grok_tabs = QTabWidget()
        self.grok_tabs.setObjectName('TopTabRow')

        self.tab_grok_text = TextToVideoTab()
        self.tab_grok_image = ImageToVideoTab()
        self.tab_grok_settings = GrokSettingsTab(config=self._cfg)

        while self.tab_grok_image.sub_tabs.count() > 1:
            self.tab_grok_image.sub_tabs.removeTab(1)
        self.tab_grok_image.sub_tabs.setTabText(0, 'Tạo Video Từ Ảnh')
        self.tab_grok_image.sub_tabs.tabBar().setVisible(False)

        self.grok_tabs.addTab(self.tab_grok_text, icon(''), 'Text to Video')
        self.grok_tabs.addTab(self.tab_grok_image, icon(''), 'Image to Video')
        self.grok_tabs.addTab(self.tab_grok_settings, icon(''), 'Cài đặt')

        left_layout.addWidget(self.grok_tabs, 1)
        self.grok_tabs.currentChanged.connect(lambda _: self._on_main_tab_changed())

        bottom = QWidget()
        b = QVBoxLayout(bottom)
        b.setContentsMargins(0, 0, 0, 12)
        b.setSpacing(4)

        btn_row = QHBoxLayout()
        self.btn_start = QPushButton('Tạo video')
        self.btn_start.setObjectName('Accent')
        self.btn_start.clicked.connect(self._on_start_stop)
        btn_row.addWidget(self.btn_start, 3)

        self.btn_stop = QPushButton('Dừng')
        self.btn_stop.setObjectName('Danger')
        self.btn_stop.clicked.connect(self._on_stop_all)
        self.btn_stop.setEnabled(True)
        btn_row.addWidget(self.btn_stop, 1)

        self.btn_view = QPushButton('Xem video/Ảnh')
        self.btn_view.setObjectName('Warning')
        self.btn_view.setIcon(icon('file-open.png'))
        self.btn_view.clicked.connect(self._open_output_folder)
        btn_row.addWidget(self.btn_view, 2)

        b.addLayout(btn_row)

        cfg_wrap = QWidget()
        cfg_wrap.setObjectName('BottomCfgWrap')
        cfg_box_layout = QVBoxLayout(cfg_wrap)
        cfg_box_layout.setContentsMargins(10, 10, 10, 10)
        cfg_box_layout.setSpacing(6)

        cfg_grid = QGridLayout()
        cfg_grid.setContentsMargins(0, 0, 0, 0)
        cfg_grid.setHorizontalSpacing(12)
        cfg_grid.setVerticalSpacing(6)

        self.combo_aspect = QComboBox()
        self.combo_aspect.setObjectName('BottomCfgCombo')
        self.combo_aspect.addItem('Dọc 9:16', '9:16')
        self.combo_aspect.addItem('Ngang 16:9', '16:9')
        idx = self.combo_aspect.findData(str(config.video_aspect_ratio) if config.video_aspect_ratio else '9:16')
        self.combo_aspect.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_aspect.setFixedHeight(32)
        self.combo_aspect.setFixedWidth(136)
        self.combo_aspect.currentIndexChanged.connect(self._on_bottom_config_changed)

        cfg_grid.addWidget(QLabel('Tỉ lệ khung hình'), 0, 0)
        cfg_grid.addWidget(self.combo_aspect, 1, 0)

        cfg_grid.addWidget(QLabel('Chọn thư mục lưu video'), 0, 1)

        self.out_dir = _ClickPickLineEdit()
        self.out_dir.setText(str(config.video_output_dir) if config.video_output_dir else DEFAULT_DOWNLOAD_DIR)
        self.out_dir.setObjectName('BottomCfgLine')
        self.out_dir.setFixedHeight(32)
        self.out_dir.setFixedWidth(300)
        self.out_dir.setReadOnly(True)
        self.out_dir.set_picker(self._browse_out_dir)

        act = self.out_dir.addAction(icon('folder_icon.png'), QLineEdit.ActionPosition.TrailingPosition)
        act.triggered.connect(self._browse_out_dir)

        cfg_grid.addWidget(self.out_dir, 1, 1)

        cfg_box_layout.addLayout(cfg_grid)
        b.addWidget(cfg_wrap)
        left_layout.addWidget(bottom)

        self.status = StatusPanel(config)

        self._queue_worker = WorkflowRunWorker(
            start_job_callback=self._start_queue_job,
            stop_active_callback=self.status.stop,
            log_callback=self.status.append_run_log,
            get_running_count_callback=self.status.get_running_video_count,
            get_max_in_flight_callback=self._get_multi_video_limit,
            request_retry_rows_callback=self.status.get_auto_retry_rows_for_worker,
        )

        self.status.requestStop.connect(self._on_worker_stop_requested)
        self.status.runStateChanged.connect(self._set_running_state)
        self.status.titleChanged.connect(self.setWindowTitle)
        self.status.queueJobsRequested.connect(self._enqueue_jobs)

        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(2000)
        self._queue_timer.timeout.connect(self._on_queue_tick)
        self._queue_timer.start()

        splitter.addWidget(left)
        splitter.addWidget(self.status)

        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        def _set_initial_split():
            try:
                total = max(1, splitter.width())
                splitter.setSizes([int(total * 0.48), int(total * 0.52)])
            except Exception:
                pass

        QTimer.singleShot(0, _set_initial_split)

        left.setMinimumWidth(260)
        self.resize(1296, 805)
        self._on_main_tab_changed()

    def _active_leaf_tab(self) -> QWidget:
        return self.grok_tabs.currentWidget()

    def _active_platform_prefix(self) -> str:
        return 'GROK'

    def _on_main_tab_changed(self) -> None:
        self._update_bottom_config_visibility()
        self._update_start_button_for_tab()

    def _update_bottom_config_visibility(self) -> None:
        return

    def _update_start_button_for_tab(self) -> None:
        platform_prefix = self._active_platform_prefix()
        if bool(self.status.isRunning()) or self._queue_worker.is_busy():
            self.btn_start.setText(f'{platform_prefix} - Thêm vào hàng chờ')
            self.btn_start.setObjectName('Success')
            self.btn_start.style().unpolish(self.btn_start)
            self.btn_start.style().polish(self.btn_start)
            self.btn_start.update()
            return

        cur = self._active_leaf_tab()
        if cur is self.tab_grok_text:
            self.btn_start.setText(f'{platform_prefix} - Tạo Text to Video')
            self.btn_start.setObjectName('Accent')
        elif cur is self.tab_grok_image:
            self.btn_start.setText(f'{platform_prefix} - Tạo Video Từ Ảnh')
            self.btn_start.setObjectName('Accent')
        elif cur is self.tab_grok_settings:
            self.btn_start.setText(f'{platform_prefix} - Không chạy workflow')
            self.btn_start.setObjectName('Warning')
        else:
            self.btn_start.setText(f'{platform_prefix} - Tạo theo tab')
            self.btn_start.setObjectName('Accent')

        self.btn_start.style().unpolish(self.btn_start)
        self.btn_start.style().polish(self.btn_start)
        self.btn_start.update()

    def _flow_name_from_current_tab(self) -> str:
        cur = self._active_leaf_tab()
        if cur is self.tab_grok_text:
            return 'grok_text_to_video'
        if cur is self.tab_grok_image:
            return 'grok_image_to_video'
        if cur is self.tab_grok_settings:
            return 'grok_settings'
        return 'unknown'

    def _browse_out_dir(self) -> None:
        cur = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        path = QFileDialog.getExistingDirectory(self, 'Chọn thư mục lưu video', cur)
        if path:
            self.out_dir.setText(path)
            self._on_bottom_config_changed()

    def _on_bottom_config_changed(self) -> None:
        self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
        self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        self._cfg.video_resolution = '480p'
        self._cfg.auto_upscale = True
        self._cfg.save()

    def _open_output_folder(self) -> None:
        out = str(self._cfg.video_output_dir) or DEFAULT_DOWNLOAD_DIR
        try:
            os.makedirs(out, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))
        except Exception:
            pass

    def _get_multi_video_limit(self) -> int:
        try:
            value = int(getattr(self._cfg, 'grok_multi_video', getattr(self._cfg, 'multi_video', 1)) or 1)
            return max(1, value)
        except Exception:
            return 1

    def _on_worker_stop_requested(self) -> None:
        self.btn_start.setEnabled(False)

    def _is_add_to_queue_mode(self) -> bool:
        return bool(self.status.isRunning()) or self._queue_worker.is_busy()

    def _confirm_add_to_queue(self, workflow_title: str, prompt_count: int) -> bool:
        count = int(prompt_count) or 0
        return QMessageBox.question(
            self,
            'Xác nhận thêm hàng chờ',
            f'Bạn có muốn thêm {count} prompt {workflow_title} vào hàng chờ không?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) == QMessageBox.StandardButton.Yes

    def _notify_add_to_queue_success(self, workflow_title: str, prompt_count: int) -> None:
        count = int(prompt_count) or 0
        QMessageBox.information(self, 'Thêm hàng chờ', f'Đã thêm thành công {count} prompt {workflow_title} vào hàng chờ.')

    def _start_queue_job(self, item: WorkflowQueueItem) -> bool:
        return self.status.start_queued_job(item.mode_key, item.rows)

    def _enqueue_payload(self, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            return

        try:
            rows = [int(r) for r in payload.get('rows', [])]
        except Exception:
            return

        if not rows:
            return

        mode_key = str(payload.get('mode_key') or '').strip()
        label = str(payload.get('label') or '')
        if not label and mode_key:
            label = 'workflow'

        pending = self._queue_worker.enqueue(WorkflowQueueItem(mode_key=mode_key, rows=rows, label=label))
        self.status.append_run_log(f'📥 Đã thêm hàng chờ: {label} ({len(rows)} dòng) | Còn chờ: {pending}')

        if self.status.isRunning():
            self._queue_worker.ensure_started()

        self._update_start_button_for_tab()

    def _enqueue_jobs(self, jobs: list) -> None:
        if not jobs:
            return
        for payload in jobs:
            if isinstance(payload, dict):
                self._enqueue_payload(payload)

    def _on_queue_tick(self) -> None:
        if not self._queue_worker.is_stopping():
            self._queue_worker.ensure_started()

    def _on_stop_all(self) -> None:
        if not bool(self.status.isRunning()) and not self._queue_worker.is_busy():
            QMessageBox.information(self, 'Thông báo', 'Hiện không có workflow nào đang chạy.')
            return

        confirm = QMessageBox.question(
            self,
            'Xác nhận dừng',
            'Bạn có chắc muốn dừng workflow đang chạy và xóa toàn bộ hàng chờ?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._queue_worker.stop_all()
        self._update_start_button_for_tab()

    def _on_start_stop(self) -> None:
        flow_name = self._flow_name_from_current_tab()

        if flow_name == 'grok_settings':
            QMessageBox.information(self, 'Cài đặt GROK', 'Tab Cài đặt GROK không chạy workflow.')
            return

        self._cfg.video_aspect_ratio = str(self.combo_aspect.currentData()) or '9:16'
        self._cfg.video_output_dir = self.out_dir.text().strip() or DEFAULT_DOWNLOAD_DIR
        self._cfg.video_resolution = '480p'
        self._cfg.auto_upscale = True
        self._cfg.save()

        if flow_name == 'grok_image_to_video':
            items = self.tab_grok_image.get_workflow_items()
            if not items:
                QMessageBox.warning(self, 'Không có dữ liệu', 'Hãy nhập ảnh và prompt ở tab GROK > Image to Video.')
                return

            invalid = []
            for i, item in enumerate(items):
                if not str(item.get('image_link') or '').strip():
                    invalid.append(i + 1)

            if invalid:
                preview = ', '.join(str(x) for x in invalid[:8])
                more = '...' if len(invalid) > 8 else ''
                QMessageBox.warning(
                    self,
                    'Thiếu ảnh',
                    f'Mode Tạo Video Từ Ảnh yêu cầu ảnh đầu vào cho mỗi dòng.\nDòng thiếu: {preview}{more}',
                )
                return

            if self._is_add_to_queue_mode():
                if self._confirm_add_to_queue('tạo video từ ảnh', len(items)):
                    self._notify_add_to_queue_success('tạo video từ ảnh', len(items))
                return

            payload = self.status.enqueue_grok_image_to_video(items)
            self._enqueue_payload(payload)
            return

        if flow_name != 'grok_text_to_video':
            QMessageBox.information(self, 'Chưa hỗ trợ', f"Tab hiện tại là '{flow_name}'.")
            return

        prompts = self.tab_grok_text.get_prompts()
        if not prompts:
            QMessageBox.warning(self, 'Không có prompt', 'Hãy nhập ít nhất một prompt ở tab GROK > Text to Video.')
            return

        if self._is_add_to_queue_mode():
            if self._confirm_add_to_queue('tạo video từ văn bản', len(prompts)):
                self._notify_add_to_queue_success('tạo video từ văn bản', len(prompts))
            return

        payload = self.status.enqueue_grok_text_to_video(prompts)
        self._enqueue_payload(payload)

    def _set_running_state(self, running: bool) -> None:
        self._queue_worker.on_run_state_changed(bool(running))
        self._update_start_button_for_tab()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(True)

    def closeEvent(self, event) -> None:
        self.status.shutdown(timeout_ms=2200)
        super().closeEvent(event)
def main() -> None:
    app = QApplication(sys.argv)
    app_ic = app_logo_icon()
    if not app_ic.isNull():
        app.setWindowIcon(app_ic)
        
    install_messagebox_theme()
    apply_style(app)
    
    cfg = AppConfig.load()
    win = MainWindow(cfg)
    win.show()
    
    sys.exit(app.exec())
