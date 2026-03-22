from __future__ import annotations
from PyQt6.QtCore import QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPlainTextEdit

class _PromptIdArea(QWidget):
    def __init__(self, editor: 'PromptEditor'):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor._id_area_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor._paint_id_area(event)

class PromptEditor(QPlainTextEdit):
    """Prompt editor with an external ID gutter.

    Rules:
    - Each prompt is one line.
    - Blank lines do NOT receive an ID.
    - IDs are sequential (1..N) across non-empty lines.
    - Draw faint dashed separators between lines.
    """
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._id_by_block = {}
        
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._rebuild_id_map)
        
        f = self.font()
        if int(f.pointSize()) > 0:
            f.setPointSize(int(f.pointSize()) + 2)
        self.setFont(f)
        
        self._id_area = _PromptIdArea(self)
        
        self.blockCountChanged.connect(self._update_margins)
        self.updateRequest.connect(self._on_update_request)
        self.textChanged.connect(self._schedule_rebuild_id_map)
        
        self._rebuild_id_map()
        self._update_margins()

    def _schedule_rebuild_id_map(self) -> None:
        self._rebuild_timer.start(80)

    def _rebuild_id_map(self) -> None:
        mapping = {}
        cur_id = 0
        block = self.document().firstBlock()
        while block.isValid():
            txt = str(block.text()) if block.text() else ''
            if txt.strip():
                cur_id += 1
                mapping[int(block.blockNumber())] = cur_id
            block = block.next()
            
        self._id_by_block = mapping
        self._id_area.update()
        self.viewport().update()

    def _id_area_width(self) -> int:
        digits = len(str(max(self._id_by_block.values(), default=0)))
        return 10 + self.fontMetrics().horizontalAdvance('9' * digits)

    def _update_margins(self) -> None:
        w = self._id_area_width()
        self.setViewportMargins(w, 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._id_area.scroll(0, dy)
        else:
            self._id_area.update(0, rect.y(), self._id_area.width(), rect.height())
            
        if rect.contains(self.viewport().rect()):
            self._update_margins()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        w = self._id_area_width()
        self._id_area.setGeometry(QRect(cr.left(), cr.top(), w, cr.height()))

    def _paint_id_area(self, event) -> None:
        painter = QPainter(self._id_area)
        painter.fillRect(event.rect(), QColor('#eef5ff'))
        
        border_pen = QPen(QColor('#d8deee'))
        painter.setPen(border_pen)
        painter.drawLine(self._id_area.width() - 1, 0, self._id_area.width() - 1, self._id_area.height())
        
        dash_pen = QPen(QColor('#d8deee'))
        dash_pen.setStyle(Qt.PenStyle.DashLine)
        dash_pen.setDashPattern([2, 2])
        
        block = self.firstVisibleBlock()
        block_number = int(block.blockNumber())
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        
        fm = self.fontMetrics()
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                pid = self._id_by_block.get(block_number, 0)
                if pid:
                    painter.setPen(QColor('#31456a'))
                    painter.drawText(0, top, self._id_area.width() - 4, fm.height(), 
                                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, 
                                     str(pid))
                
                painter.setPen(dash_pen)
                painter.drawLine(0, bottom, self._id_area.width(), bottom)
                
            block = block.next()
            block_number = int(block.blockNumber())
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        dash_pen = QPen(QColor('#d8deee'))
        dash_pen.setStyle(Qt.PenStyle.DashLine)
        dash_pen.setDashPattern([2, 2])
        painter.setPen(dash_pen)
        
        block = self.firstVisibleBlock()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        vw = int(self.viewport().width())
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if str(block.text()) and str(block.text()).strip():
                    painter.drawLine(0, bottom - 1, vw, bottom - 1)
            
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())

class TextToVideoTab(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        
        title = QLabel('Nhập prompt (mỗi dòng là 1 prompt)')
        title.setStyleSheet('font-weight: 600; color: #1f2d48;')
        layout.addWidget(title)
        
        self.editor = PromptEditor()
        self.editor.setPlaceholderText('Nhập prompt ở đây. Mỗi prompt là 1 dòng.\nVí dụ:\n- Một con mèo đeo kính đang đọc sách trong quán cà phê\n- Cảnh hoàng hôn trên biển, phong cách cinematic')
        layout.addWidget(self.editor, 1)

    def get_prompts(self) -> list[str]:
        if self.editor.toPlainText():
            try:
                raw = ''
                raw = self.editor.toPlainText().replace('\r\n', '\n').replace('\r', '\n')
                lines = [ln.strip() for ln in raw.split('\n')]
                return [ln for ln in lines if ln]
            except Exception:
                return []
        return []
