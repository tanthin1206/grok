from __future__ import annotations

from typing import Callable

from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox


_INSTALLED = False
_ORIG_EXEC: Callable | None = None
_ORIG_INFO: Callable | None = None
_ORIG_WARN: Callable | None = None
_ORIG_CRIT: Callable | None = None
_ORIG_QUESTION: Callable | None = None


_POPUP_QSS = """
QMessageBox {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f8fbff, stop:1 #eef6ff);
}
QMessageBox QLabel {
    color: #0f1f3a;
    font-size: 13px;
}
QMessageBox QLabel#qt_msgbox_label {
    font-size: 14px;
    font-weight: 700;
    padding: 4px 2px;
}
QMessageBox QLabel#qt_msgbox_informativelabel {
    color: #334155;
    font-size: 12px;
}
QMessageBox QPushButton {
    min-width: 88px;
    min-height: 30px;
    padding: 4px 10px;
    border: 1px solid #9ec5ff;
    border-radius: 8px;
    background: #dbeafe;
    color: #12306b;
    font-weight: 700;
}
QMessageBox QPushButton:hover {
    background: #bfdbfe;
}
QMessageBox QPushButton:pressed {
    background: #93c5fd;
}
"""


def _style_box(box: QMessageBox) -> None:
    try:
        box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    except Exception:
        pass

    try:
        box.setStyleSheet(_POPUP_QSS)
    except Exception:
        pass

    try:
        btn_box = box.findChild(QDialogButtonBox)
        if btn_box is not None:
            btn_box.setCenterButtons(True)
    except Exception:
        pass


def _build_and_exec(
    *,
    parent,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton,
    default: QMessageBox.StandardButton,
) -> QMessageBox.StandardButton:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title or "Thông báo"))
    box.setText(str(text or ""))
    box.setStandardButtons(buttons)
    if default != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default)
    _style_box(box)
    return QMessageBox.StandardButton(box.exec())


def install_messagebox_theme() -> None:
    global _INSTALLED, _ORIG_EXEC, _ORIG_INFO, _ORIG_WARN, _ORIG_CRIT, _ORIG_QUESTION
    if _INSTALLED:
        return

    _INSTALLED = True
    _ORIG_EXEC = QMessageBox.exec
    _ORIG_INFO = QMessageBox.information
    _ORIG_WARN = QMessageBox.warning
    _ORIG_CRIT = QMessageBox.critical
    _ORIG_QUESTION = QMessageBox.question

    def _patched_exec(self: QMessageBox) -> int:
        _style_box(self)
        return int(_ORIG_EXEC(self))

    def _patched_information(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Information,
            title=str(title or "Thông báo"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_warning(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Warning,
            title=str(title or "Cảnh báo"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_critical(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Ok,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Critical,
            title=str(title or "Lỗi"),
            text=str(text or ""),
            buttons=buttons,
            default=defaultButton,
        )

    def _patched_question(
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        resolved_default = defaultButton
        if resolved_default == QMessageBox.StandardButton.NoButton:
            if buttons & QMessageBox.StandardButton.No:
                resolved_default = QMessageBox.StandardButton.No
            elif buttons & QMessageBox.StandardButton.Yes:
                resolved_default = QMessageBox.StandardButton.Yes
        return _build_and_exec(
            parent=parent,
            icon=QMessageBox.Icon.Question,
            title=str(title or "Xác nhận"),
            text=str(text or ""),
            buttons=buttons,
            default=resolved_default,
        )

    QMessageBox.exec = _patched_exec
    QMessageBox.information = _patched_information
    QMessageBox.warning = _patched_warning
    QMessageBox.critical = _patched_critical
    QMessageBox.question = _patched_question
