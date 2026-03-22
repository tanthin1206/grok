from __future__ import annotations

import re
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from settings_manager import DATA_GENERAL_DIR


HELP_GUIDE_FILE = Path(DATA_GENERAL_DIR) / "huong_dan_su_dung_tool.md"


DEFAULT_GUIDE_TEXT = """1) Tạo Video từ Văn bản
- Bước 1: Chọn tab Text to Video.
- Bước 2: Nhập hàng loạt prompt, mỗi dòng một prompt.
- Bước 3: Bấm BẮT ĐẦU TẠO VIDEO TỪ VĂN BẢN để chạy theo danh sách prompt.
- LƯU Ý: Có thể sửa từng prompt trước khi chạy để tránh lỗi nội dung.

2) Tạo Video từ Ảnh
- Bước 1: Chọn tab Image to Video → Tạo Video Từ Ảnh.
- Bước 2: Chọn hàng loạt ảnh đầu vào.
- Bước 3: Nhập prompt tương ứng theo từng dòng.
- Bước 4: Bấm BẮT ĐẦU TẠO VIDEO TỪ ẢNH.
- LƯU Ý: Ảnh và prompt được ghép theo thứ tự từ trên xuống.

3) Tạo Video từ Ảnh Đầu / Ảnh Cuối
- Bước 1: Chọn tab Image to Video → Tạo video từ Ảnh Đầu - Ảnh Cuối.
- Bước 2: Chọn hàng loạt ảnh bắt đầu.
- Bước 3: Chọn hàng loạt ảnh kết thúc.
- Bước 4: Nhập prompt cho từng cặp ảnh.
- Bước 5: Bấm BẮT ĐẦU TẠO VIDEO TỪ ẢNH ĐẦU - ẢNH CUỐI.
- LƯU Ý: Có thể click vào từng ảnh trong bảng để thay thế nhanh.

4) Tạo Video từ Ý Tưởng
- Bước 1: Chọn tab Ý tưởng to Video.
- Bước 2: Chọn số cảnh, phong cách và ngôn ngữ thoại.
- Bước 3: Dán kịch bản/ý tưởng để tool tự dựng prompt chi tiết.
- Bước 4: Bấm TẠO VIDEO TỪ Ý TƯỞNG.
- LƯU Ý: Nên viết rõ bối cảnh và hành động để video ổn định hơn.

5) Đồng bộ Nhân vật
- Bước 1: Chọn tab Đồng bộ nhân vật.
- Bước 2: Tải ảnh các nhân vật muốn đồng bộ và đặt tên cho nhân vật đó.(Tối đa 10 nhân vật)
- Bước 3: Kiểm tra ảnh/prompt theo từng nhân vật trước khi chạy. gọi tên nhân vật trong prompt theo cú pháp {Tên nhân vật} để tool nhận diện.
- Bước 4: Bấm TẠO VIDEO ĐỒNG NHẤT NHÂN VẬT.
- LƯU Ý: Tên nhân vật nên rõ ràng và nhất quán giữa các dòng prompt.
- LƯU Ý: Mỗi Prompt có tối đa 3 nhân vật được đồng bộ cùng lúc để đảm bảo chất lượng video.

6) Tạo Ảnh
- Bước 1: Chọn tab Tạo Ảnh.
- Bước 2: Chọn kiểu tạo ảnh (Từ Prompt hoặc Từ Ảnh Tham Chiếu).
- Bước 3: Nhập prompt hàng loạt theo từng dòng.
- Bước 4: Bấm Bắt đầu Tạo Ảnh.
- LƯU Ý: Với ảnh tham chiếu, prompt sẽ được gán theo thứ tự ảnh đã tải.

7) Cài đặt
- Bước 1: Chọn tab Cài đặt.
- Bước 2: Nhập Tài khoan VEO3 và API Keys của Gemini vào form.
- Bước 3: Bấm Lưu cài đặt để áp dụng.
- Bước 4: Bấm Auto Login TK VEO3 để tool tự động đăng nhập lấy thông tin để tạo video.
- LƯU Ý: Sau khi lưu xong, quay lại tab tạo video/ảnh và bấm nút Bắt đầu tương ứng.
"""


def get_status_help_file_path() -> Path:
    try:
        HELP_GUIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        if not HELP_GUIDE_FILE.exists():
            HELP_GUIDE_FILE.write_text(DEFAULT_GUIDE_TEXT, encoding="utf-8")
    except Exception:
        pass
    return HELP_GUIDE_FILE


def _load_help_groups() -> list[tuple[str, list[str]]]:
    path = get_status_help_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        text = DEFAULT_GUIDE_TEXT

    groups: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []

    header_re = re.compile(r"^\s*(\d+\)\s*.+)$")
    bullet_re = re.compile(r"^\s*[-*•]\s*(.+)$")

    def flush_group() -> None:
        nonlocal current_title, current_lines
        title = str(current_title or "").strip()
        lines = [str(item or "").strip() for item in current_lines if str(item or "").strip()]
        if title and lines:
            groups.append((title, lines))
        current_title = ""
        current_lines = []

    for raw_ln in str(text or "").splitlines():
        ln = str(raw_ln or "").strip()
        if not ln:
            continue

        if header_re.match(ln):
            flush_group()
            current_title = ln
            continue

        bullet_match = bullet_re.match(ln)
        if bullet_match:
            current_lines.append(str(bullet_match.group(1) or "").strip())
            continue

        if current_title:
            current_lines.append(ln)

    flush_group()

    if groups:
        return groups

    # Fallback minimal group if file is malformed
    return [
        (
            "1) Hướng Dẫn Sử Dụng",
            [
                "Nội dung file hướng dẫn chưa đúng định dạng.",
                f"Vui lòng chỉnh lại file: {path}",
            ],
        )
    ]


def _add_box(layout: QVBoxLayout, title: str, lines: list[str]) -> None:
    box = QWidget()
    box.setStyleSheet("border:1px solid #c8d7f2; border-radius:10px; background:#eaf2ff;")
    bl = QVBoxLayout(box)
    bl.setContentsMargins(12, 10, 12, 10)
    bl.setSpacing(6)

    t = QLabel(title)
    t.setStyleSheet("font-weight:800; color:#1f2d48;")
    bl.addWidget(t)

    for ln in lines:
        lb = QLabel("• " + ln)
        lb.setWordWrap(True)
        lb.setStyleSheet(
            "border:1px solid #c8d7f2; border-radius:8px; background:#f2f7ff; padding:6px 8px; color:#1f2d48;"
        )
        bl.addWidget(lb)

    layout.addWidget(box)


def build_status_help_view() -> QWidget:
    wrap = QWidget()
    root = QVBoxLayout(wrap)
    root.setContentsMargins(8, 8, 8, 8)
    root.setSpacing(10)

    title = QLabel("Hướng Dẫn Sử Dụng")
    title.setStyleSheet("font-weight:800; color:#1f2d48; font-size:14px;")
    root.addWidget(title)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    root.addWidget(scroll, 1)

    body = QWidget()
    scroll.setWidget(body)
    v = QVBoxLayout(body)
    v.setContentsMargins(6, 6, 6, 6)
    v.setSpacing(12)

    for title_text, line_items in _load_help_groups():
        _add_box(v, title_text, line_items)

    v.addStretch(1)
    return wrap
