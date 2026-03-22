from __future__ import annotations

import json
import os
import re
import importlib.util
import time
import subprocess
import threading
from pathlib import Path
from datetime import datetime

import imageio_ffmpeg

from PyQt6.QtCore import pyqtSignal, Qt, QUrl, QTimer, QSize, QThread
from PyQt6.QtGui import QDesktopServices, QPainter, QColor, QBrush, QPixmap
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QHeaderView,
    QToolButton,
    QMessageBox,
    QStackedWidget,
    QGroupBox,
    QPlainTextEdit,
    QSizePolicy,
    QFileDialog,
    QDialog,
    QTextEdit,
    QDialogButtonBox,
    QSplitter,
)

from PyQt6.QtGui import QIcon

from .status_help_view import build_status_help_view, get_status_help_file_path
from A_workflow_text_to_video import TextToVideoWorkflow
from worker_run_workflow_grok import GrokImageToVideoWorker, GrokTextToVideoWorker
from settings_manager import SettingsManager, WORKFLOWS_DIR, BASE_DIR, DATA_GENERAL_DIR, get_icon_path


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


def _icon(name: str) -> QIcon:
    if not name:
        return QIcon()
    path = get_icon_path(name)
    if os.path.isfile(path):
        return QIcon(path)
    return QIcon()


class _SelectAllHeader(QHeaderView):
    def __init__(self, panel: "StatusPanel"):
        super().__init__(Qt.Orientation.Horizontal, panel.table)
        self._panel = panel

        # A real widget (instead of paint-only) so the icon never "disappears"
        # due to style/paint quirks.
        self._btn = QToolButton(self.viewport())
        self._btn.setAutoRaise(True)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn.setIconSize(QSize(16, 16))
        self._btn.setFixedSize(22, 22)
        self._btn.clicked.connect(lambda _=False: self._panel._on_header_clicked(0))
        self.sync_from_panel()

        # Keep the button centered in section 0.
        try:
            self.sectionResized.connect(lambda *_: self._reposition_btn())
            self.sectionMoved.connect(lambda *_: self._reposition_btn())
            self.geometriesChanged.connect(self._reposition_btn)
        except Exception:
            pass

    def paintSection(self, painter: QPainter, rect, logicalIndex: int) -> None:
        super().paintSection(painter, rect, logicalIndex)
        if int(logicalIndex) != 0:
            return

        # Keep paint fallback (in case the button can't be created), but the
        # button is the primary UX.
        panel = self._panel
        ic = panel._cb_on if panel._select_all_checked else panel._cb_off
        if ic is not None and not ic.isNull():
            pm = ic.pixmap(16, 16)
            if not pm.isNull():
                try:
                    painter.save()
                    x = int(rect.x() + (rect.width() - pm.width()) / 2)
                    y = int(rect.y() + (rect.height() - pm.height()) / 2)
                    painter.drawPixmap(x, y, pm)
                finally:
                    try:
                        painter.restore()
                    except Exception:
                        pass
                return

        try:
            painter.save()
            side = 14
            x = int(rect.x() + (rect.width() - side) / 2)
            y = int(rect.y() + (rect.height() - side) / 2)
            painter.setPen(QColor("#16a34a"))
            painter.setBrush(QBrush(QColor("#16a34a") if panel._select_all_checked else QColor("transparent")))
            painter.drawRect(x, y, side, side)
            if panel._select_all_checked:
                painter.setPen(QColor("#ffffff"))
                painter.drawText(x, y - 1, side, side + 2, int(Qt.AlignmentFlag.AlignCenter), "âœ“")
        finally:
            try:
                painter.restore()
            except Exception:
                pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reposition_btn()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._reposition_btn()

    def _reposition_btn(self) -> None:
        try:
            # Use viewport-based coordinates; works correctly even if sections are moved.
            x0 = int(self.sectionViewportPosition(0))
            w0 = int(self.sectionSize(0))
            h = int(self.height())
        except Exception:
            return
        if w0 <= 0 or h <= 0:
            return
        x = int(x0 + (w0 - self._btn.width()) / 2)
        y = int((h - self._btn.height()) / 2)
        self._btn.move(x, y)

    def sync_from_panel(self) -> None:
        panel = self._panel
        panel._apply_checkbox_button_state(self._btn, panel._select_all_checked)
        self._btn.setVisible(True)
        self._reposition_btn()

    def mousePressEvent(self, event) -> None:
        try:
            idx = int(self.logicalIndexAt(event.pos()))
        except Exception:
            idx = -1
        if idx == 0:
            try:
                self._panel._on_header_clicked(0)
            except Exception:
                pass
            return
        super().mousePressEvent(event)


class _IdeaToVideoWorker(QThread):
    log_message = pyqtSignal(str)
    completed = pyqtSignal(dict)

    def __init__(
        self,
        project_name: str,
        idea_text: str,
        scene_count: int,
        style: str,
        language: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._project_name = str(project_name or "default_project")
        self._idea_text = str(idea_text or "")
        self._scene_count = int(scene_count or 1)
        self._style = str(style or "3d_Pixar")
        self._language = str(language or "Tiáº¿ng Viá»‡t (vi-VN)")
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        def _log(message: str) -> None:
            self.log_message.emit(str(message or ""))

        def _should_stop() -> bool:
            return bool(self._stop_requested)

        try:
            from idea_to_video import idea_to_video_workflow

            result = idea_to_video_workflow(
                self._project_name,
                self._idea_text,
                scene_count=self._scene_count,
                style=self._style,
                language=self._language,
                log_callback=_log,
                stop_check=_should_stop,
            )
            if not isinstance(result, dict):
                result = {"success": False, "message": "Káº¿t quáº£ Idea to Video khÃ´ng há»£p lá»‡."}
            self.completed.emit(result)
        except BaseException as exc:
            self.completed.emit({"success": False, "message": f"Lá»—i Idea to Video: {exc}"})

class StatusPanel(QWidget):
    COL_CHECK = 0
    COL_STT = 1
    COL_VIDEO = 2
    COL_STATUS = 3
    COL_MODE = 4
    COL_PROMPT = 5

    MODE_TEXT_TO_VIDEO = "text_to_video"
    MODE_IMAGE_TO_VIDEO_SINGLE = "image_to_video_single"
    MODE_IMAGE_TO_VIDEO_START_END = "image_to_video_start_end"
    MODE_CHARACTER_SYNC = "character_sync"
    MODE_CREATE_IMAGE_PROMPT = "create_image_prompt"
    MODE_CREATE_IMAGE_REFERENCE = "create_image_reference"
    MODE_GROK_TEXT_TO_VIDEO = "grok_text_to_video"
    MODE_GROK_IMAGE_TO_VIDEO = "grok_image_to_video"
    AUTO_RETRY_ERROR_CODES = {"403", "13", "500"}
    AUTO_RETRY_MAX_PER_ROW = 1

    requestStop = pyqtSignal()
    runStateChanged = pyqtSignal(bool)
    titleChanged = pyqtSignal(str)
    queueJobsRequested = pyqtSignal(list)
    thumbnailReady = pyqtSignal(str, str)

    def __init__(self, config, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg = config
        self._running = False
        self._workflow: TextToVideoWorkflow | None = None
        self._workflows: list[QThread] = []
        self._idea_worker: _IdeaToVideoWorker | None = None
        self._retry_mode_queue: list[tuple[str, list[int]]] = []
        self._global_stop_requested = False
        self._stop_poll_attempts = 0
        self._active_queue_rows: set[int] = set()
        self._awaiting_completion_confirmation = False
        self._completion_poll_scheduled = False
        self._completion_poll_attempts = 0
        self._loading_status_snapshot = False
        self._status_loaded = False
        self._thumb_jobs_inflight: set[str] = set()
        self._thumb_attempted_mtime: dict[str, float] = {}
        self.thumbnailReady.connect(self._on_thumbnail_ready)

        self._cb_off = _icon("checkbox-unchecked.png")
        self._cb_on = _icon("checkbox-checked.png")
        self._use_checkbox_icon = bool((self._cb_off is not None and not self._cb_off.isNull()) and (self._cb_on is not None and not self._cb_on.isNull()))
        self._select_all_checked = False

        layout = QVBoxLayout(self)
        # Keep panel padding but bring the table closer to the toolbar.
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(6)

        # Toolbar
        tb = QHBoxLayout()
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(8)
        self.btn_join_video = QPushButton("Ná»‘i video")
        self.btn_join_video.setProperty("topRow", True)
        self.btn_join_video.setObjectName("TopAction")
        self.btn_join_video.clicked.connect(self._on_join_video_clicked)
        tb.addWidget(self.btn_join_video)

        self.btn_retry = QPushButton("Táº¡o láº¡i video")
        self.btn_retry.setProperty("topRow", True)
        self.btn_retry.setObjectName("TopAction")
        self.btn_retry.clicked.connect(self._on_retry_selected_clicked)
        tb.addWidget(self.btn_retry)

        self.btn_retry_failed = QPushButton("Táº¡o láº¡i video lá»—i")
        self.btn_retry_failed.setProperty("topRow", True)
        self.btn_retry_failed.setObjectName("TopAction")
        self.btn_retry_failed.clicked.connect(self._on_retry_failed_clicked)
        tb.addWidget(self.btn_retry_failed)

        self.btn_cut_last = QPushButton("Cáº¯t áº£nh cuá»‘i")
        self.btn_cut_last.setProperty("topRow", True)
        self.btn_cut_last.setObjectName("TopAction")
        self.btn_cut_last.clicked.connect(self._on_cut_last_clicked)
        tb.addWidget(self.btn_cut_last)

        self.btn_del = QPushButton("XÃ³a káº¿t quáº£")
        self.btn_del.setProperty("topRow", True)
        self.btn_del.setObjectName("DangerSoft")
        self.btn_del.clicked.connect(self.delete_selected_rows)
        tb.addWidget(self.btn_del)


        tb.addStretch(1)
        layout.addLayout(tb)

        summary_bar = QHBoxLayout()
        summary_bar.setContentsMargins(0, 0, 0, 2)
        summary_bar.setSpacing(8)
        self.lbl_status_summary = QLabel("")
        self.lbl_status_summary.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_status_summary.setStyleSheet("font-weight:700;")
        summary_bar.addWidget(self.lbl_status_summary)
        self.btn_open_guide = QPushButton("Xem HÆ°á»›ng Dáº«n Sá»­ Dá»¥ng TOOL")
        self.btn_open_guide.setProperty("topRow", True)
        self.btn_open_guide.setObjectName("TopAction")
        self.btn_open_guide.clicked.connect(self._open_usage_guide_file)
        summary_bar.addWidget(self.btn_open_guide)
        summary_bar.addStretch(1)
        layout.addLayout(summary_bar)

        # Main content area: show guide when empty; show status table when there are rows.
        self._body_splitter = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(self._body_splitter, 1)

        self._stack = QStackedWidget()
        self._body_splitter.addWidget(self._stack)

        self._help_view = self._build_help_view()
        self._stack.addWidget(self._help_view)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Chá»n", "STT", "Video", "Tráº¡ng thÃ¡i", "Mode", "Prompt"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setAlternatingRowColors(True)
        try:
            self.table.verticalHeader().setDefaultSectionSize(120)
        except Exception:
            pass

        hdr = _SelectAllHeader(self)
        self.table.setHorizontalHeader(hdr)
        try:
            hdr.setFixedHeight(34)
        except Exception:
            pass
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        # Column sizes: FIXED PIXELS (no ratio / no stretch)
        # Apply AFTER custom header is installed (Qt can reset widths when header changes).
        self._col_widths = {
            self.COL_CHECK: 32,   # checkbox
            self.COL_STT: 46,     # STT
            self.COL_VIDEO: 120,  # Video
            self.COL_STATUS: 128, # Tráº¡ng thÃ¡i
            self.COL_MODE: 172,   # Mode
            self.COL_PROMPT: 220, # Prompt
        }

        def _apply_col_widths() -> None:
            try:
                for i, w in self._col_widths.items():
                    hdr.resizeSection(int(i), int(w))
            except Exception:
                pass

        try:
            QTimer.singleShot(0, _apply_col_widths)
        except Exception:
            _apply_col_widths()

        # Header checkbox is painted centered by _SelectAllHeader.
        try:
            h0 = self.table.horizontalHeaderItem(0)
            if h0 is not None:
                h0.setText("")
        except Exception:
            pass
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.itemChanged.connect(self._on_table_item_changed)
        self._stack.addWidget(self.table)

        self._log_group = QGroupBox("Nháº­t kÃ½ cháº¡y")
        self._log_group.setStyleSheet("QGroupBox{font-weight:800;}")
        log_layout = QVBoxLayout(self._log_group)
        log_layout.setContentsMargins(8, 8, 8, 8)
        log_layout.setSpacing(6)

        self._run_log = QPlainTextEdit()
        self._run_log.setReadOnly(True)
        self._run_log.setMinimumHeight(78)
        self._run_log.setStyleSheet("background:#1e1e1e; color:#dcdcdc; border:1px solid #333;")
        log_layout.addWidget(self._run_log)

        self._body_splitter.addWidget(self._log_group)
        try:
            self._body_splitter.setStretchFactor(0, 4)
            self._body_splitter.setStretchFactor(1, 1)
            QTimer.singleShot(0, lambda: self._body_splitter.setSizes([640, 90]))
        except Exception:
            pass

        self._account_group = QGroupBox("ThÃ´ng tin tÃ i khoáº£n")
        self._account_group.setStyleSheet(
            "QGroupBox{font-weight:800; color:#1f2d48; border:1px solid #c8d7f2; border-radius:10px; margin-top:6px; background:#eaf2ff;}"
            "QGroupBox::title{subcontrol-origin:margin; left:10px; padding:0 4px; color:#1f2d48;}"
        )
        self._account_group.setMinimumHeight(72)
        self._account_group.setMaximumHeight(86)
        account_layout = QHBoxLayout(self._account_group)
        account_layout.setContentsMargins(12, 8, 12, 8)
        account_layout.setSpacing(18)

        lb_account = QLabel("TÃ i khoáº£n:")
        lb_account.setStyleSheet("font-weight:800; color:#1f2d48;")
        self._account_value = QLabel("Default")
        self._account_value.setStyleSheet("color:#334155;")

        lb_type = QLabel("Loáº¡i tÃ i khoáº£n:")
        lb_type.setStyleSheet("font-weight:800; color:#1f2d48;")
        self._account_type_value = QLabel("VIP1")
        self._account_type_value.setStyleSheet("color:#334155;")

        lb_expiry = QLabel("NgÃ y háº¿t háº¡n:")
        lb_expiry.setStyleSheet("font-weight:800; color:#1f2d48;")
        self._account_expiry_value = QLabel("-")
        self._account_expiry_value.setStyleSheet("color:#334155;")

        account_layout.addWidget(lb_account)
        account_layout.addWidget(self._account_value)
        account_layout.addSpacing(8)
        account_layout.addWidget(lb_type)
        account_layout.addWidget(self._account_type_value)
        account_layout.addSpacing(8)
        account_layout.addWidget(lb_expiry)
        account_layout.addWidget(self._account_expiry_value)
        account_layout.addStretch(1)

        layout.addWidget(self._account_group, 0)

        self._update_empty_state()
        self._ensure_status_snapshot_loaded()
        self._update_status_summary()
        self._refresh_account_info()

    def _format_expiry_date(self, raw_value) -> str:
        try:
            val = int(raw_value or 0)
        except Exception:
            val = 0
        if val <= 0:
            return "-"
        try:
            dt = datetime.fromtimestamp(val)
            return dt.strftime("%d/%m/%Y")
        except Exception:
            return "-"

    def _extract_license_account_and_type(self, state_data: dict) -> tuple[str, str]:
        account_value = ""
        type_value = ""
        if not isinstance(state_data, dict):
            return "", ""

        raw_features = state_data.get("features")
        payload = None
        if isinstance(raw_features, dict):
            payload = raw_features
        elif isinstance(raw_features, str):
            txt = raw_features.strip()
            if txt:
                try:
                    decoded = json.loads(txt)
                    if isinstance(decoded, dict):
                        payload = decoded
                except Exception:
                    payload = None

        if isinstance(payload, dict):
            account_value = str(payload.get("account") or "").strip()
            type_value = str(payload.get("type") or payload.get("account_type") or "").strip()

        if not account_value:
            account_value = str(state_data.get("account") or "").strip()
        if not type_value:
            type_value = str(state_data.get("type") or state_data.get("account_type") or "").strip()

        return account_value, type_value

    def _refresh_account_info(self) -> None:
        account_value = "Default"
        type_value = "VIP1"
        expiry_value = "-"

        try:
            state_path = Path(DATA_GENERAL_DIR) / "license_state.json"
            if state_path.exists():
                with open(state_path, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                if isinstance(state_data, dict):
                    parsed_account, parsed_type = self._extract_license_account_and_type(state_data)
                    if parsed_account:
                        account_value = parsed_account
                    if parsed_type:
                        type_value = parsed_type
                    expiry_value = self._format_expiry_date(state_data.get("expires_at"))
        except Exception:
            pass

        try:
            self._account_value.setText(account_value)
            self._account_type_value.setText(type_value)
            self._account_expiry_value.setText(expiry_value)
        except Exception:
            pass

    def _append_run_log(self, message: str) -> None:
        if self._run_log is None:
            return
        text = str(message or "")
        if "Äang chá»" in text and "video hoÃ n thÃ nh" in text:
            waiting = self._count_waiting_completion_on_table()
            if waiting <= 0:
                return
            text = f"â³ Äang chá» {waiting} video hoÃ n thÃ nh..."
        ts = datetime.now().strftime("%H:%M:%S")
        self._run_log.appendPlainText(f"[{ts}] {text}")

    def _open_usage_guide_file(self) -> None:
        try:
            guide_path = get_status_help_file_path()
            ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(guide_path)))
            if not ok:
                QMessageBox.warning(self, "HÆ°á»›ng dáº«n", f"KhÃ´ng má»Ÿ Ä‘Æ°á»£c file hÆ°á»›ng dáº«n:\n{guide_path}")
        except Exception as exc:
            QMessageBox.warning(self, "HÆ°á»›ng dáº«n", f"KhÃ´ng má»Ÿ Ä‘Æ°á»£c file hÆ°á»›ng dáº«n: {exc}")

    def append_run_log(self, message: str) -> None:
        self._append_run_log(message)

    def _count_waiting_completion_on_table(self) -> int:
        # Theo yÃªu cáº§u: khÃ´ng tÃ­nh video Ä‘ang chá» táº¡o (PENDING).
        count = 0
        for r in range(self.table.rowCount()):
            code = self._status_code(r)
            if code in {"TOKEN", "REQUESTED", "ACTIVE", "DOWNLOADING"}:
                count += 1
        return count

    def _build_help_view(self) -> QWidget:
        return build_status_help_view()

    def _update_empty_state(self) -> None:
        try:
            empty = int(self.table.rowCount() or 0) <= 0
        except Exception:
            empty = True
        try:
            self._stack.setCurrentIndex(0 if empty else 1)
        except Exception:
            pass
        try:
            has_log = bool(str(self._run_log.toPlainText() or "").strip()) if self._run_log is not None else False
            show_log = bool((not empty) or self.isRunning() or has_log)
            self._log_group.setVisible(show_log)
        except Exception:
            pass

    def _row_checked(self, row: int) -> bool:
        it = self.table.item(int(row), self.COL_CHECK)
        if it is None:
            return False
        try:
            return bool(it.data(Qt.ItemDataRole.UserRole) is True)
        except Exception:
            return False

    def _set_row_checked(self, row: int, checked: bool) -> None:
        r = int(row)
        it = self.table.item(r, self.COL_CHECK)
        if it is None:
            return
        want = bool(checked)
        try:
            it.setData(Qt.ItemDataRole.UserRole, want)
        except Exception:
            pass
        # Update the visual widget if present
        w = self.table.cellWidget(r, self.COL_CHECK)
        if w is not None:
            btn = getattr(w, "_cb_btn", None)
            if isinstance(btn, QToolButton):
                self._apply_checkbox_button_state(btn, want)

    def _apply_checkbox_button_state(self, btn: QToolButton, checked: bool) -> None:
        if self._use_checkbox_icon:
            ic = self._cb_on if bool(checked) else self._cb_off
            if ic is not None and not ic.isNull():
                btn.setText("")
                btn.setStyleSheet("")
                btn.setIcon(ic)
                return

        btn.setIcon(QIcon())
        btn.setText("âœ“" if bool(checked) else "")
        if checked:
            btn.setStyleSheet(
                "QToolButton {border: 1px solid #16a34a; border-radius: 3px; background: #16a34a; color: white; font-weight: 800;}"
            )
        else:
            btn.setStyleSheet(
                "QToolButton {border: 1px solid #16a34a; border-radius: 3px; background: transparent; color: #16a34a; font-weight: 800;}"
            )

    def _toggle_row_checked(self, row: int) -> None:
        self._set_row_checked(int(row), not self._row_checked(int(row)))
        self._sync_select_all_header()

    def _sync_select_all_header(self) -> None:
        # Update header icon state based on all rows.
        total = int(self.table.rowCount() or 0)
        if total <= 0:
            all_checked = False
        else:
            all_checked = True
            for r in range(total):
                if not self._row_checked(r):
                    all_checked = False
                    break

        self._select_all_checked = bool(all_checked)
        try:
            hdr = self.table.horizontalHeader()
            if isinstance(hdr, _SelectAllHeader):
                hdr.sync_from_panel()
            else:
                hdr.viewport().update()
        except Exception:
            pass

    def _on_header_clicked(self, section: int) -> None:
        if int(section) != 0:
            return
        # Toggle select-all
        want = not bool(self._select_all_checked)
        for r in range(self.table.rowCount()):
            self._set_row_checked(r, want)
        self._sync_select_all_header()

    def isRunning(self) -> bool:
        try:
            wf_running = any(bool(wf and wf.isRunning()) for wf in list(self._workflows))
            idea_running = bool(self._idea_worker and self._idea_worker.isRunning())
            return bool(wf_running or idea_running)
        except Exception:
            return False

    def get_running_video_count(self) -> int:
        if not self.isRunning():
            return 0
        running_codes = {"TOKEN", "REQUESTED", "ACTIVE", "DOWNLOADING"}
        count = 0
        rows = sorted(int(r) for r in list(self._active_queue_rows)) if self._active_queue_rows else list(range(self.table.rowCount()))
        for r in rows:
            try:
                if self._status_code(r) in running_codes:
                    count += 1
            except Exception:
                pass
        return int(count)

    def stop(self) -> None:
        self._global_stop_requested = True
        self._retry_mode_queue = []
        self._active_queue_rows.clear()
        self._awaiting_completion_confirmation = False
        self._completion_poll_scheduled = False
        self._completion_poll_attempts = 0
        self._append_run_log("ðŸ›‘ Äang dá»«ng workflow...")
        if self._idea_worker is not None:
            try:
                self._idea_worker.stop()
                self._append_run_log("ðŸ›‘ Äang dá»«ng Idea to Video...")
            except Exception:
                pass
        if self._workflow is not None:
            try:
                self._workflow.stop()
                self._workflow.requestInterruption()
            except Exception:
                pass
        for wf in list(self._workflows):
            try:
                wf.stop()
                wf.requestInterruption()
            except Exception:
                pass
        self._mark_active_rows_stopped()
        self._refresh_pending_positions()
        self.requestStop.emit()
        try:
            self._stop_poll_attempts = 0
            QTimer.singleShot(150, self._poll_stop_state)
        except Exception:
            pass

    def shutdown(self, timeout_ms: int = 2200) -> None:
        self._active_queue_rows.clear()
        self._awaiting_completion_confirmation = False
        self._completion_poll_scheduled = False
        self._completion_poll_attempts = 0
        idea = self._idea_worker
        if idea is not None:
            try:
                idea.stop()
                idea.requestInterruption()
            except Exception:
                pass

        workflows = list(self._workflows)
        for wf in workflows:
            try:
                wf.stop()
                wf.requestInterruption()
            except Exception:
                pass

        for thread_obj in ([idea] + workflows):
            if thread_obj is None:
                continue
            try:
                if thread_obj.isRunning():
                    thread_obj.wait(max(200, int(timeout_ms // 2)))
                if thread_obj.isRunning():
                    thread_obj.terminate()
                    thread_obj.wait(400)
            except Exception:
                pass

        self._idea_worker = None
        self._workflow = None
        self._workflows = []

    def _poll_stop_state(self) -> None:
        idea = self._idea_worker
        if idea is not None:
            try:
                if not idea.isRunning():
                    self._idea_worker = None
            except Exception:
                self._idea_worker = None

        alive_workflows: list[QThread] = []
        for wf in list(self._workflows):
            try:
                if wf and wf.isRunning():
                    alive_workflows.append(wf)
            except Exception:
                pass
        self._workflows = alive_workflows
        self._workflow = self._workflows[-1] if self._workflows else None

        wf = self._workflow
        if (not self._workflows) and self._idea_worker is None:
            self.runStateChanged.emit(False)
            return

        if not self._workflows:
            self._stop_poll_attempts += 1
            if self._stop_poll_attempts >= 40:
                self.runStateChanged.emit(False)
                return
            try:
                QTimer.singleShot(200, self._poll_stop_state)
            except Exception:
                pass
            return

        try:
            if not wf.isRunning():
                self._finalize_stop_if_finished()
                return
        except Exception:
            self._finalize_stop_if_finished()
            return

        self._stop_poll_attempts += 1
        if self._stop_poll_attempts >= 40:
            self._append_run_log("âš ï¸ Workflow chÆ°a thoÃ¡t ká»‹p, UI váº«n hoáº¡t Ä‘á»™ng vÃ  sáº½ nháº­n tráº¡ng thÃ¡i dá»«ng.")
            self._workflow = None
            self._workflows = []
            self.runStateChanged.emit(False)
            return

        try:
            QTimer.singleShot(200, self._poll_stop_state)
        except Exception:
            pass

    def _finalize_stop_if_finished(self) -> None:
        alive_workflows: list[QThread] = []
        for wf in list(self._workflows):
            try:
                if wf and wf.isRunning():
                    alive_workflows.append(wf)
            except Exception:
                pass
        self._workflows = alive_workflows
        self._workflow = self._workflows[-1] if self._workflows else None
        if not self._workflows:
            self.runStateChanged.emit(False)
            return
        self._append_run_log("âœ… ÄÃ£ dá»«ng workflow")

    def _mark_rows_pending(self, rows: list[int]) -> None:
        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()

    def enqueue_text_to_video(self, prompts: list[str]) -> dict | None:
        self._ensure_status_snapshot_loaded()
        clean_prompts = [str(p or "").strip() for p in (prompts or []) if str(p or "").strip()]
        if not clean_prompts:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ prompt", "HÃ£y nháº­p Ã­t nháº¥t má»™t prompt.")
            return None

        rows: list[int] = []
        for prompt_text in clean_prompts:
            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(row, self.MODE_TEXT_TO_VIDEO, payload={"prompt": prompt_text})
            rows.append(row)

        self._sync_stt_and_prompt_ids()
        self._snapshot_output_count_for_rows(rows)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_TEXT_TO_VIDEO, "rows": rows, "label": "VEO3 - Text to Video"}

    def enqueue_grok_text_to_video(self, prompts: list[str]) -> dict | None:
        self._ensure_status_snapshot_loaded()
        clean_prompts = [str(p or "").strip() for p in (prompts or []) if str(p or "").strip()]
        if not clean_prompts:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ prompt", "HÃ£y nháº­p Ã­t nháº¥t má»™t prompt GROK.")
            return None

        rows: list[int] = []
        for prompt_text in clean_prompts:
            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(row, self.MODE_GROK_TEXT_TO_VIDEO, payload={"prompt": prompt_text})
            rows.append(row)

        self._sync_stt_and_prompt_ids()
        self._set_output_count_for_rows(rows, 1)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_GROK_TEXT_TO_VIDEO, "rows": rows, "label": "GROK Text to Video"}

    def enqueue_grok_image_to_video(self, items: list[dict]) -> dict | None:
        self._ensure_status_snapshot_loaded()

        rows: list[int] = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue

            prompt_text = str(raw.get("prompt") or raw.get("description") or "").strip()
            image_link = str(raw.get("image_link") or raw.get("image") or raw.get("start_image_link") or "").strip()
            if not image_link:
                continue

            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(
                row,
                self.MODE_GROK_IMAGE_TO_VIDEO,
                payload={
                    "prompt": prompt_text,
                    "image_link": image_link,
                },
            )
            rows.append(row)

        if not rows:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ dá»¯ liá»‡u", "KhÃ´ng cÃ³ dá»¯ liá»‡u GROK Image to Video há»£p lá»‡.")
            self._update_empty_state()
            return None

        self._sync_stt_and_prompt_ids()
        self._set_output_count_for_rows(rows, 1)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_GROK_IMAGE_TO_VIDEO, "rows": rows, "label": "GROK Image to Video"}

    def enqueue_image_to_video(self, items: list[dict], mode: str = "single") -> dict | None:
        self._ensure_status_snapshot_loaded()
        normalized_mode = "start_end" if str(mode or "").strip().lower() == "start_end" else "single"

        rows: list[int] = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue

            prompt_text = str(raw.get("prompt") or raw.get("description") or "").strip()
            row = self.table.rowCount()
            self._add_row(row, prompt_text)

            if normalized_mode == "start_end":
                start_image_link = str(raw.get("start_image_link") or raw.get("image_link") or raw.get("image") or "").strip()
                end_image_link = str(raw.get("end_image_link") or raw.get("end_image") or "").strip()
                self._set_row_mode_meta(
                    row,
                    self.MODE_IMAGE_TO_VIDEO_START_END,
                    payload={
                        "prompt": prompt_text,
                        "start_image_link": start_image_link,
                        "end_image_link": end_image_link,
                    },
                )
            else:
                image_link = str(raw.get("image_link") or raw.get("image") or raw.get("start_image_link") or "").strip()
                self._set_row_mode_meta(
                    row,
                    self.MODE_IMAGE_TO_VIDEO_SINGLE,
                    payload={
                        "prompt": prompt_text,
                        "image_link": image_link,
                    },
                )

            rows.append(row)

        if not rows:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ dá»¯ liá»‡u", "KhÃ´ng cÃ³ dá»¯ liá»‡u Image to Video há»£p lá»‡.")
            self._update_empty_state()
            return None

        self._sync_stt_and_prompt_ids()
        self._snapshot_output_count_for_rows(rows)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        mode_key = self.MODE_IMAGE_TO_VIDEO_START_END if normalized_mode == "start_end" else self.MODE_IMAGE_TO_VIDEO_SINGLE
        mode_label = "VEO3 - Image to Video (áº¢nh Ä‘áº§u-cuá»‘i)" if normalized_mode == "start_end" else "VEO3 - Image to Video"
        return {"mode_key": mode_key, "rows": rows, "label": mode_label}

    def enqueue_generate_image_from_prompts(self, items: list[dict]) -> dict | None:
        self._ensure_status_snapshot_loaded()
        rows: list[int] = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            prompt_text = str(raw.get("description") or raw.get("prompt") or "").strip()
            if not prompt_text:
                continue
            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(row, self.MODE_CREATE_IMAGE_PROMPT, payload={"description": prompt_text})
            rows.append(row)

        if not rows:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ dá»¯ liá»‡u", "KhÃ´ng cÃ³ prompt há»£p lá»‡ Ä‘á»ƒ táº¡o áº£nh.")
            self._update_empty_state()
            return None

        self._sync_stt_and_prompt_ids()
        self._snapshot_output_count_for_rows(rows)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_CREATE_IMAGE_PROMPT, "rows": rows, "label": "VEO3 - Táº¡o áº£nh tá»« prompt"}

    def enqueue_generate_image_from_references(self, prompts: list[str], characters: list[dict]) -> dict | None:
        self._ensure_status_snapshot_loaded()
        clean_prompts = [str(p or "").strip() for p in (prompts or []) if str(p or "").strip()]
        clean_characters: list[dict] = []
        for ch in characters or []:
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            path = str(ch.get("path") or "").strip()
            if name and path:
                clean_characters.append({"name": name, "path": path})

        if not clean_prompts:
            QMessageBox.warning(self, "Thiáº¿u prompt", "KhÃ´ng cÃ³ prompt há»£p lá»‡ Ä‘á»ƒ cháº¡y Táº¡o áº¢nh Tá»« áº¢nh Tham Chiáº¿u.")
            return None
        if not clean_characters:
            QMessageBox.warning(self, "Thiáº¿u áº£nh tham chiáº¿u", "KhÃ´ng cÃ³ áº£nh tham chiáº¿u há»£p lá»‡ Ä‘á»ƒ cháº¡y Táº¡o áº¢nh Tá»« áº¢nh Tham Chiáº¿u.")
            return None

        rows: list[int] = []
        for prompt_text in clean_prompts:
            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(
                row,
                self.MODE_CREATE_IMAGE_REFERENCE,
                payload={"prompt": prompt_text, "characters": list(clean_characters)},
            )
            rows.append(row)

        self._sync_stt_and_prompt_ids()
        self._snapshot_output_count_for_rows(rows)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_CREATE_IMAGE_REFERENCE, "rows": rows, "label": "VEO3 - Táº¡o áº£nh tá»« áº£nh tham chiáº¿u"}

    def enqueue_character_sync(self, prompts: list[str], characters: list[dict]) -> dict | None:
        self._ensure_status_snapshot_loaded()
        clean_prompts = [str(p or "").strip() for p in (prompts or []) if str(p or "").strip()]
        clean_characters: list[dict] = []
        for ch in characters or []:
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            path = str(ch.get("path") or "").strip()
            if name and path:
                clean_characters.append({"name": name, "path": path})

        if not clean_prompts:
            QMessageBox.warning(self, "Thiáº¿u prompt", "KhÃ´ng cÃ³ prompt há»£p lá»‡ Ä‘á»ƒ cháº¡y Äá»“ng bá»™ nhÃ¢n váº­t.")
            return None
        if not clean_characters:
            QMessageBox.warning(self, "Thiáº¿u áº£nh nhÃ¢n váº­t", "KhÃ´ng cÃ³ áº£nh nhÃ¢n váº­t há»£p lá»‡ Ä‘á»ƒ cháº¡y Äá»“ng bá»™ nhÃ¢n váº­t.")
            return None

        rows: list[int] = []
        for prompt_text in clean_prompts:
            row = self.table.rowCount()
            self._add_row(row, prompt_text)
            self._set_row_mode_meta(
                row,
                self.MODE_CHARACTER_SYNC,
                payload={"prompt": prompt_text, "characters": list(clean_characters)},
            )
            rows.append(row)

        self._sync_stt_and_prompt_ids()
        self._snapshot_output_count_for_rows(rows)
        self._update_empty_state()
        self._mark_rows_pending(rows)
        return {"mode_key": self.MODE_CHARACTER_SYNC, "rows": rows, "label": "VEO3 - Äá»“ng bá»™ nhÃ¢n váº­t"}

    def start_queued_job(self, mode_key: str, rows: list[int]) -> bool:
        self._global_stop_requested = False
        clean_rows = [int(r) for r in (rows or [])]
        if not clean_rows:
            return False
        self._retry_mode_queue = []
        started = self._start_mode_group(str(mode_key or ""), clean_rows)
        if started:
            self._active_queue_rows = {int(r) for r in clean_rows}
            self._awaiting_completion_confirmation = False
            self._completion_poll_attempts = 0
            self._completion_poll_scheduled = False
        return bool(started)

    def start_text_to_video(self, prompts: list[str]) -> None:
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return
        payload = self.enqueue_text_to_video(prompts)
        if not payload:
            return
        self.start_queued_job(str(payload.get("mode_key") or ""), list(payload.get("rows") or []))

    def start_image_to_video(self, items: list[dict], mode: str = "single") -> None:
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return
        payload = self.enqueue_image_to_video(items, mode=mode)
        if not payload:
            return
        self.start_queued_job(str(payload.get("mode_key") or ""), list(payload.get("rows") or []))

    def start_generate_image_from_prompts(self, items: list[dict]) -> None:
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return
        payload = self.enqueue_generate_image_from_prompts(items)
        if not payload:
            return
        self.start_queued_job(str(payload.get("mode_key") or ""), list(payload.get("rows") or []))

    def start_generate_image_from_references(self, prompts: list[str], characters: list[dict]) -> None:
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return
        payload = self.enqueue_generate_image_from_references(prompts, characters)
        if not payload:
            return
        self.start_queued_job(str(payload.get("mode_key") or ""), list(payload.get("rows") or []))

    def start_character_sync(self, prompts: list[str], characters: list[dict]) -> None:
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return
        payload = self.enqueue_character_sync(prompts, characters)
        if not payload:
            return
        self.start_queued_job(str(payload.get("mode_key") or ""), list(payload.get("rows") or []))

    def _add_row(self, row: int, prompt: str) -> None:
        self.table.insertRow(row)
        try:
            self.table.setRowHeight(int(row), 120)
        except Exception:
            pass

        # Column 0: keep an item for selection + store checked state in UserRole,
        # and place a centered toolbutton widget for the icon.
        chk_item = QTableWidgetItem("")
        chk_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk_item.setText("")
        chk_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        chk_item.setData(Qt.ItemDataRole.UserRole, False)
        self.table.setItem(row, self.COL_CHECK, chk_item)

        w = QWidget()
        w.setContentsMargins(0, 0, 0, 0)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        btn = QToolButton()
        btn.setAutoRaise(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(22, 22)
        btn.setIconSize(btn.size())
        self._apply_checkbox_button_state(btn, False)
        btn.clicked.connect(lambda _=False, cell_widget=w: self._toggle_row_checked_by_widget(cell_widget))
        lay.addStretch(1)
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(1)
        setattr(w, "_cb_btn", btn)
        self.table.setCellWidget(row, self.COL_CHECK, w)

        stt = QTableWidgetItem(f"{row+1:03d}")
        stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, self.COL_STT, stt)

        st = QTableWidgetItem("Sáºµn sÃ ng")
        st.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        st.setData(Qt.ItemDataRole.UserRole, "READY")
        self.table.setItem(row, self.COL_STATUS, st)

        mode_item = QTableWidgetItem(self._mode_label(self.MODE_TEXT_TO_VIDEO))
        mode_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        mode_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, self.COL_MODE, mode_item)

        pr = QTableWidgetItem(str(prompt or ""))
        pr.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        pr.setData(Qt.ItemDataRole.UserRole, str(int(row) + 1))
        pr.setForeground(QBrush(QColor("transparent")))
        self.table.setItem(row, self.COL_PROMPT, pr)
        self._set_row_mode_meta(
            row,
            self.MODE_TEXT_TO_VIDEO,
            payload={"prompt": str(prompt or "").strip()},
        )
        self._setup_prompt_cell(row)

        # Video column placeholder (blank until video thumbnail is available).
        vid_item = QTableWidgetItem("")
        vid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            vid_item.setData(Qt.ItemDataRole.UserRole, "")
            vid_item.setData(Qt.ItemDataRole.UserRole + 1, {})
            vid_item.setData(Qt.ItemDataRole.UserRole + 2, 1)
            vid_item.setData(Qt.ItemDataRole.UserRole + 3, self._expected_output_count())
            vid_item.setData(Qt.ItemDataRole.UserRole + 4, {})
        except Exception:
            pass
        self.table.setItem(row, self.COL_VIDEO, vid_item)
        self._setup_video_cell(row)

        self._sync_select_all_header()
        self._update_empty_state()
        if not self._loading_status_snapshot:
            self._save_status_snapshot()
            self._update_status_summary()

    def _find_row_by_cell_widget(self, column: int, widget: QWidget | None) -> int:
        if widget is None:
            return -1
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, int(column)) is widget:
                return r
        return -1

    def _toggle_row_checked_by_widget(self, widget: QWidget | None) -> None:
        row = self._find_row_by_cell_widget(self.COL_CHECK, widget)
        if row < 0:
            return
        self._toggle_row_checked(int(row))

    def _edit_prompt_by_widget(self, widget: QWidget | None) -> None:
        row = self._find_row_by_cell_widget(self.COL_PROMPT, widget)
        if row < 0:
            return
        self._open_prompt_editor(int(row))

    def _setup_prompt_cell(self, row: int) -> None:
        wrap = QWidget()
        wrap.setContentsMargins(0, 0, 0, 0)
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(0)

        btn = QPushButton("Sá»­a")
        btn.setObjectName("TopAction")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedHeight(22)
        btn.setStyleSheet("padding: 0 10px; font-weight:700;")
        btn.clicked.connect(lambda _=False, cell_widget=wrap: self._edit_prompt_by_widget(cell_widget))
        top_row.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        top_row.addStretch(1)

        lbl = QLabel("")
        lbl.setObjectName("PromptCellLabel")
        lbl.setStyleSheet("color:#111827;")
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay.addLayout(top_row)
        lay.addWidget(lbl, 1)
        setattr(wrap, "_prompt_label", lbl)
        setattr(wrap, "_prompt_btn", btn)
        self.table.setCellWidget(int(row), self.COL_PROMPT, wrap)
        self._refresh_prompt_cell(int(row))

    def _refresh_prompt_cell(self, row: int) -> None:
        cell = self.table.cellWidget(int(row), self.COL_PROMPT)
        if cell is None:
            return
        lbl = getattr(cell, "_prompt_label", None)
        if not isinstance(lbl, QLabel):
            return
        btn = getattr(cell, "_prompt_btn", None)
        item = self.table.item(int(row), self.COL_PROMPT)
        if item is not None:
            try:
                item.setForeground(QBrush(QColor("transparent")))
            except Exception:
                pass
        txt = str((item.text() if item is not None else "") or "").strip()
        lbl.setText(txt)
        lbl.setToolTip(txt)

    def _open_prompt_editor(self, row: int) -> None:
        item = self.table.item(int(row), self.COL_PROMPT)
        if item is None:
            return
        current_text = str(item.text() or "")

        dlg = QDialog(self)
        dlg.setWindowTitle("Sá»­a Prompt")
        dlg.setModal(True)
        dlg.resize(760, 420)
        dlg.setStyleSheet(
            "QDialog{background:#f8fafc;}"
            "QLabel{color:#0f172a;font-weight:700;}"
            "QTextEdit{background:#ffffff;border:1px solid #d1d5db;border-radius:8px;padding:8px;color:#111827;}"
            "QPushButton{min-height:32px;border-radius:8px;padding:0 14px;font-weight:700;}"
            "QPushButton#okBtn{background:#2563eb;color:white;}"
            "QPushButton#cancelBtn{background:#e5e7eb;color:#111827;}"
        )

        root = QVBoxLayout(dlg)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        root.addWidget(QLabel("Prompt hiá»‡n táº¡i / chá»‰nh sá»­a:"))

        editor = QTextEdit()
        editor.setPlainText(current_text)
        root.addWidget(editor, 1)

        buttons = QDialogButtonBox()
        ok_btn = buttons.addButton("XÃ¡c nháº­n", QDialogButtonBox.ButtonRole.AcceptRole)
        ok_btn.setObjectName("okBtn")
        cancel_btn = buttons.addButton("Há»§y", QDialogButtonBox.ButtonRole.RejectRole)
        cancel_btn.setObjectName("cancelBtn")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        root.addWidget(buttons)

        if dlg.exec() != int(QDialog.DialogCode.Accepted):
            return

        new_text = str(editor.toPlainText() or "").strip()
        if not new_text or new_text == current_text.strip():
            return

        item.setText(new_text)
        payload = self._row_mode_payload(int(row))
        if isinstance(payload, dict):
            if "description" in payload:
                payload["description"] = new_text
            else:
                payload["prompt"] = new_text
            self._set_row_mode_meta(int(row), self._row_mode_key(int(row)), payload=payload)
        self._refresh_prompt_cell(int(row))
        self._save_status_snapshot()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item is None:
            return
        if int(item.column()) != self.COL_PROMPT:
            return
        row = int(item.row())
        self._refresh_prompt_cell(row)
        if not self._loading_status_snapshot:
            self._save_status_snapshot()

    def _on_cell_clicked(self, row: int, col: int) -> None:
        # Column 0 uses a toolbutton; ignore clicks on the cell background.
        if int(col) == self.COL_CHECK:
            return
        if int(col) != self.COL_VIDEO:
            return
        it = self.table.item(int(row), self.COL_VIDEO)
        try:
            path = str(it.data(Qt.ItemDataRole.UserRole) or "").strip() if it is not None else ""
        except Exception:
            path = ""
        if not path or not os.path.isfile(path):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _setup_video_cell(self, row: int) -> None:
        vw = QWidget()
        vw.setContentsMargins(0, 0, 0, 0)
        vlay = QVBoxLayout(vw)
        vlay.setContentsMargins(0, 0, 0, 0)
        vlay.setSpacing(0)

        preview = QWidget(vw)
        preview.setContentsMargins(0, 0, 0, 0)
        pv_lay = QVBoxLayout(preview)
        pv_lay.setContentsMargins(0, 0, 0, 0)
        pv_lay.setSpacing(0)

        buttons = []
        expected_outputs = self._expected_output_count()
        for idx in range(1, 5):
            b = QPushButton(str(idx), preview)
            b.setFixedSize(20, 20)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("border-radius:10px; font-size:10px; padding:0px;")
            should_show = expected_outputs >= 2 and idx <= expected_outputs
            b.setVisible(should_show)
            b.setEnabled(False)
            if should_show:
                b.setStyleSheet("border-radius:10px; font-size:10px; padding:0px; background:#f3f4f6; color:#9ca3af;")
            b.clicked.connect(lambda _=False, cell_widget=vw, n=idx: self._select_video_output_by_widget(cell_widget, n))
            buttons.append(b)

        vlabel = QLabel("", preview)
        vlabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vlabel.setStyleSheet("color:#1f2d48; font-weight:700;")
        vlabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pv_lay.addWidget(vlabel, 1)
        vlay.addWidget(preview, 1)
        preview.installEventFilter(self)
        setattr(vw, "_vid_label", vlabel)
        setattr(vw, "_vid_buttons", buttons)
        setattr(vw, "_vid_preview", preview)
        self.table.setCellWidget(row, self.COL_VIDEO, vw)
        self._reposition_video_badges(preview)

    def _select_video_output_by_widget(self, widget: QWidget | None, output_index: int) -> None:
        row = self._find_row_by_cell_widget(self.COL_VIDEO, widget)
        if row < 0:
            return
        self._select_video_output(int(row), int(output_index))

    def eventFilter(self, watched, event):
        try:
            if event.type() == event.Type.Resize and isinstance(watched, QWidget):
                self._reposition_video_badges(watched)
        except Exception:
            pass
        return super().eventFilter(watched, event)

    def _reposition_video_badges(self, preview_widget: QWidget) -> None:
        row = -1
        for r in range(self.table.rowCount()):
            cell = self.table.cellWidget(r, self.COL_VIDEO)
            if cell is None:
                continue
            pv = getattr(cell, "_vid_preview", None)
            if pv is preview_widget:
                row = r
                break
        if row < 0:
            return
        cell = self.table.cellWidget(row, self.COL_VIDEO)
        if cell is None:
            return
        buttons = getattr(cell, "_vid_buttons", [])
        visible_btns = [b for b in buttons if isinstance(b, QPushButton) and b.isVisible()]
        if not visible_btns:
            return
        right = int(preview_widget.width()) - 4
        top = 4
        for b in reversed(visible_btns):
            b.move(right - b.width(), top)
            try:
                b.raise_()
            except Exception:
                pass
            right -= (b.width() + 4)

    def _select_video_output(self, row: int, output_index: int) -> None:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            return
        try:
            video_map = dict(it.data(Qt.ItemDataRole.UserRole + 1) or {})
        except Exception:
            video_map = {}
        try:
            preview_map = dict(it.data(Qt.ItemDataRole.UserRole + 4) or {})
        except Exception:
            preview_map = {}
        path = str(video_map.get(int(output_index), "") or "")
        if not path:
            return
        it.setData(Qt.ItemDataRole.UserRole + 2, int(output_index))
        it.setData(Qt.ItemDataRole.UserRole, path)
        preview_path = str(preview_map.get(int(output_index), "") or path)
        self._render_media_preview(row, preview_path)
        self._refresh_video_badges(row)
        try:
            if os.path.isfile(path):
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        except Exception:
            pass

    def _is_image_file(self, file_path: str) -> bool:
        ext = str(Path(str(file_path or "")).suffix or "").lower()
        return ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

    def _refresh_video_badges(self, row: int) -> None:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            return
        try:
            video_map = dict(it.data(Qt.ItemDataRole.UserRole + 1) or {})
        except Exception:
            video_map = {}
        try:
            selected_idx = int(it.data(Qt.ItemDataRole.UserRole + 2) or 1)
        except Exception:
            selected_idx = 1

        cell = self.table.cellWidget(int(row), self.COL_VIDEO)
        if cell is None:
            return
        buttons = getattr(cell, "_vid_buttons", [])
        expected_outputs = self._row_output_count(int(row))
        for i, btn in enumerate(buttons, start=1):
            should_show = expected_outputs >= 2 and i <= expected_outputs
            has_video = i in video_map
            btn.setVisible(should_show)
            btn.setEnabled(has_video)
            if should_show and has_video and i == selected_idx:
                btn.setStyleSheet("border-radius:10px; font-size:10px; padding:0px; background:#2563eb; color:#fff;")
            elif should_show and has_video:
                btn.setStyleSheet("border-radius:10px; font-size:10px; padding:0px; background:#e5e7eb; color:#111827;")
            elif should_show:
                btn.setStyleSheet("border-radius:10px; font-size:10px; padding:0px; background:#f3f4f6; color:#9ca3af;")
            try:
                btn.raise_()
            except Exception:
                pass
        preview = getattr(cell, "_vid_preview", None)
        if isinstance(preview, QWidget):
            self._reposition_video_badges(preview)

    def _expected_output_count(self) -> int:
        try:
            val = int(getattr(self._cfg, "output_count", 1) or 1)
        except Exception:
            val = 1
        if val < 1:
            val = 1
        if val > 4:
            val = 4
        return val

    def _row_output_count(self, row: int) -> int:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is not None:
            try:
                v = int(it.data(Qt.ItemDataRole.UserRole + 3) or 0)
                if 1 <= v <= 4:
                    return v
            except Exception:
                pass
        return self._expected_output_count()

    def _snapshot_output_count_for_rows(self, rows: list[int]) -> None:
        snap = self._expected_output_count()
        for r in rows:
            it = self.table.item(int(r), self.COL_VIDEO)
            if it is None:
                continue
            try:
                it.setData(Qt.ItemDataRole.UserRole + 3, int(snap))
            except Exception:
                pass
            self._refresh_video_badges(int(r))

    def _set_output_count_for_rows(self, rows: list[int], output_count: int) -> None:
        try:
            snap = int(output_count)
        except Exception:
            snap = 1
        if snap < 1:
            snap = 1
        if snap > 4:
            snap = 4
        for r in rows:
            it = self.table.item(int(r), self.COL_VIDEO)
            if it is None:
                continue
            try:
                it.setData(Qt.ItemDataRole.UserRole + 3, int(snap))
            except Exception:
                pass
            self._refresh_video_badges(int(r))

    def _render_video_preview(self, row: int, video_path: str) -> None:
        cell = self.table.cellWidget(int(row), self.COL_VIDEO)
        if cell is None:
            return
        label = getattr(cell, "_vid_label", None)
        if not isinstance(label, QLabel):
            return
        if not video_path or not os.path.isfile(video_path):
            label.setText("")
            label.setPixmap(QPixmap())
            return

        thumb_path = self._ensure_video_thumbnail(video_path)
        if thumb_path and os.path.isfile(thumb_path):
            self._render_image_preview(row, thumb_path)
            return

        label.setPixmap(QPixmap())
        label.setText("")

    def _on_thumbnail_ready(self, src_video_path: str, thumb_path: str) -> None:
        src = str(src_video_path or "").strip()
        thumb = str(thumb_path or "").strip()
        if not src or not thumb or not os.path.isfile(thumb):
            return
        try:
            src_norm = os.path.normcase(os.path.normpath(src))
        except Exception:
            src_norm = src

        for row in range(self.table.rowCount()):
            item = self.table.item(int(row), self.COL_VIDEO)
            if item is None:
                continue
            selected_path = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not selected_path:
                continue
            try:
                selected_norm = os.path.normcase(os.path.normpath(selected_path))
            except Exception:
                selected_norm = selected_path
            if selected_norm != src_norm:
                continue

            try:
                selected_idx = int(item.data(Qt.ItemDataRole.UserRole + 2) or 1)
            except Exception:
                selected_idx = 1
            try:
                preview_map = dict(item.data(Qt.ItemDataRole.UserRole + 4) or {})
            except Exception:
                preview_map = {}
            preview_map[int(selected_idx)] = thumb
            item.setData(Qt.ItemDataRole.UserRole + 4, preview_map)
            self._render_image_preview(int(row), thumb)

    def _ensure_video_thumbnail(self, video_path: str) -> str:
        src = str(video_path or "").strip()
        if not src or not os.path.isfile(src):
            return ""

        src_path = Path(src)
        thumb_path = src_path.with_suffix(src_path.suffix + ".thumb.jpg")

        try:
            src_parts = [str(p).lower() for p in src_path.parts]
            if "grok_video" in src_parts:
                base_dir = src_path.parent.parent if src_path.parent.parent else src_path.parent
                grok_thumb_dir = base_dir / "grok_thumnail"
                grok_thumb_dir.mkdir(parents=True, exist_ok=True)
                thumb_path = grok_thumb_dir / f"{src_path.stem}.thumb.jpg"
        except Exception:
            pass

        try:
            if thumb_path.is_file() and thumb_path.stat().st_mtime >= src_path.stat().st_mtime:
                return str(thumb_path)
        except Exception:
            pass

        src_key = str(src_path).lower()
        try:
            src_mtime = float(src_path.stat().st_mtime)
        except Exception:
            src_mtime = 0.0

        last_attempt = float(self._thumb_attempted_mtime.get(src_key, 0.0) or 0.0)
        if last_attempt >= src_mtime:
            return ""

        if src_key not in self._thumb_jobs_inflight:
            self._thumb_jobs_inflight.add(src_key)

            def _build_thumb_async() -> None:
                try:
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    cmd = [
                        str(ffmpeg_exe),
                        "-y",
                        "-ss",
                        "00:00:00.500",
                        "-i",
                        str(src_path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "3",
                        str(thumb_path),
                    ]
                    subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=12,
                        **_win_hidden_kwargs(),
                    )
                except Exception:
                    pass
                finally:
                    self._thumb_attempted_mtime[src_key] = src_mtime
                    self._thumb_jobs_inflight.discard(src_key)
                    try:
                        if thumb_path.is_file():
                            self.thumbnailReady.emit(str(src_path), str(thumb_path))
                    except Exception:
                        pass

            try:
                threading.Thread(target=_build_thumb_async, daemon=True).start()
            except Exception:
                self._thumb_attempted_mtime[src_key] = src_mtime
                self._thumb_jobs_inflight.discard(src_key)
        return ""

    def _render_image_preview(self, row: int, image_path: str) -> None:
        cell = self.table.cellWidget(int(row), self.COL_VIDEO)
        if cell is None:
            return
        label = getattr(cell, "_vid_label", None)
        if not isinstance(label, QLabel):
            return
        if not image_path or not os.path.isfile(image_path):
            label.setText("")
            label.setPixmap(QPixmap())
            return
        pix = QPixmap(image_path)
        if pix.isNull():
            label.setText("")
            label.setPixmap(QPixmap())
            return
        target = label.size()
        if target.width() < 2 or target.height() < 2:
            target = label.parentWidget().size() if label.parentWidget() else target
        label.setPixmap(
            pix.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        label.setText("")

    def _render_media_preview(self, row: int, media_path: str) -> None:
        if self._is_image_file(media_path):
            self._render_image_preview(row, media_path)
            return
        self._render_video_preview(row, media_path)

    def _set_video_progress_text(self, row: int, progress: int) -> None:
        pct = max(0, min(100, int(progress or 0)))
        if self._row_media_map(int(row)):
            return

        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            it = QTableWidgetItem("")
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(int(row), self.COL_VIDEO, it)
        it.setText(f"{pct}%")

        cell = self.table.cellWidget(int(row), self.COL_VIDEO)
        if cell is None:
            return
        label = getattr(cell, "_vid_label", None)
        if not isinstance(label, QLabel):
            return
        label.setPixmap(QPixmap())
        label.setText(f"{pct}%")

    def _selected_rows(self) -> list[int]:
        rows: list[int] = []
        for r in range(self.table.rowCount()):
            if self._row_checked(r):
                rows.append(r)
        return rows

    def delete_selected_rows(self) -> None:
        picked = sorted(set(self._selected_rows()))
        if not picked:
            QMessageBox.warning(self, "ChÆ°a chá»n", "HÃ£y tÃ­ch chá»n cÃ¡c dÃ²ng cáº§n xÃ³a káº¿t quáº£.")
            return

        if QMessageBox.question(
            self,
            "XÃ¡c nháº­n",
            f"Báº¡n cÃ³ cháº¯c muá»‘n xÃ³a {len(picked)} káº¿t quáº£ Ä‘Ã£ chá»n?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        for r in reversed(picked):
            self.table.removeRow(r)

        self._sync_stt_and_prompt_ids()

        self._sync_select_all_header()
        self._update_empty_state()
        self._save_status_snapshot()
        self._update_status_summary()

    def _collect_prompts_from_rows(self, rows: list[int]) -> list[str]:
        prompts: list[str] = []
        for r in rows:
            it = self.table.item(int(r), self.COL_PROMPT)
            txt = (it.text() if it is not None else "") or ""
            txt = str(txt).strip()
            if txt:
                prompts.append(txt)
        return prompts

    def _next_prompt_id(self) -> str:
        max_id = 0
        for r in range(self.table.rowCount()):
            it = self.table.item(r, self.COL_PROMPT)
            if it is None:
                continue
            try:
                val = str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
                max_id = max(max_id, int(val))
            except Exception:
                continue
        return str(max_id + 1)

    def _mode_label(self, mode_key: str) -> str:
        key = str(mode_key or "").strip()
        labels = {
            self.MODE_TEXT_TO_VIDEO: "VEO3 - Táº¡o video tá»« vÄƒn báº£n",
            self.MODE_GROK_TEXT_TO_VIDEO: "GROK - Táº¡o video tá»« vÄƒn báº£n",
            self.MODE_GROK_IMAGE_TO_VIDEO: "GROK - Táº¡o video tá»« áº¢nh",
            self.MODE_IMAGE_TO_VIDEO_SINGLE: "VEO3 - Táº¡o video tá»« áº¢nh",
            self.MODE_IMAGE_TO_VIDEO_START_END: "VEO3 - Táº¡o video tá»« áº¢nh (Ä‘áº§u-cuá»‘i)",
            self.MODE_CHARACTER_SYNC: "VEO3 - Video Ä‘á»“ng nháº¥t nhÃ¢n váº­t",
            self.MODE_CREATE_IMAGE_PROMPT: "VEO3 - Táº¡o áº£nh tá»« prompt",
            self.MODE_CREATE_IMAGE_REFERENCE: "VEO3 - Táº¡o áº£nh tá»« áº£nh tham chiáº¿u",
        }
        return labels.get(key, "VEO3 - Táº¡o video tá»« vÄƒn báº£n")

    def _set_row_mode_meta(self, row: int, mode_key: str, payload: dict | None = None) -> None:
        prompt_item = self.table.item(int(row), self.COL_PROMPT)
        if prompt_item is None:
            return
        key = str(mode_key or self.MODE_TEXT_TO_VIDEO).strip() or self.MODE_TEXT_TO_VIDEO
        prompt_item.setData(Qt.ItemDataRole.UserRole + 1, key)
        mode_label = self._mode_label(key)
        prompt_item.setData(Qt.ItemDataRole.UserRole + 2, mode_label)
        prompt_item.setData(Qt.ItemDataRole.UserRole + 3, dict(payload or {}))
        mode_item = self.table.item(int(row), self.COL_MODE)
        if mode_item is None:
            mode_item = QTableWidgetItem(mode_label)
            mode_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            mode_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(int(row), self.COL_MODE, mode_item)
        else:
            mode_item.setText(mode_label)

    def _row_mode_key(self, row: int) -> str:
        prompt_item = self.table.item(int(row), self.COL_PROMPT)
        if prompt_item is None:
            return self.MODE_TEXT_TO_VIDEO
        try:
            key = str(prompt_item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        except Exception:
            key = ""
        return key or self.MODE_TEXT_TO_VIDEO

    def _row_mode_label(self, row: int) -> str:
        key = self._row_mode_key(int(row))
        prompt_item = self.table.item(int(row), self.COL_PROMPT)
        if prompt_item is None:
            return self._mode_label(key)
        try:
            label = str(prompt_item.data(Qt.ItemDataRole.UserRole + 2) or "").strip()
        except Exception:
            label = ""
        return label or self._mode_label(key)

    def _row_mode_payload(self, row: int) -> dict:
        prompt_item = self.table.item(int(row), self.COL_PROMPT)
        if prompt_item is None:
            return {}
        try:
            raw = prompt_item.data(Qt.ItemDataRole.UserRole + 3)
        except Exception:
            raw = None
        return dict(raw) if isinstance(raw, dict) else {}

    def _sync_stt_and_prompt_ids(self) -> None:
        for r in range(self.table.rowCount()):
            stt_item = self.table.item(r, self.COL_STT)
            if stt_item is not None:
                stt_item.setText(f"{r+1:03d}")
            prompt_item = self.table.item(r, self.COL_PROMPT)
            if prompt_item is not None:
                prompt_item.setData(Qt.ItemDataRole.UserRole, str(r + 1))

    def _collect_existing_prompt_ids(self) -> set[str]:
        ids: set[str] = set()
        for r in range(self.table.rowCount()):
            prompt_id = self._prompt_id_of_row(r)
            if prompt_id:
                ids.add(prompt_id)
        return ids

    def _resolve_unique_prompt_id(self, preferred_id: str, used_ids: set[str]) -> str:
        candidate = str(preferred_id or "").strip()
        if candidate and candidate not in used_ids:
            used_ids.add(candidate)
            return candidate

        next_id = 1
        while str(next_id) in used_ids:
            next_id += 1
        resolved = str(next_id)
        used_ids.add(resolved)
        return resolved

    def _status_code(self, row: int) -> str:
        it = self.table.item(int(row), self.COL_STATUS)
        if it is None:
            return "READY"
        try:
            return str(it.data(Qt.ItemDataRole.UserRole) or "READY")
        except Exception:
            return "READY"

    def _set_status_code(self, row: int, code: str) -> None:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            item = QTableWidgetItem("")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(int(row), self.COL_STATUS, item)
        item.setData(Qt.ItemDataRole.UserRole, str(code or "READY"))

    def _row_auto_retry_count(self, row: int) -> int:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            return 0
        try:
            return max(0, int(item.data(Qt.ItemDataRole.UserRole + 10) or 0))
        except Exception:
            return 0

    def _set_row_auto_retry_count(self, row: int, count: int) -> None:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            item = QTableWidgetItem("")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(int(row), self.COL_STATUS, item)
        try:
            item.setData(Qt.ItemDataRole.UserRole + 10, max(0, int(count or 0)))
        except Exception:
            pass

    def _is_auto_retryable_error_text(self, text: str) -> bool:
        raw = str(text or "")
        if not raw:
            return False
        return bool(re.search(r"(?<!\d)(403|500|13)(?!\d)", raw))

    def _row_failed_error_text(self, row: int) -> str:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            return ""
        parts: list[str] = []
        try:
            parts.append(str(item.data(Qt.ItemDataRole.UserRole + 6) or "").strip())
        except Exception:
            pass
        try:
            parts.append(str(item.data(Qt.ItemDataRole.UserRole + 7) or "").strip())
        except Exception:
            pass
        parts.append(str(item.text() or "").strip())
        return " | ".join([p for p in parts if p])

    def get_auto_retry_rows_for_worker(self, mode_key: str, rows: list[int], retry_round: int = 0) -> list[int]:
        if int(retry_round or 0) >= self.AUTO_RETRY_MAX_PER_ROW:
            return []

        retry_rows: list[int] = []
        for r in [int(x) for x in (rows or [])]:
            if r < 0 or r >= self.table.rowCount():
                continue
            if self._status_code(r) != "FAILED":
                continue
            if self._row_auto_retry_count(r) >= self.AUTO_RETRY_MAX_PER_ROW:
                continue
            failed_text = self._row_failed_error_text(r)
            if not self._is_auto_retryable_error_text(failed_text):
                continue
            self._set_row_auto_retry_count(r, self._row_auto_retry_count(r) + 1)
            self._set_status_code(r, "PENDING")
            retry_rows.append(r)

        if retry_rows:
            self._refresh_pending_positions()
            self._append_run_log(
                f"ðŸ” Worker yÃªu cáº§u retry mÃ£ lá»—i 403/13/500: {self._mode_label(str(mode_key or ''))} "
                f"({len(retry_rows)} dÃ²ng)"
            )
        return retry_rows

    def _status_text(self, code: str, queue_position: int = 0) -> str:
        c = str(code or "").upper()
        if c == "TOKEN":
            return "Äang táº¡o"
        if c == "REQUESTED":
            return "Äang táº¡o"
        if c == "PENDING":
            if queue_position > 0:
                return f"Äang chá» (vá»‹ trÃ­ {queue_position})"
            return "Äang chá»"
        if c == "ACTIVE":
            return "Äang táº¡o"
        if c == "DOWNLOADING":
            return "Äang táº¡o"
        if c == "SUCCESSFUL":
            return "HoÃ n thÃ nh"
        if c == "FAILED":
            return "Lá»—i"
        if c == "CANCELED":
            return "Há»§y"
        if c == "STOPPED":
            return "Há»§y"
        return "Sáºµn sÃ ng"

    def _apply_status_color(self, row: int, status_text: str = "") -> None:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            return
        code = self._status_code(row)
        text = str(status_text or item.text() or "")
        if code in {"FAILED", "STOPPED", "CANCELED"} or "Lá»—i" in text or "Há»§y" in text:
            color = QColor("#d32f2f")
        elif code in {"SUCCESSFUL"}:
            color = QColor("#2e7d32")
        elif code in {"ACTIVE", "DOWNLOADING", "TOKEN", "REQUESTED"}:
            color = QColor("#ef6c00")
        else:
            color = QColor("#374151")
        item.setForeground(QBrush(color))

    def _refresh_pending_positions(self) -> None:
        pending_rows: list[int] = []
        for r in range(self.table.rowCount()):
            if self._status_code(r) == "PENDING":
                pending_rows.append(r)

        pending_pos = {row: idx + 1 for idx, row in enumerate(pending_rows)}
        for r in range(self.table.rowCount()):
            code = self._status_code(r)
            item = self.table.item(r, self.COL_STATUS)
            if item is None:
                continue
            cur_text = str(item.text() or "")
            if code == "FAILED" and cur_text.startswith("Lá»—i"):
                pass
            else:
                item.setText(self._status_text(code, pending_pos.get(r, 0)))
            self._apply_status_color(r)
        self._update_status_summary()
        if not self._loading_status_snapshot:
            self._save_status_snapshot()

    def _normalize_status_code(self, raw: str) -> str:
        text = str(raw or "").upper()
        if "CANCEL" in text or "HUY" in text:
            return "CANCELED"
        if "TOKEN" in text:
            return "TOKEN"
        if "REQUEST" in text or "SUBMIT" in text:
            return "REQUESTED"
        if "QUEUE" in text or "QUEUED" in text:
            return "PENDING"
        if "PENDING" in text:
            return "PENDING"
        if (
            "ACTIVE" in text
            or "RUNNING" in text
            or "PROCESS" in text
            or "PROGRESS" in text
            or "CREATING" in text
            or "GENERATING" in text
            or "STARTED" in text
        ):
            return "ACTIVE"
        if "SUCCESS" in text:
            return "SUCCESSFUL"
        if "FAIL" in text:
            return "FAILED"
        if "DOWNLOADING" in text:
            return "DOWNLOADING"
        return "READY"

    def _prompt_id_of_row(self, row: int) -> str:
        it = self.table.item(int(row), self.COL_PROMPT)
        if it is None:
            return ""
        try:
            return str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
        except Exception:
            return ""

    def _find_row_by_prompt_id(self, prompt_id: str) -> int:
        needle = str(prompt_id or "").strip()
        if not needle:
            return -1
        for r in range(self.table.rowCount() - 1, -1, -1):
            if self._prompt_id_of_row(r) == needle:
                return r
        return -1

    def _collect_runnable_rows(self) -> list[int]:
        rows: list[int] = []
        for r in range(self.table.rowCount()):
            status_code = self._status_code(r)
            if status_code not in {"READY", "FAILED", "STOPPED", "CANCELED"}:
                continue
            prompt_it = self.table.item(r, self.COL_PROMPT)
            prompt_text = (prompt_it.text() if prompt_it is not None else "")
            if str(prompt_text or "").strip():
                rows.append(r)
        return rows

    def _build_project_data_from_rows(self, rows: list[int]) -> dict:
        self._sync_stt_and_prompt_ids()
        items: list[dict] = []
        for r in rows:
            prompt_it = self.table.item(r, self.COL_PROMPT)
            prompt_text = str((prompt_it.text() if prompt_it is not None else "") or "").strip()
            prompt_id = self._prompt_id_of_row(r)
            if not prompt_id:
                prompt_id = self._next_prompt_id()
                if prompt_it is not None:
                    prompt_it.setData(Qt.ItemDataRole.UserRole, prompt_id)
            if prompt_text:
                items.append({"id": prompt_id, "description": prompt_text})

        return {
            "prompts": {"text_to_video": items},
            "_use_project_prompts": True,
            "_worker_controls_lifecycle": False,
            "aspect_ratio": str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16"),
            "veo_model": str(getattr(self._cfg, "veo_model", "Veo 3.1 - Fast") or "Veo 3.1 - Fast"),
            "output_count": int(getattr(self._cfg, "output_count", 1) or 1),
        }

    def _resolve_project_name(self) -> str:
        config = SettingsManager.load_config()
        project_name = "default_project"
        if isinstance(config, dict):
            project_name = str(config.get("current_project") or project_name).strip() or project_name
        return project_name

    def _status_snapshot_path(self) -> Path:
        project_dir = WORKFLOWS_DIR / self._resolve_project_name()
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "status.json"

    def _row_media_map(self, row: int) -> dict[int, str]:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            return {}
        try:
            raw = dict(it.data(Qt.ItemDataRole.UserRole + 1) or {})
        except Exception:
            raw = {}
        out: dict[int, str] = {}
        for key, value in raw.items():
            try:
                idx = int(key)
            except Exception:
                continue
            p = str(value or "").strip()
            if p:
                out[idx] = p
        return out

    def _row_preview_map(self, row: int) -> dict[int, str]:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            return {}
        try:
            raw = dict(it.data(Qt.ItemDataRole.UserRole + 4) or {})
        except Exception:
            raw = {}
        out: dict[int, str] = {}
        for key, value in raw.items():
            try:
                idx = int(key)
            except Exception:
                continue
            p = str(value or "").strip()
            if p:
                out[idx] = p
        return out

    def _build_status_snapshot(self) -> dict:
        rows_data: list[dict] = []
        for r in range(self.table.rowCount()):
            stt_item = self.table.item(r, self.COL_STT)
            prompt_item = self.table.item(r, self.COL_PROMPT)
            status_item = self.table.item(r, self.COL_STATUS)
            video_item = self.table.item(r, self.COL_VIDEO)

            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            prompt_id = self._prompt_id_of_row(r)
            status_code = self._status_code(r)
            status_text = str((status_item.text() if status_item is not None else "") or self._status_text(status_code))
            media_map = self._row_media_map(r)
            preview_map = self._row_preview_map(r)

            selected_output_index = 1
            media_path = ""
            output_count = self._row_output_count(r)
            if video_item is not None:
                try:
                    selected_output_index = int(video_item.data(Qt.ItemDataRole.UserRole + 2) or 1)
                except Exception:
                    selected_output_index = 1
                media_path = str(video_item.data(Qt.ItemDataRole.UserRole) or "").strip()

            if not media_path and media_map:
                media_path = str(media_map.get(selected_output_index) or media_map.get(sorted(media_map.keys())[0]) or "")

            rows_data.append(
                {
                    "row": int(r),
                    "stt": str((stt_item.text() if stt_item is not None else f"{r+1:03d}") or f"{r+1:03d}"),
                    "prompt_id": prompt_id,
                    "prompt": prompt_text,
                    "mode_key": self._row_mode_key(r),
                    "mode_label": self._row_mode_label(r),
                    "mode_name": self._row_mode_label(r),
                    "mode_payload": self._row_mode_payload(r),
                    "status_code": status_code,
                    "status_text": status_text,
                    "output_count": int(output_count),
                    "selected_output_index": int(selected_output_index),
                    "media_map": {str(k): v for k, v in media_map.items()},
                    "preview_map": {str(k): v for k, v in preview_map.items()},
                }
            )

        return {
            "project_name": self._resolve_project_name(),
            "updated_at": int(time.time()),
            "rows": rows_data,
        }

    def _save_status_snapshot(self) -> None:
        if self._loading_status_snapshot:
            return
        try:
            path = self._status_snapshot_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._build_status_snapshot(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_status_snapshot(self) -> None:
        path = self._status_snapshot_path()
        if not path.exists():
            self._status_loaded = True
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self._status_loaded = True
            return

        rows = data.get("rows", []) if isinstance(data, dict) else []
        if not isinstance(rows, list):
            rows = []

        self._loading_status_snapshot = True
        try:
            self.table.setRowCount(0)
            for idx, row_data in enumerate(rows):
                if not isinstance(row_data, dict):
                    continue
                prompt_text = str(row_data.get("prompt") or "").strip()
                prompt_id = str(row_data.get("prompt_id") or "").strip()
                row = self.table.rowCount()
                self._add_row(row, prompt_text)

                stt_item = self.table.item(row, self.COL_STT)
                if stt_item is not None:
                    stt_item.setText(str(row_data.get("stt") or f"{idx+1:03d}"))

                prompt_item = self.table.item(row, self.COL_PROMPT)
                if prompt_item is not None and prompt_id:
                    prompt_item.setData(Qt.ItemDataRole.UserRole, prompt_id)

                mode_key = str(row_data.get("mode_key") or self.MODE_TEXT_TO_VIDEO).strip() or self.MODE_TEXT_TO_VIDEO
                mode_payload = row_data.get("mode_payload") if isinstance(row_data.get("mode_payload"), dict) else {}
                self._set_row_mode_meta(row, mode_key, payload=mode_payload)
                self._refresh_prompt_cell(row)

                status_code = str(row_data.get("status_code") or "READY")
                status_text = str(row_data.get("status_text") or self._status_text(status_code))
                self._set_status_code(row, status_code)
                status_item = self.table.item(row, self.COL_STATUS)
                if status_item is not None:
                    status_item.setText(status_text)
                self._apply_status_color(row, status_text)

                video_item = self.table.item(row, self.COL_VIDEO)
                if video_item is None:
                    video_item = QTableWidgetItem("")
                    video_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.table.setItem(row, self.COL_VIDEO, video_item)

                output_count = int(row_data.get("output_count") or self._expected_output_count())
                output_count = max(1, min(4, output_count))
                video_item.setData(Qt.ItemDataRole.UserRole + 3, output_count)

                raw_map = row_data.get("media_map") or {}
                media_map: dict[int, str] = {}
                if isinstance(raw_map, dict):
                    for key, value in raw_map.items():
                        try:
                            mk = int(key)
                        except Exception:
                            continue
                        mv = str(value or "").strip()
                        if mv:
                            media_map[mk] = mv
                video_item.setData(Qt.ItemDataRole.UserRole + 1, media_map)

                raw_preview_map = row_data.get("preview_map") or {}
                preview_map: dict[int, str] = {}
                if isinstance(raw_preview_map, dict):
                    for key, value in raw_preview_map.items():
                        try:
                            mk = int(key)
                        except Exception:
                            continue
                        mv = str(value or "").strip()
                        if mv:
                            preview_map[mk] = mv

                selected_idx = int(row_data.get("selected_output_index") or 1)
                if selected_idx not in media_map and media_map:
                    selected_idx = sorted(media_map.keys())[0]
                if selected_idx < 1:
                    selected_idx = 1
                video_item.setData(Qt.ItemDataRole.UserRole + 2, selected_idx)

                media_path = str(row_data.get("media_path") or "").strip()
                if not media_path and media_map:
                    media_path = str(media_map.get(selected_idx) or media_map.get(sorted(media_map.keys())[0]) or "")

                if not preview_map:
                    thumb_path = str(row_data.get("thumbnail_path") or "").strip()
                    if thumb_path and self._is_image_file(thumb_path):
                        preview_map[selected_idx] = thumb_path
                if selected_idx not in preview_map:
                    preview_candidate = media_path
                    if preview_candidate and self._is_image_file(preview_candidate):
                        preview_map[selected_idx] = preview_candidate

                preview_path = str(preview_map.get(selected_idx) or media_path)
                video_item.setData(Qt.ItemDataRole.UserRole, media_path)
                video_item.setText("")
                video_item.setData(Qt.ItemDataRole.UserRole + 4, preview_map)

                self._refresh_video_badges(row)
                self._render_media_preview(row, preview_path)

            self._sync_stt_and_prompt_ids()
        finally:
            self._loading_status_snapshot = False

        self._status_loaded = True
        self._update_empty_state()
        self._update_status_summary()

    def _ensure_status_snapshot_loaded(self) -> None:
        if self._status_loaded:
            return
        self._load_status_snapshot()

    def _update_status_summary(self) -> None:
        done_count = 0
        failed_count = 0
        creating_count = 0
        waiting_count = 0
        creating_codes = {"TOKEN", "REQUESTED", "ACTIVE", "DOWNLOADING"}
        waiting_codes = {"PENDING"}

        for r in range(self.table.rowCount()):
            code = self._status_code(r)
            output_count = self._row_output_count(r)
            media_map = self._row_media_map(r)
            success_for_row = len(media_map)

            done_count += success_for_row

            if code == "FAILED":
                remain = output_count - success_for_row
                failed_count += remain if remain > 0 else 1

            if code in creating_codes:
                remain = output_count - success_for_row
                creating_count += remain if remain > 0 else 1

            if code in waiting_codes:
                remain = output_count - success_for_row
                waiting_count += remain if remain > 0 else 1

        if self.lbl_status_summary is not None:
            self.lbl_status_summary.setText(
                "<span style='color:#16a34a;'>HoÃ n thÃ nh(" + str(done_count) + ")</span>"
                "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                "<span style='color:#f59e0b;'>Äang táº¡o(" + str(creating_count) + ")</span>"
                "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                "<span style='color:#2563eb;'>Äang chá»(" + str(waiting_count) + ")</span>"
                "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                "<span style='color:#f87171;'>Lá»—i(" + str(failed_count) + ")</span>"
            )

    def _clear_media_for_rows(self, rows: list[int], delete_files: bool = True) -> None:
        cleaned = 0
        for r in rows:
            row = int(r)
            it = self.table.item(row, self.COL_VIDEO)
            if it is None:
                continue

            paths_to_delete: set[str] = set()
            try:
                current = str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
            except Exception:
                current = ""
            if current:
                paths_to_delete.add(current)

            try:
                media_map = dict(it.data(Qt.ItemDataRole.UserRole + 1) or {})
            except Exception:
                media_map = {}
            for value in media_map.values():
                p = str(value or "").strip()
                if p:
                    paths_to_delete.add(p)

            try:
                preview_map = dict(it.data(Qt.ItemDataRole.UserRole + 4) or {})
            except Exception:
                preview_map = {}
            for value in preview_map.values():
                p = str(value or "").strip()
                if p:
                    paths_to_delete.add(p)

            if delete_files:
                for p in paths_to_delete:
                    try:
                        if os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        pass

            try:
                it.setData(Qt.ItemDataRole.UserRole, "")
                it.setData(Qt.ItemDataRole.UserRole + 1, {})
                it.setData(Qt.ItemDataRole.UserRole + 2, 1)
                it.setData(Qt.ItemDataRole.UserRole + 4, {})
            except Exception:
                pass

            self._refresh_video_badges(row)
            self._render_media_preview(row, "")
            cleaned += 1

        if cleaned > 0 and not self._loading_status_snapshot:
            self._save_status_snapshot()
            self._update_status_summary()

    def start_idea_to_video(self, idea_settings: dict) -> None:
        self._global_stop_requested = False
        if self.isRunning():
            QMessageBox.information(self, "Äang cháº¡y", "Workflow Ä‘ang cháº¡y, hÃ£y dá»«ng trÆ°á»›c khi cháº¡y má»›i.")
            return

        idea_text = str((idea_settings or {}).get("idea") or "").strip()
        if not idea_text:
            QMessageBox.warning(self, "Thiáº¿u ká»‹ch báº£n", "Vui lÃ²ng nháº­p ná»™i dung á»Ÿ Ã´ Ká»‹ch báº£n/ Ã tÆ°á»Ÿng.")
            return

        scene_count = int((idea_settings or {}).get("scene_count") or 1)
        style = str((idea_settings or {}).get("style") or "3d_Pixar")
        language = str((idea_settings or {}).get("dialogue_language") or "Tiáº¿ng Viá»‡t (vi-VN)")

        project_name = self._resolve_project_name()

        self._append_run_log(f"ðŸš€ Khá»Ÿi Ä‘á»™ng Idea to Video | project={project_name} | scenes={scene_count} | style={style}")
        self._append_run_log("â³ Äang táº¡o prompt tá»« Ã½ tÆ°á»Ÿng...")
        self._update_empty_state()
        self._idea_worker = _IdeaToVideoWorker(
            project_name=project_name,
            idea_text=idea_text,
            scene_count=scene_count,
            style=style,
            language=language,
            parent=self,
        )
        self._idea_worker.log_message.connect(self._append_run_log)
        self._idea_worker.completed.connect(self._on_idea_to_video_complete)
        self._idea_worker.start()
        self.runStateChanged.emit(True)

    def _on_idea_to_video_complete(self, result: dict) -> None:
        self._idea_worker = None

        if self._global_stop_requested:
            self._append_run_log("ðŸ›‘ Bá» qua callback Idea to Video vÃ¬ Ä‘Ã£ nháº­n lá»‡nh dá»«ng toÃ n bá»™")
            self.runStateChanged.emit(False)
            return

        ok = bool((result or {}).get("success"))
        msg = str((result or {}).get("message") or "")
        if msg:
            self._append_run_log(msg)

        if not ok:
            self.runStateChanged.emit(False)
            return

        prompts_data = (result or {}).get("prompts")
        prompt_texts: list[str] = []
        if isinstance(prompts_data, list):
            for item in prompts_data:
                if isinstance(item, dict):
                    text = str(item.get("prompt") or item.get("description") or "").strip()
                    if text:
                        prompt_texts.append(text)

        if not prompt_texts:
            self._append_run_log("âš ï¸ Idea to Video khÃ´ng tráº£ vá» prompt há»£p lá»‡.")
            self.runStateChanged.emit(False)
            return

        self._append_run_log(f"âœ… Idea to Video táº¡o {len(prompt_texts)} prompt. Báº¯t Ä‘áº§u cháº¡y Text to Video...")
        self.start_text_to_video(prompt_texts)

    def _start_text_to_video_rows(self, rows: list[int]) -> bool:
        if not rows:
            return False

        self._snapshot_output_count_for_rows(rows)

        project_name = self._resolve_project_name()
        project_data = self._build_project_data_from_rows(rows)
        prompts = project_data.get("prompts", {}).get("text_to_video", [])
        if not prompts:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ prompt", "KhÃ´ng cÃ³ prompt há»£p lá»‡ trong báº£ng status.")
            return False

        try:
            self._append_run_log(f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow Text to Video | project={project_name} | prompts={len(prompts)}")
            self._workflow = TextToVideoWorkflow(project_name=project_name, project_data=project_data, parent=self)
            self._workflow.log_message.connect(self._on_workflow_log)
            self._workflow.video_updated.connect(self._on_video_updated)
            self._workflow.automation_complete.connect(self._on_workflow_complete)
            self._workflow.start()
            self._workflows.append(self._workflow)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow: {exc}")
            return False

        for r in rows:
            self._set_status_code(r, "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_grok_text_to_video_rows(self, rows: list[int]) -> bool:
        if not rows:
            return False

        self._set_output_count_for_rows(rows, 1)

        prompts: list[str] = []
        prompt_ids: list[str] = []
        for r in rows:
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            if not prompt_text:
                continue
            prompts.append(prompt_text)
            prompt_ids.append(self._prompt_id_of_row(int(r)) or str(int(r) + 1))

        if not prompts:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ prompt", "KhÃ´ng cÃ³ prompt GROK há»£p lá»‡ trong báº£ng status.")
            return False

        aspect_ratio = str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16")
        grok_video_length_seconds = int(getattr(self._cfg, "grok_video_length_seconds", 6) or 6)
        grok_video_resolution = str(getattr(self._cfg, "grok_video_resolution", "480p") or "480p")
        grok_account_type = str(getattr(self._cfg, "grok_account_type", "SUPER") or "SUPER").strip().upper()
        if grok_account_type == "ULTRA":
            grok_account_type = "SUPER"
        if grok_account_type == "NORMAL":
            if grok_video_length_seconds != 6 or grok_video_resolution != "480p":
                self._append_run_log("â„¹ï¸ GROK NORMAL: Ã©p cáº¥u hÃ¬nh 6 giÃ¢y vÃ  480p")
            grok_video_length_seconds = 6
            grok_video_resolution = "480p"
        output_dir = str(getattr(self._cfg, "video_output_dir", "") or "").strip()
        max_concurrency = max(1, int(getattr(self._cfg, "grok_multi_video", getattr(self._cfg, "multi_video", 5)) or 5))
        offscreen = True

        try:
            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow GROK Text to Video | prompts={len(prompts)} | max={max_concurrency}"
            )
            worker = GrokTextToVideoWorker(
                prompts=prompts,
                prompt_ids=prompt_ids,
                aspect_ratio=aspect_ratio,
                video_length_seconds=grok_video_length_seconds,
                resolution_name=grok_video_resolution,
                output_dir=output_dir,
                max_concurrency=max_concurrency,
                offscreen_chrome=offscreen,
                parent=self,
            )
            worker.log_message.connect(self._append_run_log)
            worker.status_updated.connect(self._on_grok_status_updated)
            worker.video_updated.connect(self._on_video_updated)
            worker.automation_complete.connect(self._on_workflow_complete)
            worker.start()
            self._workflow = worker
            self._workflows.append(worker)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng GROK workflow: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng GROK workflow: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _on_grok_status_updated(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        prompt_id = str(payload.get("prompt_id") or "").strip()
        row = self._find_row_by_prompt_id(prompt_id) if prompt_id else -1
        if row < 0:
            return

        current_code = self._status_code(row)
        if current_code in {"SUCCESSFUL", "FAILED", "CANCELED", "STOPPED"}:
            return

        progress = payload.get("progress")
        status_text = str(payload.get("status_text") or "").strip()
        low = status_text.lower()

        if isinstance(progress, int):
            self._set_video_progress_text(row, int(progress))
            self._set_row_status_detail(row, "ACTIVE", "Äang táº¡o")

        if "lá»—i" in low or low == "error" or low.startswith("error"):
            self._set_row_status_detail(row, "FAILED", self._format_failed_status_text("GROK_ERROR", status_text))
            self._try_finalize_grok_batch_now()
            return
        if "hoÃ n thÃ nh" in low or "hoan thanh" in low or "done" in low or "complete" in low:
            self._set_row_status_detail(row, "SUCCESSFUL", "HoÃ n thÃ nh")
            return
        if "táº£i" in low or "download" in low:
            self._set_row_status_detail(row, "DOWNLOADING", "Äang táº£i video")
            return
        if "xáº¿p hÃ ng" in low:
            self._set_row_status_detail(row, "PENDING", "Äang chá»")
            return
        if status_text:
            self._set_row_status_detail(row, "ACTIVE", "Äang táº¡o")

        self._try_finalize_grok_batch_now()

    def _start_grok_image_to_video_rows(self, rows: list[int]) -> bool:
        if not rows:
            return False

        self._set_output_count_for_rows(rows, 1)

        items: list[dict] = []
        prompt_ids: list[str] = []
        for r in rows:
            payload = self._row_mode_payload(int(r))
            image_link = str(payload.get("image_link") or "").strip()
            if not image_link:
                continue
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            items.append({"image_path": image_link, "prompt": prompt_text})
            prompt_ids.append(self._prompt_id_of_row(int(r)) or str(int(r) + 1))

        if not items:
            QMessageBox.warning(self, "KhÃ´ng cÃ³ dá»¯ liá»‡u", "KhÃ´ng cÃ³ dá»¯ liá»‡u GROK Image to Video há»£p lá»‡ trong báº£ng status.")
            return False

        aspect_ratio = str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16")
        grok_video_length_seconds = int(getattr(self._cfg, "grok_video_length_seconds", 6) or 6)
        grok_video_resolution = str(getattr(self._cfg, "grok_video_resolution", "480p") or "480p")
        grok_account_type = str(getattr(self._cfg, "grok_account_type", "SUPER") or "SUPER").strip().upper()
        if grok_account_type == "ULTRA":
            grok_account_type = "SUPER"
        if grok_account_type == "NORMAL":
            if grok_video_length_seconds != 6 or grok_video_resolution != "480p":
                self._append_run_log("â„¹ï¸ GROK NORMAL: Ã©p cáº¥u hÃ¬nh 6 giÃ¢y vÃ  480p")
            grok_video_length_seconds = 6
            grok_video_resolution = "480p"
        output_dir = str(getattr(self._cfg, "video_output_dir", "") or "").strip()
        max_concurrency = max(1, int(getattr(self._cfg, "grok_multi_video", getattr(self._cfg, "multi_video", 5)) or 5))
        offscreen = True

        try:
            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow GROK Image to Video | jobs={len(items)} | max={max_concurrency}"
            )
            worker = GrokImageToVideoWorker(
                items=items,
                prompt_ids=prompt_ids,
                aspect_ratio=aspect_ratio,
                video_length_seconds=grok_video_length_seconds,
                resolution_name=grok_video_resolution,
                output_dir=output_dir,
                max_concurrency=max_concurrency,
                offscreen_chrome=offscreen,
                parent=self,
            )
            worker.log_message.connect(self._append_run_log)
            worker.status_updated.connect(self._on_grok_status_updated)
            worker.video_updated.connect(self._on_video_updated)
            worker.automation_complete.connect(self._on_workflow_complete)
            worker.start()
            self._workflow = worker
            self._workflows.append(worker)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng GROK Image to Video: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng GROK Image to Video: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_image_to_video_rows(self, rows: list[int], normalized_mode: str) -> bool:
        if not rows:
            return False

        clean_items: list[dict] = []
        prompt_key = "image_to_video_start_end" if normalized_mode == "start_end" else "image_to_video"
        for r in rows:
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            payload = self._row_mode_payload(int(r))
            if normalized_mode == "start_end":
                start_image_link = str(payload.get("start_image_link") or "").strip()
                end_image_link = str(payload.get("end_image_link") or "").strip()
                if not (start_image_link and end_image_link and prompt_text):
                    continue
                clean_items.append(
                    {
                        "id": self._prompt_id_of_row(int(r)) or str(int(r) + 1),
                        "prompt": prompt_text,
                        "start_image_link": start_image_link,
                        "end_image_link": end_image_link,
                    }
                )
            else:
                image_link = str(payload.get("image_link") or "").strip()
                if not (image_link and prompt_text):
                    continue
                clean_items.append(
                    {
                        "id": self._prompt_id_of_row(int(r)) or str(int(r) + 1),
                        "prompt": prompt_text,
                        "image_link": image_link,
                    }
                )

        if not clean_items:
            return False

        self._snapshot_output_count_for_rows(rows)
        project_name = self._resolve_project_name()
        project_data = {
            "prompts": {prompt_key: clean_items},
            "_use_project_prompts": True,
            "_worker_controls_lifecycle": False,
            "i2v_mode": normalized_mode,
            "aspect_ratio": str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16"),
            "veo_model": str(getattr(self._cfg, "veo_model", "Veo 3.1 - Fast") or "Veo 3.1 - Fast"),
            "output_count": int(getattr(self._cfg, "output_count", 1) or 1),
            "video_output_dir": str(getattr(self._cfg, "video_output_dir", "") or "").strip(),
        }

        try:
            from A_workflow_image_to_video import ImageToVideoWorkflow

            mode_label = "áº¢nh Äáº§u - áº¢nh Cuá»‘i" if normalized_mode == "start_end" else "áº¢nh"
            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow Image to Video ({mode_label}) | project={project_name} | prompts={len(clean_items)}"
            )
            self._workflow = ImageToVideoWorkflow(project_name=project_name, project_data=project_data, parent=self)
            self._workflow.log_message.connect(self._on_workflow_log)
            self._workflow.video_updated.connect(self._on_video_updated)
            self._workflow.automation_complete.connect(self._on_workflow_complete)
            self._workflow.start()
            self._workflows.append(self._workflow)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Image to Video: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Image to Video: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_generate_image_rows(self, rows: list[int]) -> bool:
        if not rows:
            return False

        clean_items: list[dict] = []
        for r in rows:
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            if not prompt_text:
                continue
            clean_items.append({"id": self._prompt_id_of_row(int(r)) or str(int(r) + 1), "description": prompt_text})

        if not clean_items:
            return False

        self._snapshot_output_count_for_rows(rows)
        project_name = self._resolve_project_name()
        project_data = {
            "prompts": {"text_to_video": clean_items},
            "_use_project_prompts": True,
            "_worker_controls_lifecycle": True,
            "aspect_ratio": str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16"),
            "veo_model": str(getattr(self._cfg, "veo_model", "Veo 3.1 - Fast") or "Veo 3.1 - Fast"),
            "output_count": int(getattr(self._cfg, "output_count", 1) or 1),
            "video_output_dir": str(getattr(self._cfg, "video_output_dir", "") or "").strip(),
        }

        try:
            from A_workflow_generate_image import GenerateImageWorkflow

            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh tá»« Prompt | project={project_name} | prompts={len(clean_items)}"
            )
            self._workflow = GenerateImageWorkflow(project_name=project_name, project_data=project_data, parent=self)
            self._workflow.log_message.connect(self._on_workflow_log)
            self._workflow.video_updated.connect(self._on_video_updated)
            self._workflow.automation_complete.connect(self._on_workflow_complete)
            self._workflow.start()
            self._workflows.append(self._workflow)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_generate_image_reference_rows(self, rows: list[int], shared_characters: list[dict] | None = None) -> bool:
        if not rows:
            return False

        prompts: list[dict] = []
        for r in rows:
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            if not prompt_text:
                continue
            prompts.append({"id": self._prompt_id_of_row(int(r)) or str(int(r) + 1), "prompt": prompt_text})

        characters = list(shared_characters or [])
        if not characters:
            for r in rows:
                payload = self._row_mode_payload(int(r))
                cand = payload.get("characters") if isinstance(payload, dict) else None
                if isinstance(cand, list) and cand:
                    characters = [x for x in cand if isinstance(x, dict)]
                    break

        clean_characters: list[dict] = []
        for ch in characters:
            name = str(ch.get("name") or "").strip() if isinstance(ch, dict) else ""
            path = str(ch.get("path") or "").strip() if isinstance(ch, dict) else ""
            if name and path:
                clean_characters.append({"name": name, "path": path})

        if not prompts or not clean_characters:
            return False

        project_name = self._resolve_project_name()
        project_data = {
            "prompts": {"create_image_reference": prompts},
            "characters": clean_characters,
            "image_mode": "reference",
            "_use_project_prompts": True,
            "_worker_controls_lifecycle": True,
            "aspect_ratio": str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16"),
            "veo_model": str(getattr(self._cfg, "veo_model", "Veo 3.1 - Fast") or "Veo 3.1 - Fast"),
            "create_image_model": str(getattr(self._cfg, "create_image_model", "Imagen 4") or "Imagen 4"),
            "output_count": int(getattr(self._cfg, "output_count", 1) or 1),
            "video_output_dir": str(getattr(self._cfg, "video_output_dir", "") or "").strip(),
        }

        try:
            from A_workflow_image_to_image import GenerateImageWorkflow

            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh tá»« áº¢nh Tham Chiáº¿u | project={project_name} | prompts={len(prompts)} | refs={len(clean_characters)}"
            )
            self._workflow = GenerateImageWorkflow(project_name=project_name, project_data=project_data, parent=self)
            self._workflow.log_message.connect(self._on_workflow_log)
            self._workflow.video_updated.connect(self._on_video_updated)
            self._workflow.automation_complete.connect(self._on_workflow_complete)
            self._workflow.start()
            self._workflows.append(self._workflow)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh tá»« áº¢nh Tham Chiáº¿u: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Táº¡o áº¢nh tá»« áº¢nh Tham Chiáº¿u: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_character_sync_rows(self, rows: list[int], shared_characters: list[dict] | None = None) -> bool:
        if not rows:
            return False

        prompts: list[dict] = []
        for r in rows:
            prompt_item = self.table.item(int(r), self.COL_PROMPT)
            prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
            if not prompt_text:
                continue
            prompts.append({"id": self._prompt_id_of_row(int(r)) or str(int(r) + 1), "prompt": prompt_text})

        characters = list(shared_characters or [])
        if not characters:
            for r in rows:
                payload = self._row_mode_payload(int(r))
                cand = payload.get("characters") if isinstance(payload, dict) else None
                if isinstance(cand, list) and cand:
                    characters = [x for x in cand if isinstance(x, dict)]
                    break

        clean_characters: list[dict] = []
        for ch in characters:
            name = str(ch.get("name") or "").strip() if isinstance(ch, dict) else ""
            path = str(ch.get("path") or "").strip() if isinstance(ch, dict) else ""
            if name and path:
                clean_characters.append({"name": name, "path": path})

        if not prompts or not clean_characters:
            return False

        project_name = self._resolve_project_name()
        project_data = {
            "prompts": {"character_sync": prompts},
            "characters": clean_characters,
            "_use_project_prompts": True,
            "_worker_controls_lifecycle": True,
            "aspect_ratio": str(getattr(self._cfg, "video_aspect_ratio", "9:16") or "9:16"),
            "veo_model": str(getattr(self._cfg, "veo_model", "Veo 3.1 - Fast") or "Veo 3.1 - Fast"),
            "output_count": int(getattr(self._cfg, "output_count", 1) or 1),
            "video_output_dir": str(getattr(self._cfg, "video_output_dir", "") or "").strip(),
        }

        try:
            from A_workflow_sync_chactacter import CharacterSyncWorkflow

            self._append_run_log(
                f"ðŸš€ Khá»Ÿi Ä‘á»™ng workflow Äá»“ng bá»™ nhÃ¢n váº­t | project={project_name} | prompts={len(prompts)} | characters={len(clean_characters)}"
            )
            self._workflow = CharacterSyncWorkflow(project_name=project_name, project_data=project_data, parent=self)
            self._workflow.log_message.connect(self._on_workflow_log)
            self._workflow.video_updated.connect(self._on_video_updated)
            self._workflow.automation_complete.connect(self._on_workflow_complete)
            self._workflow.start()
            self._workflows.append(self._workflow)
        except Exception as exc:
            self._workflow = None
            self._append_run_log(f"âŒ KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Äá»“ng bá»™ nhÃ¢n váº­t: {exc}")
            QMessageBox.critical(self, "Lá»—i workflow", f"KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng workflow Äá»“ng bá»™ nhÃ¢n váº­t: {exc}")
            return False

        for r in rows:
            self._set_status_code(int(r), "PENDING")
        self._refresh_pending_positions()
        self.runStateChanged.emit(True)
        return True

    def _start_rows_by_mode(self, rows: list[int]) -> None:
        if not rows:
            return

        self._sync_stt_and_prompt_ids()
        grouped: dict[str, list[int]] = {}
        for r in rows:
            mode_key = self._row_mode_key(int(r))
            grouped.setdefault(mode_key, []).append(int(r))

        queue: list[tuple[str, list[int]]] = []
        queue_jobs: list[dict] = []
        all_valid_rows: list[int] = []
        skipped_messages: list[str] = []
        for mode_key, mode_rows in grouped.items():
            valid_rows: list[int] = []
            for r in mode_rows:
                prompt_item = self.table.item(int(r), self.COL_PROMPT)
                prompt_text = str((prompt_item.text() if prompt_item is not None else "") or "").strip()
                payload = self._row_mode_payload(int(r))
                if mode_key == self.MODE_IMAGE_TO_VIDEO_SINGLE and not str(payload.get("image_link") or "").strip():
                    continue
                if mode_key == self.MODE_GROK_IMAGE_TO_VIDEO and not str(payload.get("image_link") or "").strip():
                    continue
                if mode_key == self.MODE_IMAGE_TO_VIDEO_START_END:
                    if not str(payload.get("start_image_link") or "").strip() or not str(payload.get("end_image_link") or "").strip():
                        continue
                if mode_key in {self.MODE_TEXT_TO_VIDEO, self.MODE_GROK_TEXT_TO_VIDEO, self.MODE_CREATE_IMAGE_PROMPT} and not prompt_text:
                    continue
                if mode_key == self.MODE_CREATE_IMAGE_REFERENCE:
                    chars = payload.get("characters") if isinstance(payload, dict) else None
                    if not prompt_text or not isinstance(chars, list) or not chars:
                        continue
                valid_rows.append(int(r))
            if valid_rows:
                queue.append((mode_key, valid_rows))
                all_valid_rows.extend(valid_rows)
            else:
                skipped_messages.append(f"{self._mode_label(mode_key)}: thiáº¿u dá»¯ liá»‡u")

        if not queue:
            QMessageBox.warning(self, "Thiáº¿u dá»¯ liá»‡u", "KhÃ´ng cÃ³ dÃ²ng há»£p lá»‡ Ä‘á»ƒ táº¡o láº¡i theo mode Ä‘Ã£ lÆ°u.")
            return

        prev_codes: dict[int, str] = {}
        for r in all_valid_rows:
            rr = int(r)
            prev_codes[rr] = self._status_code(rr)
            self._set_status_code(rr, "PENDING")
        self._refresh_pending_positions()

        self._retry_mode_queue = []
        for mode_key, mode_rows in queue:
            queue_jobs.append(
                {
                    "mode_key": str(mode_key),
                    "rows": [int(r) for r in mode_rows],
                    "label": self._mode_label(str(mode_key)),
                }
            )

        started = bool(queue_jobs)
        if started:
            try:
                self.queueJobsRequested.emit(queue_jobs)
            except Exception:
                started = False

        if skipped_messages:
            self._append_run_log("âš ï¸ Bá» qua má»™t sá»‘ dÃ²ng: " + " | ".join(skipped_messages))

        if not started:
            for r, code in prev_codes.items():
                self._set_status_code(int(r), str(code or "READY"))
            self._refresh_pending_positions()
            QMessageBox.warning(self, "KhÃ´ng thá»ƒ cháº¡y", "KhÃ´ng thá»ƒ khá»Ÿi Ä‘á»™ng láº¡i cÃ¡c dÃ²ng Ä‘Ã£ chá»n theo mode Ä‘Ã£ lÆ°u.")

    def _start_mode_group(self, mode_key: str, rows: list[int]) -> bool:
        key = str(mode_key or "").strip() or self.MODE_TEXT_TO_VIDEO
        if key == self.MODE_TEXT_TO_VIDEO:
            return self._start_text_to_video_rows(rows)
        if key == self.MODE_GROK_TEXT_TO_VIDEO:
            return self._start_grok_text_to_video_rows(rows)
        if key == self.MODE_GROK_IMAGE_TO_VIDEO:
            return self._start_grok_image_to_video_rows(rows)
        if key == self.MODE_IMAGE_TO_VIDEO_SINGLE:
            return self._start_image_to_video_rows(rows, "single")
        if key == self.MODE_IMAGE_TO_VIDEO_START_END:
            return self._start_image_to_video_rows(rows, "start_end")
        if key == self.MODE_CREATE_IMAGE_PROMPT:
            return self._start_generate_image_rows(rows)
        if key == self.MODE_CREATE_IMAGE_REFERENCE:
            return self._start_generate_image_reference_rows(rows)
        if key == self.MODE_CHARACTER_SYNC:
            return self._start_character_sync_rows(rows)

        QMessageBox.warning(
            self,
            "Mode chÆ°a há»— trá»£",
            f"Mode '{self._mode_label(key)}' hiá»‡n chÆ°a tÃ­ch há»£p cháº¡y láº¡i tá»± Ä‘á»™ng.",
        )
        return False

    def _extract_prompt_id_from_log(self, message: str) -> str:
        text = str(message or "")
        m = re.search(r"prompt\s+([A-Za-z0-9_-]+)", text, re.IGNORECASE)
        if not m:
            m = re.search(r"prompt_id\s*[:=]\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
        if m:
            return str(m.group(1))
        return ""

    def _set_row_status_detail(self, row: int, code: str, text: str, error_code: str = "", error_message: str = "") -> None:
        item = self.table.item(int(row), self.COL_STATUS)
        if item is None:
            item = QTableWidgetItem("")
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(int(row), self.COL_STATUS, item)
        self._set_status_code(row, code)
        try:
            if str(code or "").upper() == "FAILED":
                item.setData(Qt.ItemDataRole.UserRole + 6, str(error_code or "").strip())
                item.setData(Qt.ItemDataRole.UserRole + 7, str(error_message or "").strip())
            else:
                item.setData(Qt.ItemDataRole.UserRole + 6, "")
                item.setData(Qt.ItemDataRole.UserRole + 7, "")
        except Exception:
            pass
        item.setText(str(text or self._status_text(code)))
        self._apply_status_color(row, item.text())
        if not self._loading_status_snapshot:
            self._save_status_snapshot()
            self._update_status_summary()

    def _format_failed_status_text(self, error_code: str = "", error_message: str = "") -> str:
        code = str(error_code or "").strip()
        message = str(error_message or "").strip()
        if code and message:
            return f"Lá»—i (mÃ£ lá»—i: {code} | message: {message})"
        if code:
            return f"Lá»—i (mÃ£ lá»—i: {code})"
        if message:
            return f"Lá»—i (message: {message})"
        return "Lá»—i"

    def _on_workflow_log(self, message: str) -> None:
        text = str(message or "")

        lower_text = text.lower()
        if "upload áº£nh nhÃ¢n váº­t tháº¥t báº¡i" in lower_text or "khÃ´ng Ä‘á»c Ä‘Æ°á»£c áº£nh" in lower_text:
            self._append_run_log(message)
            failed = self._fail_active_rows_now("CHAR_UPLOAD_FAILED", "Upload áº£nh nhÃ¢n váº­t tháº¥t báº¡i")
            if failed > 0:
                self._append_run_log(
                    f"âš ï¸ ÄÃ£ Ä‘Ã¡nh lá»—i {failed} dÃ²ng do lá»—i áº£nh nhÃ¢n váº­t, worker sáº½ xá»­ lÃ½ queue tiáº¿p theo."
                )
            return

        pending_queue_rows = 0
        for rr in range(self.table.rowCount()):
            if self._status_code(rr) == "PENDING":
                pending_queue_rows += 1

        if "Háº¿t táº¥t cáº£ prompts" in text:
            if pending_queue_rows > 0:
                self._append_run_log(
                    f"âœ… ÄÃ£ gá»­i háº¿t prompts cá»§a workflow hiá»‡n táº¡i, cÃ²n {pending_queue_rows} prompt trong hÃ ng chá»."
                )
            else:
                self._append_run_log("âœ… ÄÃ£ gá»­i háº¿t prompts cá»§a workflow hiá»‡n táº¡i, tiáº¿p tá»¥c chá» video hoÃ n thÃ nh...")
            return

        if "ÄÃ£ Ä‘Ã³ng Chrome sau khi gá»­i háº¿t prompts" in text:
            if pending_queue_rows > 0:
                self._append_run_log(
                    f"ðŸ”’ ÄÃ£ Ä‘Ã³ng Chrome cá»§a workflow hiá»‡n táº¡i, cÃ²n {pending_queue_rows} prompt trong hÃ ng chá»."
                )
            else:
                self._append_run_log("ðŸ”’ ÄÃ£ Ä‘Ã³ng Chrome cá»§a workflow hiá»‡n táº¡i.")
            return

        completion_markers = (
            "Workflow Ä‘Ã£ hoÃ n táº¥t",
            "Háº¿t táº¥t cáº£ prompts, chá» video hoÃ n thÃ nh",
            "Táº¥t cáº£ video Ä‘Ã£ hoÃ n thÃ nh",
            "thoÃ¡t workflow",
        )
        if any(marker in text for marker in completion_markers):
            return

        self._append_run_log(message)
        prompt_id = self._extract_prompt_id_from_log(text)
        row = self._find_row_by_prompt_id(prompt_id) if prompt_id else -1

        token_markers = (
            "Äang láº¥y token",
            "láº¥y token...",
            "Láº¥y token thÃ nh cÃ´ng",
            "Timeout láº¥y token",
            "Lá»—i láº¥y token",
        )
        if row >= 0 and ("Prompt" in text and ("Äang láº¥y token" in text or "Láº¥y token thÃ nh cÃ´ng" in text)):
            self._set_row_status_detail(row, "ACTIVE", "Äang táº¡o")
            return

        if row >= 0 and any(marker in text for marker in token_markers):
            self._set_row_status_detail(row, "TOKEN", "Äang láº¥y token")
            return

        if row >= 0 and (
            "Báº¯t Ä‘áº§u gá»­i request" in text
            or "ÄÃ£ gá»­i create video" in text
            or "Gen láº¡i request" in text
            or "Gá»­i request sync character" in text
        ):
            self._set_row_status_detail(row, "REQUESTED", "ÄÃ£ gá»­i request")
            return

        if row >= 0:
            err_match = re.search(r"Lá»—i\s*([0-9A-Z_]+)", text)
            if err_match:
                err_code = str(err_match.group(1) or "").strip()
                if err_code:
                    detail_msg = ""
                    msg_match = re.search(r"(?:API\s*lá»—i|error|message)\s*[:\-]\s*(.+)", text, re.IGNORECASE)
                    if msg_match:
                        detail_msg = str(msg_match.group(1) or "").strip()
                    self._set_row_status_detail(
                        row,
                        "FAILED",
                        self._format_failed_status_text(err_code, detail_msg),
                        error_code=err_code,
                        error_message=detail_msg,
                    )
                    return

    def _on_video_updated(self, payload: dict) -> None:
        prompt_id = str(payload.get("_prompt_id") or "").strip()
        if not prompt_id:
            prompt_idx = str(payload.get("prompt_idx") or "")
            if "_" in prompt_idx:
                prompt_id = prompt_idx.split("_", 1)[0]
            else:
                prompt_id = prompt_idx
        row = self._find_row_by_prompt_id(prompt_id)
        if row < 0:
            return

        status_code = self._normalize_status_code(str(payload.get("status") or ""))
        if status_code == "PENDING":
            prev_code = self._status_code(row)
            if prev_code in {"TOKEN", "REQUESTED", "ACTIVE"}:
                status_code = "ACTIVE"
        self._set_status_code(row, status_code)
        err_code = str(payload.get("error_code") or "").strip()
        err_message = str(payload.get("error_message") or "").strip()
        if status_code == "FAILED":
            self._set_row_status_detail(
                row,
                "FAILED",
                self._format_failed_status_text(err_code, err_message),
                error_code=err_code,
                error_message=err_message,
            )
        elif status_code == "DOWNLOADING":
            self._set_row_status_detail(row, "DOWNLOADING", "Äang táº£i video")

        video_path = str(payload.get("video_path") or "").strip()
        image_path = str(payload.get("image_path") or "").strip()
        open_path = video_path or image_path
        preview_path_payload = image_path or open_path
        if open_path:
            path_ok = os.path.isfile(open_path)
            prompt_idx = str(payload.get("prompt_idx") or "")
            output_index = 1
            if "_" in prompt_idx:
                try:
                    output_index = int(prompt_idx.split("_", 1)[1])
                except Exception:
                    output_index = 1
            vid_item = self.table.item(row, self.COL_VIDEO)
            if vid_item is None:
                vid_item = QTableWidgetItem("")
                vid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, self.COL_VIDEO, vid_item)
            try:
                video_map = dict(vid_item.data(Qt.ItemDataRole.UserRole + 1) or {})
            except Exception:
                video_map = {}
            try:
                preview_map = dict(vid_item.data(Qt.ItemDataRole.UserRole + 4) or {})
            except Exception:
                preview_map = {}
            if path_ok:
                video_map[int(output_index)] = open_path
            if preview_path_payload and os.path.isfile(preview_path_payload):
                preview_map[int(output_index)] = preview_path_payload
            vid_item.setData(Qt.ItemDataRole.UserRole + 1, video_map)
            vid_item.setData(Qt.ItemDataRole.UserRole + 4, preview_map)
            selected_idx = int(vid_item.data(Qt.ItemDataRole.UserRole + 2) or 1)
            if selected_idx not in video_map and video_map:
                selected_idx = sorted(video_map.keys())[0]
                vid_item.setData(Qt.ItemDataRole.UserRole + 2, selected_idx)
            selected_path = str(video_map.get(selected_idx, "") or "")
            selected_preview = str(preview_map.get(selected_idx, "") or selected_path)
            vid_item.setData(Qt.ItemDataRole.UserRole, selected_path)
            vid_item.setText("")

            cell = self.table.cellWidget(row, self.COL_VIDEO)
            if cell is not None:
                self._refresh_video_badges(row)
                self._render_media_preview(row, selected_preview)

        self._refresh_pending_positions()
        self._try_finalize_grok_batch_now()
        if self._awaiting_completion_confirmation and row in self._active_queue_rows:
            self._try_finish_workflow_completion()

    def _try_finalize_grok_batch_now(self) -> bool:
        rows = sorted(int(r) for r in list(self._active_queue_rows))
        if not rows:
            return False

        grok_rows = [
            r
            for r in rows
            if self._row_mode_key(r) in {self.MODE_GROK_TEXT_TO_VIDEO, self.MODE_GROK_IMAGE_TO_VIDEO}
        ]
        if not grok_rows:
            return False

        for r in grok_rows:
            code = self._status_code(r)
            if code in {"FAILED", "STOPPED", "CANCELED"}:
                continue
            try:
                produced = int(len(self._row_media_map(r)))
            except Exception:
                produced = 0
            if produced >= 1:
                if code not in {"SUCCESSFUL", "FAILED", "STOPPED", "CANCELED"}:
                    self._set_row_status_detail(r, "SUCCESSFUL", "HoÃ n thÃ nh")
                continue
            return False

        if self._awaiting_completion_confirmation:
            self._try_finish_workflow_completion()
        return True

    def _mark_active_rows_stopped(self) -> None:
        for r in range(self.table.rowCount()):
            code = self._status_code(r)
            if code in {"PENDING", "ACTIVE", "TOKEN", "REQUESTED", "DOWNLOADING"}:
                self._set_row_status_detail(r, "CANCELED", "Há»§y")

    def _fail_active_rows_now(self, error_code: str, error_message: str) -> int:
        changed = 0
        for r in sorted(int(x) for x in list(self._active_queue_rows)):
            if r < 0 or r >= self.table.rowCount():
                continue
            if self._is_row_terminal_for_completion(r):
                continue
            self._set_row_status_detail(
                r,
                "FAILED",
                self._format_failed_status_text(error_code, error_message),
                error_code=error_code,
                error_message=error_message,
            )
            changed += 1
        if changed > 0:
            self._refresh_pending_positions()
            if self._awaiting_completion_confirmation:
                self._try_finish_workflow_completion()
        return changed

    def _finalize_unresolved_active_rows_after_exit(self) -> None:
        unresolved_rows: list[int] = []
        for r in sorted(int(x) for x in list(self._active_queue_rows)):
            if r < 0 or r >= self.table.rowCount():
                continue
            if self._is_row_terminal_for_completion(r):
                continue
            unresolved_rows.append(r)

        if not unresolved_rows:
            return

        for r in unresolved_rows:
            try:
                expected = max(1, int(self._row_output_count(r)))
            except Exception:
                expected = 1
            try:
                produced = int(len(self._row_media_map(r)))
            except Exception:
                produced = 0
            msg = self._format_failed_status_text(
                "WORKFLOW_EXITED",
                f"Workflow káº¿t thÃºc sá»›m, nháº­n {produced}/{expected} output",
            )
            self._set_row_status_detail(
                r,
                "FAILED",
                msg,
                error_code="WORKFLOW_EXITED",
                error_message=f"Workflow káº¿t thÃºc sá»›m, nháº­n {produced}/{expected} output",
            )

        self._append_run_log(
            f"âš ï¸ Workflow Ä‘Ã£ thoÃ¡t nhÆ°ng cÃ²n {len(unresolved_rows)} dÃ²ng chÆ°a hoÃ n táº¥t; Ä‘Ã£ chuyá»ƒn sang Lá»—i Ä‘á»ƒ khÃ´ng káº¹t hÃ ng chá»."
        )

    def _on_workflow_complete(self) -> None:
        if self._global_stop_requested:
            self._workflow = None
            self._retry_mode_queue = []
            self._active_queue_rows.clear()
            self._awaiting_completion_confirmation = False
            self._completion_poll_scheduled = False
            self._completion_poll_attempts = 0
            self._close_all_workflow_chrome_profiles_async()
            self._append_run_log("ðŸ›‘ ÄÃ£ dá»«ng toÃ n bá»™: khÃ´ng cháº¡y workflow káº¿ tiáº¿p")
            self._refresh_pending_positions()
            self.runStateChanged.emit(False)
            return

        alive_workflows: list[QThread] = []
        for wf in list(self._workflows):
            try:
                if wf and wf.isRunning():
                    alive_workflows.append(wf)
            except Exception:
                pass
        self._workflows = alive_workflows
        self._workflow = self._workflows[-1] if self._workflows else None
        self._refresh_pending_positions()
        if not self._workflows:
            self._finalize_unresolved_active_rows_after_exit()
        self._awaiting_completion_confirmation = True
        self._completion_poll_attempts = 0
        self._try_finish_workflow_completion()

    def _is_row_terminal_for_completion(self, row: int) -> bool:
        try:
            rr = int(row)
        except Exception:
            return True

        if rr < 0 or rr >= self.table.rowCount():
            return True

        code = self._status_code(rr)
        return code in {"SUCCESSFUL", "FAILED", "STOPPED", "CANCELED"}

    def _close_all_workflow_chrome_profiles(self) -> None:
        try:
            from chrome import kill_profile_chrome as kill_veo_chrome, resolve_profile_dir as resolve_veo_profile

            kill_veo_chrome(resolve_veo_profile())
        except Exception:
            pass
        try:
            from grok_chrome_manager import kill_profile_chrome as kill_grok_chrome, resolve_profile_dir as resolve_grok_profile

            kill_grok_chrome(resolve_grok_profile())
        except Exception:
            pass

    def _close_all_workflow_chrome_profiles_async(self) -> None:
        if bool(getattr(self, "_chrome_cleanup_running", False)):
            return
        setattr(self, "_chrome_cleanup_running", True)

        def _run() -> None:
            try:
                self._close_all_workflow_chrome_profiles()
            finally:
                try:
                    setattr(self, "_chrome_cleanup_running", False)
                except Exception:
                    pass

        try:
            threading.Thread(target=_run, daemon=True).start()
        except Exception:
            try:
                setattr(self, "_chrome_cleanup_running", False)
            except Exception:
                pass

    def _schedule_completion_poll(self, delay_ms: int = 500) -> None:
        if self._completion_poll_scheduled:
            return
        self._completion_poll_scheduled = True

        def _poll() -> None:
            self._completion_poll_scheduled = False
            self._try_finish_workflow_completion()

        try:
            QTimer.singleShot(max(100, int(delay_ms)), _poll)
        except Exception:
            self._completion_poll_scheduled = False

    def _try_finish_workflow_completion(self) -> None:
        if not self._awaiting_completion_confirmation:
            return

        rows = sorted(int(r) for r in list(self._active_queue_rows))
        if not rows:
            self._awaiting_completion_confirmation = False
            self.runStateChanged.emit(False)
            return

        all_terminal = True
        for r in rows:
            if not self._is_row_terminal_for_completion(r):
                all_terminal = False
                break

        if all_terminal:
            self._active_queue_rows.clear()
            self._awaiting_completion_confirmation = False
            self._completion_poll_attempts = 0
            self._completion_poll_scheduled = False
            self._close_all_workflow_chrome_profiles_async()
            self.runStateChanged.emit(False)
            return

        self._completion_poll_attempts += 1
        if self._completion_poll_attempts == 1:
            self._append_run_log("â³ ChÆ°a Ä‘á»§ Ä‘iá»u kiá»‡n hoÃ n thÃ nh, tiáº¿p tá»¥c chá» tráº¡ng thÃ¡i video thá»±c táº¿...")
        if self._completion_poll_attempts >= 600:
            self._append_run_log("âš ï¸ QuÃ¡ thá»i gian chá» xÃ¡c nháº­n hoÃ n thÃ nh, chuyá»ƒn workflow káº¿ tiáº¿p Ä‘á»ƒ trÃ¡nh káº¹t hÃ ng chá».")
            self._active_queue_rows.clear()
            self._awaiting_completion_confirmation = False
            self._completion_poll_attempts = 0
            self._completion_poll_scheduled = False
            self._close_all_workflow_chrome_profiles_async()
            self.runStateChanged.emit(False)
            return

        self._schedule_completion_poll(500)

    def retry_selected_rows(self) -> None:
        rows = self._selected_rows()
        if not rows:
            QMessageBox.warning(self, "ChÆ°a chá»n", "HÃ£y tÃ­ch chá»n cÃ¡c dÃ²ng cáº§n táº¡o láº¡i.")
            return
        self._clear_media_for_rows(rows, delete_files=True)
        self._start_rows_by_mode(rows)

    def _confirm_action(self, title: str, text: str) -> bool:
        return (
            QMessageBox.question(
                self,
                str(title or "XÃ¡c nháº­n"),
                str(text or "Báº¡n cÃ³ cháº¯c muá»‘n tiáº¿p tá»¥c?"),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Ok
        )

    def _load_merge_video_module(self):
        module_path = Path(__file__).resolve().parent.parent / "merge+video.py"
        if not module_path.exists():
            raise FileNotFoundError(f"KhÃ´ng tÃ¬m tháº¥y file: {module_path}")
        spec = importlib.util.spec_from_file_location("merge_plus_video", str(module_path))
        if spec is None or spec.loader is None:
            raise RuntimeError("KhÃ´ng load Ä‘Æ°á»£c module merge+video.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _video_path_of_row(self, row: int) -> str:
        it = self.table.item(int(row), self.COL_VIDEO)
        if it is None:
            return ""
        try:
            current = str(it.data(Qt.ItemDataRole.UserRole) or "").strip()
        except Exception:
            current = ""
        if current and os.path.isfile(current):
            return current
        try:
            video_map = dict(it.data(Qt.ItemDataRole.UserRole + 1) or {})
        except Exception:
            video_map = {}
        for idx in sorted(video_map.keys()):
            path = str(video_map.get(idx) or "").strip()
            if path and os.path.isfile(path):
                return path
        return ""

    def _collect_checked_video_paths(self) -> list[str]:
        paths: list[str] = []
        for r in self._selected_rows():
            p = self._video_path_of_row(r)
            if p:
                paths.append(p)
        return paths

    def _pick_external_videos(self) -> list[str]:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Chá»n video",
            str(getattr(self._cfg, "video_output_dir", str(BASE_DIR)) or str(BASE_DIR)),
            "Video files (*.mp4 *.mkv *.mov *.avi *.flv *.wmv *.webm *.m4v);;All files (*.*)",
        )
        return [str(x) for x in (files or []) if str(x).strip()]

    def _ask_video_source(self, action_name: str) -> list[str]:
        action = str(action_name or "").strip().lower()
        is_cut = "cáº¯t" in action

        title = "Cáº¯t áº£nh cuá»‘i" if is_cut else "Ná»‘i video"
        if is_cut:
            message = "Báº¡n muá»‘n cáº¯t áº£nh tá»« video Ä‘Ã£ chá»n hay duyá»‡t cáº¯t video khÃ¡c?"
            btn_selected_text = "Cáº¯t áº£nh video Ä‘Ã£ chá»n"
            btn_browse_text = "Duyá»‡t cáº¯t video khÃ¡c"
        else:
            message = "Báº¡n muá»‘n ná»‘i video Ä‘Ã£ chá»n hay duyá»‡t ná»‘i video khÃ¡c?"
            btn_selected_text = "Ná»‘i video Ä‘Ã£ chá»n"
            btn_browse_text = "Duyá»‡t ná»‘i video khÃ¡c"

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(message)
        btn_selected = box.addButton(btn_selected_text, QMessageBox.ButtonRole.AcceptRole)
        btn_browse = box.addButton(btn_browse_text, QMessageBox.ButtonRole.ActionRole)
        btn_cancel = box.addButton("Há»§y", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_selected)
        box.exec()

        clicked = box.clickedButton()
        if clicked == btn_cancel or clicked is None:
            return []
        if clicked == btn_selected:
            return self._collect_checked_video_paths()
        if clicked == btn_browse:
            return self._pick_external_videos()
        return []

    def _on_join_video_clicked(self) -> None:
        video_paths = self._ask_video_source("Ná»‘i video")
        if len(video_paths) < 2:
            QMessageBox.warning(self, "Thiáº¿u dá»¯ liá»‡u", "Cáº§n Ã­t nháº¥t 2 video Ä‘á»ƒ ná»‘i.")
            return
        try:
            mod = self._load_merge_video_module()
            base_out = Path(str(getattr(self._cfg, "video_output_dir", str(BASE_DIR)) or str(BASE_DIR)))
            merge_out = base_out / "Video Ä‘Ã£ ná»‘i"
            merged_file = mod.merge_videos(video_paths, str(merge_out), output_stem="video_da_noi")
            self._append_run_log(f"âœ… Ná»‘i video thÃ nh cÃ´ng: {merged_file}")
            QMessageBox.information(self, "Ná»‘i video", f"ÄÃ£ ná»‘i video thÃ nh cÃ´ng:\n{merged_file}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(merge_out)))
        except Exception as exc:
            QMessageBox.critical(self, "Lá»—i ná»‘i video", str(exc))

    def _on_retry_selected_clicked(self) -> None:
        if not self._confirm_action("XÃ¡c nháº­n", "Báº¡n cÃ³ cháº¯c muá»‘n táº¡o láº¡i video cho cÃ¡c dÃ²ng Ä‘Ã£ chá»n?"):
            return
        self.retry_selected_rows()

    def _on_retry_failed_clicked(self) -> None:
        if not self._confirm_action("XÃ¡c nháº­n", "Báº¡n cÃ³ cháº¯c muá»‘n táº¡o láº¡i táº¥t cáº£ video lá»—i?"):
            return
        self.retry_failed_rows()

    def _on_cut_last_clicked(self) -> None:
        video_paths = self._ask_video_source("Cáº¯t áº£nh cuá»‘i")
        if not video_paths:
            QMessageBox.warning(self, "Thiáº¿u dá»¯ liá»‡u", "KhÃ´ng cÃ³ video Ä‘á»ƒ cáº¯t áº£nh cuá»‘i.")
            return
        try:
            mod = self._load_merge_video_module()
            base_out = Path(str(getattr(self._cfg, "video_output_dir", str(BASE_DIR)) or str(BASE_DIR)))
            frame_out = base_out / "Frame cuá»‘i video"
            frames = mod.extract_last_frames(video_paths, str(frame_out))
            self._append_run_log(f"âœ… Cáº¯t frame cuá»‘i thÃ nh cÃ´ng: {len(frames)} áº£nh")
            QMessageBox.information(self, "Cáº¯t áº£nh cuá»‘i", f"ÄÃ£ cáº¯t {len(frames)} áº£nh cuá»‘i video.")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(frame_out)))
        except Exception as exc:
            QMessageBox.critical(self, "Lá»—i cáº¯t áº£nh cuá»‘i", str(exc))

    def retry_failed_rows(self) -> None:
        rows: list[int] = []
        for r in range(self.table.rowCount()):
            if self._status_code(r) == "FAILED":
                rows.append(r)
        if not rows:
            QMessageBox.information(self, "KhÃ´ng cÃ³ lá»—i", "KhÃ´ng cÃ³ dÃ²ng lá»—i Ä‘á»ƒ táº¡o láº¡i.")
            return
        self._start_rows_by_mode(rows)
