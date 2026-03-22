from __future__ import annotations
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .tab_character_sync import CharacterSyncTab
from .tab_text_to_video import PromptEditor


class CreateImageFromPromptTab(QWidget):
    """Simple prompt-only UI, reused from Text-to-Video prompt editor style."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        title = QLabel("Nhập prompt (mỗi dòng là 1 prompt)")
        title.setStyleSheet("font-weight: 600; color: #1f2d48;")
        layout.addWidget(title)

        self.editor = PromptEditor()
        self.editor.setPlaceholderText(
            "Nhập prompt ở đây. Mỗi prompt là 1 dòng.\n"
            "Ví dụ:\n"
            "- Một con mèo đeo kính đang đọc sách trong quán cà phê\n"
            "- Cảnh hoàng hôn trên biển, phong cách cinematic"
        )
        layout.addWidget(self.editor, 1)

    def get_prompts(self) -> list[str]:
        raw = self.editor.toPlainText() or ""
        lines = [ln.strip() for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        return [ln for ln in lines if ln]


class CreateImageFromReferenceTab(CharacterSyncTab):
    """Reuses CharacterSyncTab UI exactly for reference-image creation tab."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)


class CreateImageTab(QWidget):
    MODEL_OPTIONS: list[tuple[str, str]] = [
        ("🍌 Nano Banana pro", "Nano Banana pro"),
        ("🍌 Nano Banana 2", "Nano Banana 2"),
        ("🍌 Nano Banana", "Nano Banana"),
        ("📷 Imagen 4", "Imagen 4"),
    ]

    def __init__(self, parent: QWidget | None = None, config=None, on_model_changed=None):
        super().__init__(parent)
        self._cfg = config
        self._on_model_changed_cb = on_model_changed
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.setContentsMargins(8, 6, 8, 0)
        top.setSpacing(8)
        lbl = QLabel("Model Tạo ảnh")
        lbl.setStyleSheet("font-weight: 600; color: #1f2d48;")
        top.addWidget(lbl)

        self.model_combo = QComboBox()
        self.model_combo.setObjectName("BottomCfgCombo")
        for label, value in self.MODEL_OPTIONS:
            self.model_combo.addItem(label, value)

        current_model = str(getattr(self._cfg, "create_image_model", "Imagen 4") or "Imagen 4")
        idx = self.model_combo.findData(current_model)
        self.model_combo.setCurrentIndex(idx if idx >= 0 else len(self.MODEL_OPTIONS) - 1)
        self.model_combo.setFixedHeight(32)
        self.model_combo.setMinimumWidth(210)
        self.model_combo.currentIndexChanged.connect(self._on_model_combo_changed)
        top.addWidget(self.model_combo)
        top.addStretch(1)
        root.addLayout(top)

        self.tabs = QTabWidget()
        self.tab_prompt = CreateImageFromPromptTab()
        self.tab_reference = CreateImageFromReferenceTab()
        self.tabs.addTab(self.tab_prompt, "Tạo Ảnh Từ Prompt")
        self.tabs.addTab(self.tab_reference, "Tạo Ảnh Từ Ảnh Tham Chiếu")
        root.addWidget(self.tabs, 1)

    def _on_model_combo_changed(self) -> None:
        model_value = str(self.model_combo.currentData() or "Imagen 4")
        if self._cfg is not None:
            try:
                setattr(self._cfg, "create_image_model", model_value)
            except Exception:
                pass
        if callable(self._on_model_changed_cb):
            try:
                self._on_model_changed_cb(model_value)
            except Exception:
                pass

    def current_mode(self) -> str:
        return "reference" if self.tabs.currentIndex() == 1 else "prompt"

    def get_prompt_items(self) -> list[dict]:
        prompts = self.tab_prompt.get_prompts()
        items: list[dict] = []
        for idx, prompt in enumerate(prompts, start=1):
            txt = str(prompt or "").strip()
            if not txt:
                continue
            items.append({"id": str(idx), "description": txt})
        return items

    def get_reference_data(self) -> tuple[list[str], list[dict]]:
        prompts = self.tab_reference.get_prompts() if hasattr(self.tab_reference, "get_prompts") else []
        characters = self.tab_reference.get_character_items() if hasattr(self.tab_reference, "get_character_items") else []
        clean_prompts = [str(p or "").strip() for p in prompts if str(p or "").strip()]
        clean_characters: list[dict] = []
        for ch in characters or []:
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            path = str(ch.get("path") or "").strip()
            if not (name and path):
                continue
            clean_characters.append({"name": name, "path": path})
        return clean_prompts, clean_characters
