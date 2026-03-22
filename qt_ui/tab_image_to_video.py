from __future__ import annotations
import os
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
                             QFileDialog, QLineEdit, QTableWidget, QTableWidgetItem, 
                             QAbstractItemView, QMessageBox, QTabWidget)
from . import tab_text_to_video
from .tab_text_to_video import PromptEditor
import settings_manager
from settings_manager import get_icon_path

def _icon(name: str) -> QIcon:
    if not name:
        return QIcon()
    path = get_icon_path(name)
    if os.path.isfile(path):
        return QIcon(path)
    return QIcon()

class _ClickableThumb(QLabel):
    def __init__(self, parent: QWidget | None = None, on_click=None):
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and callable(self._on_click):
            try:
                self._on_click()
            except Exception:
                pass
        super().mousePressEvent(event)

class _SingleImageTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._images = []
        self._prompt_overrides = []
        
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync_table)
        
        root = QVBoxLayout(self)
        
        row1 = QHBoxLayout()
        step1 = QLabel('Bước 1: Chọn hàng loạt ảnh (Sẽ crop về tỷ lệ đã chọn)')
        step1.setStyleSheet('font-weight: 600; color: #1f2d48;')
        row1.addWidget(step1)
        row1.addStretch(1)
        
        self.btn_clear = QPushButton('Xóa')
        self.btn_clear.clicked.connect(self.confirm_clear_all)
        self.btn_clear.setEnabled(False)
        row1.addWidget(self.btn_clear)
        
        self.btn_pick = QPushButton('Chưa chọn ảnh')
        self.btn_pick.setIcon(_icon('folder_icon.png'))
        self.btn_pick.clicked.connect(self.pick_images)
        row1.addWidget(self.btn_pick)
        
        for btn in (self.btn_clear, self.btn_pick):
            btn.setFixedHeight(34)
            
        self.btn_clear.setFixedWidth(64)
        self.btn_pick.setMinimumWidth(180)
        
        root.addLayout(row1)
        
        step2 = QLabel('Bước 2: Nhập hàng loạt prompt tương ứng')
        step2.setStyleSheet('font-weight: 600; color: #1f2d48;')
        root.addWidget(step2)
        
        self.prompts = PromptEditor()
        self.prompts.setPlaceholderText("- Dán hàng loạt prompt vào, mỗi dòng là 1 prompt. Tool TỰ ĐỘNG gán prompt vào ảnh theo thứ tự\n- KHÔNG CHẤP NHẬN ẢNH NHẠY CẢM, NGƯỜI NỔI TIẾNG, TRẺ EM, BẠO LỰC...")
        self.prompts.textChanged.connect(self._schedule_sync_table)
        self.prompts.setFixedHeight(130)
        root.addWidget(self.prompts)
        
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['STT', 'Ảnh', 'Prompt'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(1, 248)
        
        self.table.verticalHeader().setDefaultSectionSize(116)
        root.addWidget(self.table, 1)

    def _ensure_len(self, lst: list[str], n: int) -> None:
        nn = int(n)
        if nn < 0: return
        while len(lst) < nn:
            lst.append('')

    def _ensure_rows(self, n: int) -> None:
        nn = int(n)
        if nn < 0: return
        self._ensure_len(self._images, nn)
        self._ensure_len(self._prompt_overrides, nn)

    def _remove_row(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._images), len(self._prompt_overrides))
        if i < 0 or i >= max_len:
            return
        try:
            self._ensure_rows(max_len)
            del self._images[i]
            del self._prompt_overrides[i]
            self._refresh_buttons_state()
            self._sync_table()
        except Exception:
            pass

    def _move_up(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._images), len(self._prompt_overrides))
        if i <= 0 or i >= max_len:
            return
        self._ensure_rows(max_len)
        self._images[i], self._images[i-1] = self._images[i-1], self._images[i]
        self._prompt_overrides[i], self._prompt_overrides[i-1] = self._prompt_overrides[i-1], self._prompt_overrides[i]
        self._sync_table()

    def _move_down(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._images), len(self._prompt_overrides))
        if i < 0 or i >= max_len - 1:
            return
        self._ensure_rows(max_len)
        self._images[i], self._images[i+1] = self._images[i+1], self._images[i]
        self._prompt_overrides[i], self._prompt_overrides[i+1] = self._prompt_overrides[i+1], self._prompt_overrides[i]
        self._sync_table()

    def _build_image_cell(self, idx: int, path: str) -> tuple[QWidget, int]:
        wrap = QWidget()
        outer = QHBoxLayout(wrap)
        outer.setContentsMargins(1, 2, 1, 2)
        outer.setSpacing(3)
        
        left = QWidget()
        l = QVBoxLayout(left)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)
        
        thumb = _ClickableThumb(on_click=lambda: self._pick_single(idx))
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        scaled_h = 0
        if path:
            pix = QPixmap(path)
        else:
            pix = QPixmap()
            
        if not pix.isNull():
            pix = pix.scaled(206, 124, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            thumb.setPixmap(pix)
            thumb.setFixedSize(pix.size())
            scaled_h = int(pix.height())
            
        if path:
            txt = os.path.basename(path)
        else:
            txt = '(Click chọn)'
            
        btn_name = QPushButton(txt)
        btn_name.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_name.setFlat(True)
        btn_name.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_name.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;}')
        
        if path:
            btn_name.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;color:#31456a; font-size:10px; font-weight:600;}')
        else:
            btn_name.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;color:#2563eb; font-size:12px; font-weight:800;}')
            
        btn_name.clicked.connect(lambda: self._pick_single(idx))
        
        l.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)
        l.addWidget(btn_name, 0, Qt.AlignmentFlag.AlignHCenter)
        
        outer.addWidget(left, 1)
        
        btns = QWidget()
        b = QVBoxLayout(btns)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(5)
        
        btn_del = QPushButton('x')
        btn_del.setFixedSize(22, 22)
        btn_del.setStyleSheet('background:#fee2e2; border:1px solid #fecaca; border-radius:11px;color:#dc2626; font-weight:400; font-size:14px; padding:0px;')
        btn_del.clicked.connect(lambda: self._remove_row(idx))
        
        btn_up = QPushButton('▲')
        btn_up.setFixedSize(22, 22)
        btn_up.clicked.connect(lambda: self._move_up(idx))
        
        btn_down = QPushButton('▼')
        btn_down.setFixedSize(22, 22)
        btn_down.clicked.connect(lambda: self._move_down(idx))
        
        btn_up.setStyleSheet('background:#eaf2ff; border:1px solid #c8d7f2; border-radius:11px;color:#3e5784; font-weight:500; font-size:12px; padding:0px;')
        btn_down.setStyleSheet('background:#eaf2ff; border:1px solid #c8d7f2; border-radius:11px;color:#3e5784; font-weight:500; font-size:12px; padding:0px;')
        
        if idx <= 0:
            btn_up.setEnabled(False)
            
        max_len = max(len(self._images), len(self._prompt_overrides))
        if idx >= max_len - 1:
            btn_down.setEnabled(False)
            
        b.addWidget(btn_del, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addWidget(btn_up, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addWidget(btn_down, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addStretch(1)
        
        outer.addWidget(btns, 0, Qt.AlignmentFlag.AlignTop)
        
        target_h = max(132, scaled_h + 30)
        return wrap, int(target_h)

    def _build_prompt_cell(self, row: int, default_text: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText('Prompt cho ảnh này...')
        r = int(row)
        if r < 0: r = 0
        self._ensure_rows(r + 1)
        
        if self._prompt_overrides[r] and self._prompt_overrides[r].strip():
            current = self._prompt_overrides[r]
        else:
            current = default_text
            
        edit.setText(str(current) if current else '')
        edit.textChanged.connect(lambda t: self._prompt_overrides.__setitem__(int(r), str(t)))
        return edit

    def _pick_single(self, idx: int) -> None:
        i = int(idx)
        f, _ = QFileDialog.getOpenFileName(self, f'Chọn ảnh (dòng {i+1})', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if f:
            self._ensure_rows(i + 1)
            self._images[i] = str(f)
            self._sync_table()

    def pick_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, 'Chọn ảnh', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if not files:
            return
            
        picked = [str(p) for p in files]
        rows = max(len(self._images), len(self._prompt_overrides))
        self._ensure_rows(max(1, rows))
        
        try:
            start_at = self._images.index('')
        except ValueError:
            start_at = len(self._images)
            
        i = int(start_at)
        for p in picked:
            if i >= len(self._images):
                self._ensure_rows(i + 1)
            self._images[i] = p
            i += 1
            
        self._refresh_buttons_state()
        self._sync_table()

    def confirm_clear_all(self) -> None:
        if not self._images and not self.prompts.toPlainText() and not ''.strip():
            return
            
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle('Xác nhận xóa')
        msg.setText('Xóa hết ảnh và prompt?')
        msg.setInformativeText('Thao tác này sẽ xóa toàn bộ ảnh và prompt đã nhập.')
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        
        msg.button(QMessageBox.StandardButton.Yes).setText('Xóa hết')
        msg.button(QMessageBox.StandardButton.No).setText('Hủy')
        
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self.clear_all()

    def clear_all(self) -> None:
        self._images = []
        self._prompt_overrides = []
        self.prompts.setPlainText('')
        self._refresh_buttons_state()
        self._sync_table()

    def _refresh_buttons_state(self) -> None:
        has_any = bool(self._images) or bool(self.prompts.toPlainText()) or bool(''.strip())
        self.btn_clear.setEnabled(has_any)
        
        n = len(self._images)
        if n:
            self.btn_pick.setText(f'Đã chọn {n} ảnh')
        else:
            self.btn_pick.setText('Chưa chọn ảnh')

    def _get_prompts(self) -> list[str]:
        if self.prompts.toPlainText():
            raw = ''
            try:
                raw = self.prompts.toPlainText().replace('\r\n', '\n').replace('\r', '\n')
                lines = [ln.strip() for ln in raw.split('\n')]
                return [ln for ln in lines if ln]
            except Exception:
                return []
        return []

    def _schedule_sync_table(self) -> None:
        self._sync_timer.start(140)

    def _effective_image_rows(self) -> int:
        last = -1
        for i, path in enumerate(self._images):
            if str(path) and str(path).strip():
                last = i
        return last + 1

    def _sync_table(self) -> None:
        v_scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(0)
        
        prompts = self._get_prompts()
        rows = max(self._effective_image_rows(), len(self._prompt_overrides))
        self._ensure_rows(rows)
        
        for i in range(rows):
            self.table.insertRow(i)
            
            stt = QTableWidgetItem(str(i + 1))
            stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 0, stt)
            
            p = self._images[i] if i < len(self._images) else ''
            image_widget, row_h = self._build_image_cell(i, p)
            self.table.setCellWidget(i, 1, image_widget)
            
            default_prompt = prompts[i] if i < len(prompts) else ''
            self.table.setCellWidget(i, 2, self._build_prompt_cell(i, default_prompt))
            
            self.table.setRowHeight(i, int(row_h))
            
        self.table.verticalScrollBar().setValue(int(v_scroll))
        self._refresh_buttons_state()

    def get_items(self) -> list[dict]:
        prompts = self._get_prompts()
        rows = max(self._effective_image_rows(), len(self._prompt_overrides))
        self._ensure_rows(rows)
        
        items = []
        for i in range(rows):
            image_link = str(self._images[i] if self._images[i] else '').strip()
            prompt_text = str(self._prompt_overrides[i] if self._prompt_overrides[i] else '').strip()
            
            if not prompt_text and i < len(prompts):
                prompt_text = str(prompts[i] if prompts[i] else '').strip()
                
            if image_link or prompt_text:
                items.append({
                    'id': str(i + 1),
                    'prompt': prompt_text,
                    'image_link': image_link
                })
        return items

class _StartEndImageTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._start_images = []
        self._end_images = []
        self._prompt_overrides = []
        
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync_table)
        
        root = QVBoxLayout(self)
        
        row1 = QHBoxLayout()
        step1 = QLabel('Bước 1: Chọn hàng loạt ảnh BẮT ĐẦU')
        step1.setStyleSheet('font-weight: 600; color: #1f2d48;')
        row1.addWidget(step1)
        row1.addStretch(1)
        
        self.btn_clear_start = QPushButton('Xóa')
        self.btn_clear_start.clicked.connect(self.confirm_clear_all)
        self.btn_clear_start.setEnabled(False)
        row1.addWidget(self.btn_clear_start)
        
        self.btn_pick_start = QPushButton('Chưa chọn ảnh bắt đầu')
        self.btn_pick_start.setIcon(_icon('folder_icon.png'))
        self.btn_pick_start.clicked.connect(self.pick_start_images)
        row1.addWidget(self.btn_pick_start)
        
        for btn in (self.btn_clear_start, self.btn_pick_start):
            btn.setFixedHeight(34)
            
        self.btn_clear_start.setFixedWidth(64)
        self.btn_pick_start.setMinimumWidth(240)
        
        root.addLayout(row1)
        
        row2 = QHBoxLayout()
        step2 = QLabel('Bước 2: Chọn hàng loạt ảnh KẾT THÚC')
        step2.setStyleSheet('font-weight: 600; color: #1f2d48;')
        row2.addWidget(step2)
        row2.addStretch(1)
        
        self.btn_clear_end = QPushButton('Xóa')
        self.btn_clear_end.clicked.connect(self.confirm_clear_all)
        self.btn_clear_end.setEnabled(False)
        row2.addWidget(self.btn_clear_end)
        
        self.btn_pick_end = QPushButton('Chưa chọn ảnh kết thúc')
        self.btn_pick_end.setIcon(_icon('folder_icon.png'))
        self.btn_pick_end.clicked.connect(self.pick_end_images)
        row2.addWidget(self.btn_pick_end)
        
        for btn in (self.btn_clear_end, self.btn_pick_end):
            btn.setFixedHeight(34)
            
        self.btn_clear_end.setFixedWidth(64)
        self.btn_pick_end.setMinimumWidth(240)
        
        root.addLayout(row2)
        
        step3 = QLabel('Bước 3: Nhập hàng loạt prompt tương ứng')
        step3.setStyleSheet('font-weight: 600; color: #1f2d48;')
        root.addWidget(step3)
        
        self.prompts = PromptEditor()
        self.prompts.setStyleSheet('font-size: 12px;')
        self.prompts.setPlaceholderText("- Dán hàng loạt prompt vào, mỗi dòng là 1 prompt. Tool TỰ ĐỘNG gán prompt vào ảnh theo thứ tự\n- Tool TỰ ĐỘNG ghép cặp: ảnh_start[0] + ảnh_end[0] + prompt[0], ...\n- KHÔNG CHẤP NHẬN ẢNH NHẠY CẢM, NGƯỜI NỔI TIẾNG, TRẺ EM, BẠO LỰC...")
        self.prompts.textChanged.connect(self._schedule_sync_table)
        self.prompts.setFixedHeight(130)
        root.addWidget(self.prompts)
        
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['STT', 'Ảnh Start', 'Ảnh End', 'Prompt'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 44)
        self.table.setColumnWidth(1, 172)
        self.table.setColumnWidth(2, 172)
        
        self.table.verticalHeader().setDefaultSectionSize(98)
        root.addWidget(self.table, 1)

    def _ensure_len(self, lst: list[str], n: int) -> None:
        nn = int(n)
        if nn < 0: return
        while len(lst) < nn:
            lst.append('')

    def _ensure_rows(self, n: int) -> None:
        nn = int(n)
        if nn < 0: return
        self._ensure_len(self._start_images, nn)
        self._ensure_len(self._end_images, nn)
        self._ensure_len(self._prompt_overrides, nn)

    def _remove_row(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        if i < 0 or i >= max_len:
            return
        try:
            self._ensure_rows(max_len)
            del self._start_images[i]
            del self._end_images[i]
            del self._prompt_overrides[i]
            self._refresh_buttons_state()
            self._sync_table()
        except Exception:
            pass

    def _move_up(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        if i <= 0 or i >= max_len:
            return
        self._ensure_rows(max_len)
        self._start_images[i], self._start_images[i-1] = self._start_images[i-1], self._start_images[i]
        self._end_images[i], self._end_images[i-1] = self._end_images[i-1], self._end_images[i]
        self._prompt_overrides[i], self._prompt_overrides[i-1] = self._prompt_overrides[i-1], self._prompt_overrides[i]
        self._sync_table()

    def _move_down(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        if i < 0 or i >= max_len - 1:
            return
        self._ensure_rows(max_len)
        self._start_images[i], self._start_images[i+1] = self._start_images[i+1], self._start_images[i]
        self._end_images[i], self._end_images[i+1] = self._end_images[i+1], self._end_images[i]
        self._prompt_overrides[i], self._prompt_overrides[i+1] = self._prompt_overrides[i+1], self._prompt_overrides[i]
        self._sync_table()

    def _clear_end_image(self, idx: int) -> None:
        i = int(idx)
        max_len = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        if i < 0: return
        self._ensure_rows(max_len)
        self._end_images[i] = ''
        self._sync_table()

    def _pick_single_start(self, idx: int) -> None:
        i = int(idx)
        f, _ = QFileDialog.getOpenFileName(self, f'Chọn ảnh BẮT ĐẦU (dòng {i+1})', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if f:
            self._ensure_rows(i + 1)
            self._start_images[i] = str(f)
            self._sync_table()

    def _pick_single_end(self, idx: int) -> None:
        i = int(idx)
        f, _ = QFileDialog.getOpenFileName(self, f'Chọn ảnh KẾT THÚC (dòng {i+1})', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if f:
            self._ensure_rows(i + 1)
            self._end_images[i] = str(f)
            self._sync_table()

    def _build_image_cell(self, idx: int, path: str, role: str='start') -> tuple[QWidget, int]:
        wrap = QWidget()
        outer = QHBoxLayout(wrap)
        outer.setContentsMargins(1, 2, 1, 2)
        outer.setSpacing(3)
        
        left = QWidget()
        l = QVBoxLayout(left)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(4)
        
        if role == 'start':
            thumb = _ClickableThumb(on_click=lambda: self._pick_single_start(idx))
        else:
            thumb = _ClickableThumb(on_click=lambda: self._pick_single_end(idx))
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        scaled_h = 0
        if path:
            pix = QPixmap(path)
        else:
            pix = QPixmap()
            
        if not pix.isNull():
            pix = pix.scaled(138, 86, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            thumb.setPixmap(pix)
            thumb.setFixedSize(pix.size())
            scaled_h = int(pix.height())
            
        if path:
            name_txt = os.path.basename(path)
        else:
            name_txt = '(Click chọn)'
            
        name_btn = QPushButton(name_txt)
        name_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        name_btn.setFlat(True)
        name_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        name_btn.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;}')
        
        if path:
            name_btn.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;color:#31456a; font-size:10px; font-weight:600;}')
        else:
            name_btn.setStyleSheet('QPushButton{border:none; background:transparent; padding:0px; text-align:center;color:#2563eb; font-size:12px; font-weight:800;}')
            
        if role == 'start':
            name_btn.clicked.connect(lambda: self._pick_single_start(idx))
        else:
            name_btn.clicked.connect(lambda: self._pick_single_end(idx))
            
        l.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)
        l.addWidget(name_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        
        outer.addWidget(left, 1)
        
        btns = QWidget()
        b = QVBoxLayout(btns)
        b.setContentsMargins(0, 0, 0, 0)
        b.setSpacing(5)
        
        btn_del = QPushButton('x')
        btn_del.setFixedSize(22, 22)
        btn_del.setStyleSheet('background:#fee2e2; border:1px solid #fecaca; border-radius:11px;color:#dc2626; font-weight:400; font-size:14px; padding:0px;')
        
        if role == 'start':
            btn_del.clicked.connect(lambda: self._remove_row(idx))
        else:
            btn_del.clicked.connect(lambda: self._clear_end_image(idx))
            
        btn_up = QPushButton('▲')
        btn_up.setFixedSize(22, 22)
        btn_up.clicked.connect(lambda: self._move_up(idx))
        
        btn_down = QPushButton('▼')
        btn_down.setFixedSize(22, 22)
        btn_down.clicked.connect(lambda: self._move_down(idx))
        
        btn_up.setStyleSheet('background:#eaf2ff; border:1px solid #c8d7f2; border-radius:11px;color:#3e5784; font-weight:500; font-size:12px; padding:0px;')
        btn_down.setStyleSheet('background:#eaf2ff; border:1px solid #c8d7f2; border-radius:11px;color:#3e5784; font-weight:500; font-size:12px; padding:0px;')
        
        if idx <= 0:
            btn_up.setEnabled(False)
            
        max_len = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        if idx >= max_len - 1:
            btn_down.setEnabled(False)
            
        b.addWidget(btn_del, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addWidget(btn_up, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addWidget(btn_down, 0, Qt.AlignmentFlag.AlignHCenter)
        b.addStretch(1)
        
        outer.addWidget(btns, 0, Qt.AlignmentFlag.AlignTop)
        
        target_h = max(108, scaled_h + 28)
        return wrap, int(target_h)

    def _build_prompt_cell(self, row: int, default_text: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText('Prompt cho ảnh này...')
        r = int(row)
        if r < 0: r = 0
        self._ensure_rows(r + 1)
        
        if self._prompt_overrides[r] and self._prompt_overrides[r].strip():
            current = self._prompt_overrides[r]
        else:
            current = default_text
            
        edit.setText(str(current) if current else '')
        edit.textChanged.connect(lambda t: self._prompt_overrides.__setitem__(int(r), str(t)))
        return edit

    def pick_start_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, 'Chọn ảnh BẮT ĐẦU', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if not files:
            return
            
        picked = [str(p) for p in files]
        rows = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        self._ensure_rows(max(1, rows))
        
        try:
            start_at = self._start_images.index('')
        except ValueError:
            start_at = len(self._start_images)
            
        i = int(start_at)
        for p in picked:
            if i >= len(self._start_images):
                self._ensure_rows(i + 1)
            self._start_images[i] = p
            i += 1
            
        self._refresh_buttons_state()
        self._sync_table()

    def pick_end_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, 'Chọn ảnh KẾT THÚC', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if not files:
            return
            
        picked = [str(p) for p in files]
        rows = max(len(self._start_images), len(self._end_images), len(self._prompt_overrides))
        self._ensure_rows(max(1, rows))
        
        try:
            start_at = self._end_images.index('')
        except ValueError:
            start_at = len(self._end_images)
            
        i = int(start_at)
        for p in picked:
            if i >= len(self._end_images):
                self._ensure_rows(i + 1)
            self._end_images[i] = p
            i += 1
            
        self._refresh_buttons_state()
        self._sync_table()

    def confirm_clear_all(self) -> None:
        if not self._start_images and not self._end_images and not self.prompts.toPlainText() and not ''.strip():
            return
            
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle('Xác nhận xóa')
        msg.setText('Xóa hết ảnh và prompt?')
        msg.setInformativeText('Thao tác này sẽ xóa toàn bộ danh sách ảnh đã chọn, toàn bộ prompt đã nhập, và prompt đã sửa theo từng ảnh.')
        msg.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg.setDefaultButton(QMessageBox.StandardButton.No)
        
        msg.button(QMessageBox.StandardButton.Yes).setText('Xóa hết')
        msg.button(QMessageBox.StandardButton.No).setText('Hủy')
        
        if msg.exec() == QMessageBox.StandardButton.Yes:
            self.clear_all()

    def clear_all(self) -> None:
        self._start_images = []
        self._end_images = []
        self._prompt_overrides = []
        self.prompts.setPlainText('')
        self._refresh_buttons_state()
        self._sync_table()

    def _refresh_buttons_state(self) -> None:
        has_any = bool(self._start_images) or bool(self._end_images) or bool(self.prompts.toPlainText()) or bool(''.strip())
        self.btn_clear_start.setEnabled(has_any)
        self.btn_clear_end.setEnabled(has_any)
        
        n1 = len(self._start_images)
        n2 = len(self._end_images)
        
        if n1:
            self.btn_pick_start.setText(f'Đã chọn {n1} ảnh bắt đầu')
        else:
            self.btn_pick_start.setText('Chưa chọn ảnh bắt đầu')
            
        if n2:
            self.btn_pick_end.setText(f'Đã chọn {n2} ảnh kết thúc')
        else:
            self.btn_pick_end.setText('Chưa chọn ảnh kết thúc')

    def _get_prompts(self) -> list[str]:
        if self.prompts.toPlainText():
            raw = ''
            try:
                raw = self.prompts.toPlainText().replace('\r\n', '\n').replace('\r', '\n')
                lines = [ln.strip() for ln in raw.split('\n')]
                return [ln for ln in lines if ln]
            except Exception:
                return []
        return []

    def _effective_image_rows(self) -> int:
        last = -1
        max_len = max(len(self._start_images), len(self._end_images))
        for i in range(max_len):
            sp = str(self._start_images[i] if i < len(self._start_images) else '').strip()
            ep = str(self._end_images[i] if i < len(self._end_images) else '').strip()
            if sp or ep:
                last = i
        return last + 1

    def _schedule_sync_table(self) -> None:
        self._sync_timer.start(140)

    def _sync_table(self) -> None:
        v_scroll = self.table.verticalScrollBar().value()
        self.table.setRowCount(0)
        
        prompts = self._get_prompts()
        rows = max(self._effective_image_rows(), len(self._prompt_overrides))
        self._ensure_rows(rows)
        
        for i in range(rows):
            self.table.insertRow(i)
            
            stt = QTableWidgetItem(str(i + 1))
            stt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 0, stt)
            
            sp = ''
            if i < len(self._start_images):
                sp = self._start_images[i]
            
            ep = ''
            if i < len(self._end_images):
                ep = self._end_images[i]
                
            start_widget, row_h1 = self._build_image_cell(i, sp, role='start')
            end_widget, row_h2 = self._build_image_cell(i, ep, role='end')
            
            self.table.setCellWidget(i, 1, start_widget)
            self.table.setCellWidget(i, 2, end_widget)
            
            default_prompt = ''
            if i < len(prompts):
                default_prompt = prompts[i]
                
            self.table.setCellWidget(i, 3, self._build_prompt_cell(i, default_prompt))
            
            self.table.setRowHeight(i, int(max(row_h1, row_h2)))
            
        self.table.verticalScrollBar().setValue(int(v_scroll))
        self._refresh_buttons_state()

    def get_items(self) -> list[dict]:
        prompts = self._get_prompts()
        rows = max(self._effective_image_rows(), len(self._prompt_overrides))
        self._ensure_rows(rows)
        
        items = []
        for i in range(rows):
            start_image_link = str(self._start_images[i] if self._start_images[i] else '').strip()
            end_image_link = str(self._end_images[i] if self._end_images[i] else '').strip()
            prompt_text = str(self._prompt_overrides[i] if self._prompt_overrides[i] else '').strip()
            
            if not prompt_text and i < len(prompts):
                prompt_text = str(prompts[i] if prompts[i] else '').strip()
                
            if start_image_link or end_image_link:
                items.append({
                    'id': str(i + 1),
                    'prompt': prompt_text,
                    'start_image_link': start_image_link,
                    'end_image_link': end_image_link
                })
        return items

class ImageToVideoTab(QWidget):
    """Container tab with 2 sub-tabs, kept in a single UI file.

    - Tab 1: Tạo Video Từ Ảnh
    - Tab 2: Tạo video từ Ảnh Đầu - Ảnh Cuối
    """
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        
        self.sub_tabs = QTabWidget()
        self.single_tab = _SingleImageTab()
        self.start_end_tab = _StartEndImageTab()
        
        self.sub_tabs.addTab(self.single_tab, 'Tạo Video Từ Ảnh')
        self.sub_tabs.addTab(self.start_end_tab, 'Tạo video từ Ảnh Đầu - Ảnh Cuối')
        
        root.addWidget(self.sub_tabs, 1)

    def current_mode(self) -> str:
        if self.sub_tabs.currentIndex() == 1:
            return 'start_end'
        return 'single'

    def get_workflow_items(self) -> list[dict]:
        if self.current_mode() == 'start_end':
            return self.start_end_tab.get_items()
        return self.single_tab.get_items()
