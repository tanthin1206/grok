from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import tab_text_to_video
from tab_text_to_video import PromptEditor
from settings_manager import get_icon_path


def _icon(name: str) -> QIcon:
    if not name:
        return QIcon()
    path = get_icon_path(name)
    if os.path.isfile(path):
        return QIcon(path)
    return QIcon()


def _is_image_file(path: str) -> bool:
    p = str(path or "").lower().strip()
    return p.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))


class _DropArea(QFrame):
    def __init__(self, tab: "CharacterSyncTab"):
        super().__init__(tab)
        self._tab = tab
        self.setAcceptDrops(True)
        self.setObjectName("DropArea")

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

        self._empty = QLabel(
            "Click để chọn ảnh\n"
            "hoặc kéo ảnh vào khu vực này,\n"
            "sau đó đặt tên nhân vật\n"
            "(tối đa 10 ảnh)"
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color:#64748b; font-weight:600;")
        self._body_lay.addWidget(self._empty, 1)

    def mousePressEvent(self, event) -> None:
        # Allow clicking the area to pick images (requested).
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                self._tab.pick_images()
            except Exception:
                pass
        return super().mousePressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
                return
        except Exception:
            pass
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        paths: list[str] = []
        try:
            for u in event.mimeData().urls():
                p = u.toLocalFile()
                if p and os.path.isfile(p) and _is_image_file(p):
                    paths.append(str(p))
        except Exception:
            pass

        if paths:
            self._tab.add_images(paths)
            try:
                event.acceptProposedAction()
            except Exception:
                pass
        else:
            event.ignore()

    def set_cards(self, cards: list[QWidget]) -> None:
        # Clear layout
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
    def __init__(self, tab: "CharacterSyncTab", idx: int, path: str, name: str):
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
        self._img.setStyleSheet("background:#f1f7ff; border:1px solid #c8d7f2; border-radius:8px;")
        g.addWidget(self._img, 0, 0)

        btn = QToolButton()
        btn.setText("×")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(22, 22)
        btn.setStyleSheet(
            "background:#64748b; color:white; border:none; border-radius:11px;"
            "font-weight:900; font-size:16px;"
        )
        btn.clicked.connect(lambda _=False, i=self._idx: self._tab.remove_image(i))
        g.addWidget(btn, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

        root.addWidget(img_wrap)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Đặt tên nhân vật cho ảnh này...")
        self._name.setText(str(name or ""))
        self._name.textChanged.connect(lambda t, i=self._idx: self._tab.set_name(i, str(t)))
        root.addWidget(self._name)

        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        try:
            pix = QPixmap(self._path)
        except Exception:
            pix = QPixmap()

        if pix.isNull():
            self._img.setText("(không đọc được ảnh)")
            self._img.setMinimumHeight(120)
            return

        # Slightly smaller max width for displayed images.
        w = max(1, int(self.width() or 1))
        target_w = max(200, w)
        target_h = 135
        scaled = pix.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img.setPixmap(scaled)
        self._img.setFixedHeight(max(120, int(scaled.height())))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_pixmap()


class CharacterSyncTab(QWidget):
    """UI-only tab matching the provided mock for 'Đồng bộ nhân vật'."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._items: list[dict[str, str]] = []  # {path, name}

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(10)

        # Left: prompt editor
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)

        title = QLabel("Prompt hàng loạt")
        title.setStyleSheet("font-weight:700; color:#1f2d48;")
        ll.addWidget(title)

        self.prompts = PromptEditor()
        self.prompts.setPlaceholderText(
            "- Dán hàng loạt prompt, mỗi dòng 1 prompt\n\n"
            "- Chọn 10 ảnh nhân vật và đặt tên riêng cho nhân vật\n\n"
            "- Gọi tên nhân vật (chỉ tên) và mô tả hành động của nhân vật, bối cảnh...\n\n"
            "*LƯU Ý :\n"
            "- Ảnh nhân vật nên up ảnh nền trắng hoặc png ko nền\n"
            "- Tên nhân vật nên đặt từ 4 ký tự trở lên\n"
            "- Prompt có tối đa 3 nhân vật, nếu nhiều hơn, hãy ghép 2 nhân vật trong 1 ảnh"
        )
        ll.addWidget(self.prompts, 1)

        # Right: image selector
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        hdr = QHBoxLayout()
        # Align this header row with the inner content padding of the drop area.
        try:
            hdr.setContentsMargins(14, 0, 14, 0)
        except Exception:
            pass
        self.btn_pick = QPushButton("Chọn ảnh nhân vật (tối đa 10)")
        try:
            ic = _icon("folder_icon.png")
            if not ic.isNull():
                self.btn_pick.setIcon(ic)
        except Exception:
            pass
        self.btn_pick.clicked.connect(self.pick_images)
        hdr.addWidget(self.btn_pick)
        hdr.addStretch(1)
        rl.addLayout(hdr)

        self.drop = _DropArea(self)
        self.drop.setStyleSheet(
            "QFrame#DropArea{border:1px dashed #c8d7f2; border-radius:10px; background:#eef5ff;}"
        )
        rl.addWidget(self.drop, 1)

        root.addWidget(left, 3)
        root.addWidget(right, 2)

        self._refresh_cards()

    def pick_images(self) -> None:
        if len(self._items) >= 10:
            self._show_max_message()
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Chọn ảnh nhân vật",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp);;All Files (*.*)",
        )
        if not files:
            return
        self.add_images([str(p) for p in files])

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
            if not p or not os.path.isfile(p) or not _is_image_file(p):
                continue
            if len(self._items) >= 10:
                break
            self._items.append({"path": str(p), "name": ""})

        # If user selected more than remaining, tell them we've hit the cap.
        if len(paths) > len(truncated) and len(self._items) >= 10:
            self._show_max_message()
        self._refresh_cards()

    def _show_max_message(self) -> None:
        QMessageBox.information(self, "Đủ số lượng", "Đã đủ số lượng nhân vật tối đa (10 ảnh).")

    def remove_image(self, idx: int) -> None:
        i = int(idx)
        if i < 0 or i >= len(self._items):
            return
        try:
            del self._items[i]
        except Exception:
            return
        self._refresh_cards()

    def set_name(self, idx: int, name: str) -> None:
        i = int(idx)
        if i < 0 or i >= len(self._items):
            return
        self._items[i]["name"] = str(name or "")

    def _refresh_cards(self) -> None:
        cards: list[QWidget] = []
        for i, it in enumerate(list(self._items)):
            cards.append(_CharacterCard(self, i, it.get("path", ""), it.get("name", "")))
        self.drop.set_cards(cards)

    def get_prompts(self) -> list[str]:
        raw = self.prompts.toPlainText() or ""
        lines = [ln.strip() for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        return [ln for ln in lines if ln]

    def get_character_items(self) -> list[dict]:
        out: list[dict] = []
        for item in list(self._items or []):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            name = str(item.get("name") or "").strip()
            if not path:
                continue
            out.append({"path": path, "name": name})
        return out
