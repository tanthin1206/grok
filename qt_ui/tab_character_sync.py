from __future__ import annotations
import os
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PyQt6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, 
                             QLineEdit, QMessageBox, QPushButton, QScrollArea, 
                             QToolButton, QVBoxLayout, QWidget)
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

def _is_image_file(path: str) -> bool:
    if not str(path):
        return False
    p = str(path).lower().strip()
    return p.endswith(('.png', '.jpg', '.jpeg', '.webp', '.bmp'))

class _DropArea(QFrame):
    def __init__(self, tab: 'CharacterSyncTab'):
        super().__init__(tab)
        self._tab = tab
        self.setAcceptDrops(True)
        self.setObjectName('DropArea')
        
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)
        
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        lay.addWidget(self._scroll, 1)
        
        self._body = QWidget()
        self._scroll.setWidget(self._body)
        
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(10)
        
        self._empty = QLabel('Click để chọn ảnh\nhoặc kéo ảnh vào khu vực này,\nsau đó đặt tên nhân vật\n(tối đa 10 ảnh)')
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet('color:#64748b; font-weight:600;')
        self._body_lay.addWidget(self._empty, 1)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self._tab.pick_images()
            except Exception:
                pass
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except Exception:
            pass
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        try:
            paths = []
            for u in event.mimeData().urls():
                p = u.toLocalFile()
                if p and os.path.isfile(p) and _is_image_file(p):
                    paths.append(str(p))
            
            if paths:
                self._tab.add_images(paths)
                event.acceptProposedAction()
                return
            event.ignore()
        except Exception:
            pass

    def set_cards(self, cards: list[QWidget]) -> None:
        while self._body_lay.count():
            it = self._body_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        
        if not cards:
            self._body_lay.addWidget(self._empty, 1)
            return
            
        self._body_lay.addStretch(0)
        for c in cards:
            self._body_lay.addWidget(c)
        self._body_lay.addStretch(1)

class _CharacterCard(QWidget):
    def __init__(self, tab: 'CharacterSyncTab', idx: int, path: str, name: str):
        super().__init__(tab)
        self._tab = tab
        self._idx = int(idx)
        self._path = str(path)
        
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        
        img_wrap = QWidget()
        g = QGridLayout(img_wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(0)
        
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setStyleSheet('background:#f1f7ff; border:1px solid #c8d7f2; border-radius:8px;')
        g.addWidget(self._img, 0, 0)
        
        btn = QToolButton()
        btn.setText('×')
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(22, 22)
        btn.setStyleSheet('background:#64748b; color:white; border:none; border-radius:11px;font-weight:900; font-size:16px;')
        btn.clicked.connect(lambda: self._tab.remove_image(self._idx))
        g.addWidget(btn, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        
        root.addWidget(img_wrap)
        
        self._name = QLineEdit()
        self._name.setPlaceholderText('Đặt tên nhân vật cho ảnh này...')
        self._name.setText(str(name) if name else '')
        self._name.textChanged.connect(lambda t: self._tab.set_name(self._idx, str(t)))
        root.addWidget(self._name)
        
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        try:
            pix = QPixmap(self._path)
            if pix.isNull():
                self._img.setText('(không đọc được ảnh)')
                self._img.setMinimumHeight(120)
                return
            
            w = max(1, int(self.width()))
            target_w = max(200, w)
            target_h = 135
            
            scaled = pix.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self._img.setPixmap(scaled)
            self._img.setFixedHeight(max(120, int(scaled.height())))
        except Exception:
            pix = QPixmap()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_pixmap()

class CharacterSyncTab(QWidget):
    """UI-only tab matching the provided mock for 'Đồng bộ nhân vật'."""
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items = []
        
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)
        
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        
        title = QLabel('Prompt hàng loạt')
        title.setStyleSheet('font-weight:700; color:#1f2d48;')
        ll.addWidget(title)
        
        self.prompts = PromptEditor()
        self.prompts.setPlaceholderText("- Dán hàng loạt prompt, mỗi dòng 1 prompt\n\n- Chọn 10 ảnh nhân vật và đặt tên riêng cho nhân vật\n\n- Gọi tên nhân vật (chỉ tên) và mô tả hành động của nhân vật, bối cảnh...\n\n*LƯU Ý :\n- Ảnh nhân vật nên up ảnh nền trắng hoặc png ko nền\n- Tên nhân vật nên đặt từ 4 ký tự trở lên\n- Prompt có tối đa 3 nhân vật, nếu nhiều hơn, hãy ghép 2 nhân vật trong 1 ảnh")
        ll.addWidget(self.prompts, 1)
        
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        
        hdr = QHBoxLayout()
        hdr.setContentsMargins(14, 0, 14, 0)
        
        self.btn_pick = QPushButton('Chọn ảnh nhân vật (tối đa 10)')
        try:
            ic = _icon('folder_icon.png')
            if not ic.isNull():
                self.btn_pick.setIcon(ic)
        except Exception:
            pass
        self.btn_pick.clicked.connect(self.pick_images)
        hdr.addWidget(self.btn_pick)
        hdr.addStretch(1)
        
        rl.addLayout(hdr)
        
        self.drop = _DropArea(self)
        self.drop.setStyleSheet('QFrame#DropArea{border:1px dashed #c8d7f2; border-radius:10px; background:#eef5ff;}')
        rl.addWidget(self.drop, 1)
        
        root.addWidget(left, 3)
        root.addWidget(right, 2)
        
        self._refresh_cards()

    def pick_images(self) -> None:
        if len(self._items) >= 10:
            self._show_max_message()
            return
            
        files, _ = QFileDialog.getOpenFileNames(self, 'Chọn ảnh nhân vật', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)')
        if not files:
            return
            
        try:
            self.add_images([str(p) for p in files])
        except Exception:
            pass

    def add_images(self, paths: list[str]) -> None:
        if len(self._items) >= 10:
            self._show_max_message()
            return
            
        remaining = max(0, 10 - len(self._items))
        if remaining <= 0:
            self._show_max_message()
            return
            
        truncated = list(paths)[:remaining]
        for p in truncated:
            if p and os.path.isfile(p) and _is_image_file(p):
                if len(self._items) >= 10:
                    break
                self._items.append({'path': str(p), 'name': ''})
        
        if len(paths) > len(truncated) or len(self._items) >= 10:
            self._show_max_message()
            
        self._refresh_cards()

    def _show_max_message(self) -> None:
        QMessageBox.information(self, 'Đủ số lượng', 'Đã đủ số lượng nhân vật tối đa (10 ảnh).')

    def remove_image(self, idx: int) -> None:
        i = int(idx)
        if i < 0 or i >= len(self._items):
            return
        try:
            del self._items[i]
            self._refresh_cards()
        except Exception:
            pass

    def set_name(self, idx: int, name: str) -> None:
        i = int(idx)
        if i < 0 or i >= len(self._items):
            return
        self._items[i]['name'] = str(name) if name else ''

    def _refresh_cards(self) -> None:
        cards = []
        for i, it in enumerate(self._items):
            cards.append(_CharacterCard(self, i, it.get('path', ''), it.get('name', '')))
        self.drop.set_cards(cards)

    def get_prompts(self) -> list[str]:
        if self.prompts.toPlainText():
            raw = ''
            try:
                raw = self.prompts.toPlainText().replace('\r\n', '\n').replace('\r', '\n')
                lines = [ln.strip() for ln in raw.split('\n')]
                return [ln for ln in lines if ln]
            except Exception:
                return []
        return []

    def get_character_items(self) -> list[dict]:
        out = []
        if self._items:
            for item in self._items:
                if isinstance(item, dict):
                    path = str(item.get('path', '')).strip()
                    name = str(item.get('name', '')).strip()
                    if path:
                        out.append({'path': path, 'name': name})
        return out
