"""
Test Gemini API - Generate prompts từ kịch bản theo 3 bước
Bước 1: Tạo nhân vật & bối cảnh (step1_char_file.json)
Bước 2: Tạo kịch bản & lời thoại (step2_shot_file.json)
Bước 3: Tạo prompt chi tiết (step3_prompt_file.json)
"""
import json
import os
import re
import datetime as datetime_module
from pathlib import Path
from settings_manager import DATA_GENERAL_DIR, WORKFLOWS_DIR
from style import STYLE_JSON

try:
    from google import genai
except Exception:
    genai = None


def load_api_keys():
    """Load tất cả API keys từ data_general/gemini_api_key.txt (mỗi dòng 1 key)."""
    primary_file = DATA_GENERAL_DIR / "gemini_api_key.txt"
    legacy_file = DATA_GENERAL_DIR / "gemini_API" / "gemini_api.txt"

    for api_key_file in (primary_file, legacy_file):
        if api_key_file.exists():
            with open(api_key_file, 'r', encoding='utf-8') as f:
                keys = [line.strip() for line in f.readlines() if line.strip()]
                if keys:
                    return keys
    return []


def _show_api_key_error(message):
    try:
        from PyQt6.QtCore import QCoreApplication, QTimer
        from PyQt6.QtWidgets import QMessageBox

        app = QCoreApplication.instance()
        if app is None:
            return

        def _show():
            QMessageBox.critical(None, "Lỗi API", message)

        QTimer.singleShot(0, app, _show)
    except Exception:
        pass


def load_visual_style(style="3d_Pixar"):
    """Load visual_style từ style.py (embedded JSON map)."""
    style_name = str(style or "").strip()
    style_data = STYLE_JSON.get(style_name)
    if isinstance(style_data, dict):
        value = style_data.get("visual_style")
        if value is not None:
            return str(value).strip()

    fallback = STYLE_JSON.get("3d_Pixar")
    if isinstance(fallback, dict):
        return str(fallback.get("visual_style") or "").strip()
    return ""


BODY_PROPORTION_RULE = (
    "maintain consistent body thickness across all scenes, no bulk exaggeration, "
    "no chest enlargement, no perspective distortion"
)

BODY_CONSTRAINT_RULE = (
    "no fat, no chubby look, no exaggerated torso width, maintain athletic slim build"
)

CAMERA_LENS_RULE = "50mm natural perspective, no wide-angle distortion"


def _sanitize_pose_text(raw_pose: str) -> str:
    pose = str(raw_pose or "").strip()
    if not pose:
        return pose
    replacements = [
        (r"\bchest\s+puffed\s+out\b", "neutral chest posture"),
        (r"\bheroic\b", "confident"),
        (r"\bbroad\s+chest\b", "balanced torso"),
        (r"\bbarrel\s+chest\b", "balanced torso"),
    ]
    for pattern, replacement in replacements:
        pose = re.sub(pattern, replacement, pose, flags=re.IGNORECASE)
    return pose


def _enforce_scene_body_consistency(scene_obj: dict) -> dict:
    if not isinstance(scene_obj, dict):
        return scene_obj

    camera = scene_obj.get("camera")
    if not isinstance(camera, dict):
        camera = {}
    angle = str(camera.get("angle") or "").strip().lower()
    if "low" in angle:
        camera["angle"] = "eye level"
    camera["lens"] = CAMERA_LENS_RULE
    scene_obj["camera"] = camera

    character_lock = scene_obj.get("character_lock")
    if isinstance(character_lock, dict):
        for key, char_data in character_lock.items():
            if not isinstance(char_data, dict):
                continue
            char_data["proportion_rule"] = BODY_PROPORTION_RULE
            char_data["body_constraint"] = BODY_CONSTRAINT_RULE
            char_data["pose"] = _sanitize_pose_text(char_data.get("pose", ""))
            metrics = str(char_data.get("body_metrics") or "")
            if metrics:
                if "no-auto-rescale" not in metrics:
                    metrics = f"{metrics},no-auto-rescale"
                if "lock-proportions" not in metrics:
                    metrics = f"{metrics},lock-proportions"
                char_data["body_metrics"] = metrics
            character_lock[key] = char_data
        scene_obj["character_lock"] = character_lock

    note = str(scene_obj.get("lip_sync_director_note") or "").strip()
    body_note = "Keep body proportions consistent across scenes; avoid torso/chest enlargement."
    if body_note not in note:
        scene_obj["lip_sync_director_note"] = f"{note} {body_note}".strip()

    return scene_obj


def get_current_api_key_index(project_dir=None):
    """Lấy index API key hiện tại từ state file
    Nếu file tồn tại, lấy index đã lưu (tiếp tục từ key đó)
    Nếu không, bắt đầu từ key #0
    """
    if project_dir is None:
        return 0

    state_file = Path(project_dir) / ".api_key_index"
    if state_file.exists():
        try:
            with open(state_file, 'r') as f:
                index = int(f.read().strip())
                return index
        except Exception:
            return 0
    return 0


def save_current_api_key_index(index, project_dir=None):
    """Lưu index API key hiện tại"""
    if project_dir is None:
        return  # ❌ KHÔNG LƯU NẾU KHÔNG CÓ PROJECT_DIR
    state_file = Path(project_dir) / ".api_key_index"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, 'w') as f:
        f.write(str(index))


def call_gemini_with_retry(prompt_text, api_keys, current_key_index=0, log_callback=None, project_dir=None, stop_check=None, request_timeout_sec=120):
    """Helper function - gọi Gemini với retry logic + log

    Args:
        prompt_text: Prompt để gửi
        api_keys: List các API keys
        current_key_index: Index của API key hiện tại
        log_callback: Function để log lên UI (nếu None thì không log)
        project_dir: Thư mục dự án để lưu state
    """
    def log(msg):
        if log_callback:
            log_callback(msg)

    def should_stop():
        return bool(stop_check and stop_check())

    if genai is None:
        log("❌ Thiếu thư viện Gemini. Cài đặt: pip install google-genai")
        return None, current_key_index

    # ✅ RESET INDEX NẾU VẬT QUÁ SỐ API KEY CÓ SẢN
    if current_key_index >= len(api_keys):
        log(f"⚠️ API key index vượt quá ({current_key_index} >= {len(api_keys)}) - reset về key #0")
        current_key_index = 0
        save_current_api_key_index(current_key_index, project_dir)

    max_retries = len(api_keys)

    for attempt in range(max_retries):
        if should_stop():
            log("⏹️ Đã dừng trong lúc gọi Gemini")
            return None, current_key_index
        if current_key_index >= len(api_keys):
            log(f"❌ Hết lượt API - không còn API key nào cả!")
            return None, current_key_index

        api_key = api_keys[current_key_index]
        api_display = f"API #{current_key_index + 1}/{len(api_keys)}"
        log(f"🔑 Đang dùng {api_display}...")

        if should_stop():
            log("⏹️ Đã dừng trước khi gửi request Gemini")
            return None, current_key_index

        use_client_api = hasattr(genai, "Client")
        if use_client_api:
            client = genai.Client(api_key=api_key)

        try:
            if use_client_api:
                try:
                    response = client.models.generate_content(
                        model="gemini-2.5-flash-lite",
                        contents=prompt_text,
                        request_options={"timeout": request_timeout_sec}
                    )
                except TypeError:
                    response = client.models.generate_content(
                        model="gemini-2.5-flash-lite",
                        contents=prompt_text
                    )
            else:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel("gemini-2.5-flash-lite")
                try:
                    response = model.generate_content(
                        prompt_text,
                        request_options={"timeout": request_timeout_sec}
                    )
                except TypeError:
                    response = model.generate_content(prompt_text)

            if should_stop():
                log("⏹️ Đã dừng sau khi nhận response Gemini")
                return None, current_key_index

            if not response or not response.text:
                log(f"⚠️ Response rỗng từ {api_display} - thử API tiếp theo...")
                current_key_index += 1
                save_current_api_key_index(current_key_index, project_dir)
                continue

            log(f"✅ {api_display} thành công!")
            save_current_api_key_index(current_key_index, project_dir)
            return response.text, current_key_index

        except Exception as e:
            error_str = str(e)
            api_display = f"API #{current_key_index + 1}/{len(api_keys)}"

            # ✅ TRY ALL ERRORS - CHUYỂN KEY TIẾP THEO, KHÔNG DỪNG NGAY
            if "429" in error_str or "quota" in error_str.lower() or "RESOURCE_EXHAUSTED" in error_str:
                log(f"⚠️ {api_display} hết lượt (429 Quota Exceeded)")
            elif "API_KEY_INVALID" in error_str or "invalid api key" in error_str.lower():
                log(f"⚠️ {api_display} lỗi API key (Invalid Key)")
            elif "401" in error_str or "unauthenticated" in error_str.lower():
                log(f"⚠️ {api_display} lỗi authentication (401)")
            elif "400" in error_str or "INVALID_ARGUMENT" in error_str:
                log(f"⚠️ {api_display} lỗi yêu cầu (400 Invalid Argument) - {error_str[:120]}")
            else:
                log(f"⚠️ {api_display} gặp lỗi: {error_str[:120]}")

            log(f"   Chuyển sang API key tiếp theo...")
            current_key_index += 1
            save_current_api_key_index(current_key_index, project_dir)

            if current_key_index < len(api_keys):
                continue
            else:
                log(f"❌ Hết lượt tất cả API keys - không còn API nào khả dụng!")
                break

    log(f"❌ Không thể hoàn thành request - tất cả API keys đều hết lượt hoặc lỗi")
    return None, current_key_index


def parse_json_response(response_text):
    """Parse JSON từ response Gemini (xử lý markdown code blocks)"""
    try:
        response_text = response_text.strip()

        # Loại bỏ markdown code blocks nếu có
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        parsed_json = json.loads(response_text)
        return parsed_json
    except json.JSONDecodeError:
        return None


def gemini_step_1(script, project_dir, scene_count, style, log_callback=None, stop_check=None):
    """
    BƯỚC 1: Tạo nhân vật & bối cảnh
    Input: Kịch bản thô + config từ UI
    Output: step1_char_file.json (character_lock + background_lock + visual_style)

    📖 Dùng config từ UI: style, scene
    """
    project_dir = str(project_dir or "")
    num_scenes = int(max(1, min(100, int(scene_count or 5))))
    style = str(style or "3d_Pixar")

    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if stop_check and stop_check():
        log("⏹️ Đã dừng trước khi bắt đầu Step 1")
        return None

    log("\n" + "="*70)
    log("🎬 BƯỚC 1: TẠO NHÂN VẬT & BỐI CẢNH")
    log("="*70)


    api_keys = load_api_keys()
    if not api_keys:
        log("❌ Không tìm thấy API keys")
        return None

    # ✅ LẤY API KEY INDEX TỪ LẦN CHẠY TRƯỚC (NẾU CÓ)
    current_key_index = get_current_api_key_index(project_dir)

    prompt = f"""You are an expert character and environment designer for animated films. Your task is to create detailed character and environment specifications to ensure consistency across all scenes.

STORY/IDEA:
{script}

========== BUILD SHARED COMPONENTS (DETAILED STRUCTURE) ==========

SAFETY & COMPLIANCE RULES:
- All characters must be fictional. Do NOT reference real people, celebrities, or public figures.
- Do NOT use labels that imply a real person (avoid terms like "human-girl"; use "fictional girl/child" or "young fictional character").
- For child characters: avoid exact body measurements or sexualized details; keep descriptions age-appropriate and general.
- For child characters: body_metrics MUST use non-numeric placeholders (e.g., abs.height=unspecified) and no precise measurements.
- Do NOT include logos, brand names, mascots, or "official/advertisement" language.
- Do NOT reference voice cloning or real voice likeness.

📌 A. CHARACTERS - REQUIRED FIELDS:
Each character MUST have:
1. id: Character code (CHAR_1, CHAR_2, ...)
2. name: Character name
3. species: Species (e.g., "Monkey – Chimpanzee")
4. gender: "Male" / "Female" / "Unknown"
5. age: Age description with number (e.g., "Young Adult")
6. voice_personality: Voice type; gender=Male/Female/Unknown; locale=vi-VN; accent=Regional accent, sound type
7. body_build: Body type (e.g., "Agile, muscular")
8. face_shape: Face shape description
9. hair: Hair/fur description
10. skin_or_fur_color: Color description
11. signature_feature: Distinctive feature
12. outfit_top: Top clothing
13. outfit_bottom: Bottom clothing
14. helmet_or_hat: Head wear
15. shoes_or_footwear: Footwear
16. props: Carried items
17. body_metrics: u=cm; abs.height=X; abs.head=X; abs.shoulder=X; abs.torso=X; abs.tail=X; abs.paw=X; anch.bottle500=20; cons=no-auto-rescale,lock-proportions
    - If character is a child, replace numeric values with "unspecified" (e.g., abs.height=unspecified) and keep cons=child-safe-no-metrics,lock-proportions

📌 B. BACKGROUNDS (Environments) - REQUIRED FIELDS:
Each background MUST have:
1. id: Background code (BACKGROUND_1, BACKGROUND_2, ...)
2. name: Location name
3. setting: Environment type description
4. scenery: Scene description (trees, buildings, etc.)
5. props: Fixed objects in scene
6. lighting: Lighting description (time of day, light quality)

📌 C. SCENE OUTLINE (HIGH-LEVEL) - REQUIRED FIELDS:
Create exactly {num_scenes} scene outlines. Each scene MUST have ONLY:
1. scene_id: Scene number (1..{num_scenes})
2. summary: 1–2 sentence high-level description of what happens in this scene

IMPORTANT:
- If the input story already contains clear per-scene ideas, keep those ideas and do NOT invent extra details.
- If the input story is general, create simple per-scene summaries (not too detailed).

========== OUTPUT FORMAT ==========

{{
    "story_summary": "[1–3 sentence high-level summary of the story/idea, in English]",
    "character_lock": {{
    "CHAR_1": {{
      "id": "CHAR_1",
      "name": "...",
      "species": "...",
      "gender": "...",
      "age": "...",
      "voice_personality": "...",
      "body_build": "...",
      "face_shape": "...",
      "hair": "...",
      "skin_or_fur_color": "...",
      "signature_feature": "...",
      "outfit_top": "...",
      "outfit_bottom": "...",
      "helmet_or_hat": "...",
      "shoes_or_footwear": "...",
      "props": "...",
      "body_metrics": "..."
    }}
  }},
    "background_lock": {{
    "BACKGROUND_1": {{
      "id": "BACKGROUND_1",
      "name": "...",
      "setting": "...",
      "scenery": "...",
      "props": "...",
      "lighting": "..."
    }}
    }},
    "scene_outline": [
        {{
            "scene_id": "1",
            "summary": "[High-level scene summary]"
        }}
    ]
}}

STYLE: {style}
LANGUAGE: English
NUM_SCENES: {num_scenes}

Return ONLY valid JSON, no markdown, no comments."""

    log(f"\n📤 Gửi request tới Gemini...")
    log(f"   Số cảnh: {num_scenes}")
    log(f"   Phong cách: {style}")

    response_text, current_key_index = call_gemini_with_retry(
        prompt, api_keys, current_key_index, log, project_dir, stop_check=stop_check
    )
    
    # ✅ LƯU API KEY INDEX CHO LẦN CHẠY TIẾP THEO (DỨ THÀNH CÔNG HAY THẤT BẠI)
    save_current_api_key_index(current_key_index, project_dir)

    if not response_text:
        log("❌ Bước 1 thất bại")
        return None

    # Parse JSON
    parsed_json = parse_json_response(response_text)
    if not parsed_json:
        log("❌ Không parse được JSON từ response")
        return None

    # Thêm story_summary fallback nếu thiếu
    if not parsed_json.get("story_summary"):
        raw = (script or "").strip()
        parts = re.split(r"(?<=[.!?。])\s+", raw)
        fallback_summary = " ".join([p.strip() for p in parts if p.strip()][:2]).strip()
        parsed_json["story_summary"] = fallback_summary or raw[:200]

    # Thêm scene_outline fallback nếu thiếu
    scene_outline = parsed_json.get("scene_outline") if isinstance(parsed_json, dict) else None
    if not isinstance(scene_outline, list) or not scene_outline:
        fallback_outline = []
        for i in range(1, num_scenes + 1):
            fallback_outline.append({
                "scene_id": str(i),
                "summary": parsed_json.get("story_summary", "") or f"Scene {i} continues the story."
            })
        parsed_json["scene_outline"] = fallback_outline

    def _is_child_age(age_value):
        age_str = str(age_value or "").strip().lower()
        if not age_str:
            return False
        if "young adult" in age_str:
            return False
        keywords = ["child", "kid", "toddler", "teen", "minor", "preteen", "infant", "baby", "school-age", "school age"]
        if any(k in age_str for k in keywords):
            return True
        numbers = re.findall(r"\d+", age_str)
        for num in numbers:
            try:
                if int(num) < 18:
                    return True
            except Exception:
                continue
        return False

    # Sanitize child body_metrics to avoid precise measurements
    char_lock = parsed_json.get("character_lock", {}) if isinstance(parsed_json, dict) else {}
    if isinstance(char_lock, dict):
        for _, char_data in char_lock.items():
            if not isinstance(char_data, dict):
                continue
            if _is_child_age(char_data.get("age")):
                char_data["body_metrics"] = (
                    "u=cm; abs.height=unspecified; abs.head=unspecified; abs.shoulder=unspecified; "
                    "abs.torso=unspecified; abs.tail=unspecified; abs.paw=unspecified; "
                    "anch.bottle500=unspecified; cons=child-safe-no-metrics,lock-proportions"
                )

    # Thêm visual_style
    visual_style = load_visual_style(style)
    parsed_json["visual_style"] = visual_style

    # Lưu file
    output_file = Path(project_dir) / "step1_char_file.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(parsed_json, f, ensure_ascii=False, indent=2)

    return parsed_json


def gemini_step_2(step1_data, script, project_dir, scene_count, style, language, log_callback=None, stop_check=None):
    """
    BƯỚC 2: Tạo kịch bản & lời thoại
    Input: step1_data + config từ UI
    Output: step2_shot_file.json (JSON array - mỗi phần tử 1 scene)
    
    📖 Dùng config từ UI: scene, style, language
    """
    project_dir = str(project_dir or "")
    num_scenes = int(max(1, min(100, int(scene_count or 5))))
    style = str(style or "3d_Pixar")
    language_str = str(language or "Tiếng Việt (vi-VN)")
    # Extract language code from "Tiếng Việt (vi-VN)" → "vi-VN"
    lang_code = language_str.split('(')[-1].rstrip(')') if '(' in language_str else "vi-VN"
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if stop_check and stop_check():
        log("⏹️ Đã dừng trước khi bắt đầu Step 2")
        return None
    
    log("\n" + "="*70)
    log("🎬 BƯỚC 2: TẠO KỊCH BẢN")
    log("="*70)

    
    if not step1_data:
        log("❌ Cần dữ liệu từ bước 1")
        return None
    
    api_keys = load_api_keys()
    if not api_keys:
        log("❌ Không tìm thấy API keys")
        return None
    
    # ✅ LẤY API KEY INDEX TỪ LẦN CHẠY TRƯỚC (NẾU CÓ)
    current_key_index = get_current_api_key_index(project_dir)
    
    # Format character & background data (KHÔNG gửi visual_style)
    char_data = json.dumps(step1_data.get("character_lock", {}), ensure_ascii=False, indent=2)
    bg_data = json.dumps(step1_data.get("background_lock", {}), ensure_ascii=False, indent=2)
    story_summary = step1_data.get("story_summary", "") if isinstance(step1_data, dict) else ""
    scene_outline_list = step1_data.get("scene_outline", []) if isinstance(step1_data, dict) else []
    scene_outline_map = {}
    if isinstance(scene_outline_list, list):
        for item in scene_outline_list:
            if isinstance(item, dict):
                sid = str(item.get("scene_id", "")).strip()
                if sid:
                    scene_outline_map[sid] = item

    base_character_lock = step1_data.get("character_lock", {}) if isinstance(step1_data, dict) else {}
    base_background_lock = step1_data.get("background_lock", {}) if isinstance(step1_data, dict) else {}
    step3_required_fields = [
        "scene_id", "duration_sec", "character_lock", "background_lock",
        "camera", "foley_and_ambience", "dialogue", "lip_sync_director_note", "summary"
    ]
    legacy_scene_fields = ["scene_id", "content", "chars", "background", "char_items", "bg_landmarks", "dialogue"]

    def normalize_scene_for_step3(scene_obj):
        normalized = dict(scene_obj) if isinstance(scene_obj, dict) else {}

        scene_id = str(normalized.get("scene_id", "")).strip()
        normalized["scene_id"] = scene_id

        chars = normalized.get("chars", [])
        if not isinstance(chars, list):
            chars = []
        chars = [str(char_id).strip() for char_id in chars if str(char_id).strip()]
        if not chars and isinstance(base_character_lock, dict):
            chars = [str(key).strip() for key in base_character_lock.keys() if str(key).strip()]
        normalized["chars"] = chars

        character_lock = normalized.get("character_lock")
        if not isinstance(character_lock, dict) or not character_lock:
            character_lock = {}
            if isinstance(base_character_lock, dict):
                for char_id in chars:
                    lock_item = base_character_lock.get(char_id)
                    if isinstance(lock_item, dict):
                        character_lock[char_id] = dict(lock_item)
        normalized["character_lock"] = character_lock

        background_id = str(normalized.get("background", "")).strip()
        background_lock = normalized.get("background_lock")
        if isinstance(background_lock, dict) and background_lock:
            if "id" in background_lock:
                bg_obj = dict(background_lock)
                bg_key = str(bg_obj.get("id") or background_id or "BACKGROUND_1").strip()
                background_lock = {bg_key: bg_obj}
        else:
            background_lock = {}
            if isinstance(base_background_lock, dict) and base_background_lock:
                if background_id and isinstance(base_background_lock.get(background_id), dict):
                    background_lock[background_id] = dict(base_background_lock.get(background_id))
                else:
                    first_bg_key = next(iter(base_background_lock.keys()), "")
                    if first_bg_key and isinstance(base_background_lock.get(first_bg_key), dict):
                        background_lock[str(first_bg_key)] = dict(base_background_lock.get(first_bg_key))
                        if not background_id:
                            background_id = str(first_bg_key)

        if not background_id and isinstance(background_lock, dict) and background_lock:
            background_id = str(next(iter(background_lock.keys())))

        normalized["background"] = background_id
        normalized["background_lock"] = background_lock

        if "duration_sec" not in normalized:
            normalized["duration_sec"] = 8

        camera = normalized.get("camera")
        if not isinstance(camera, dict):
            camera = {
                "framing": "medium shot",
                "angle": "eye level",
                "movement": "static",
                "focus": "main characters",
            }
        normalized["camera"] = camera

        foley = normalized.get("foley_and_ambience")
        if not isinstance(foley, dict):
            foley = {
                "ambience": [],
                "fx": [],
                "music": "",
            }
        normalized["foley_and_ambience"] = foley

        dialogue = normalized.get("dialogue")
        if not isinstance(dialogue, list):
            dialogue = []
        cleaned_dialogue = []
        for line in dialogue:
            if not isinstance(line, dict):
                continue
            line_obj = dict(line)
            line_obj.setdefault("language", lang_code)
            cleaned_dialogue.append(line_obj)
        normalized["dialogue"] = cleaned_dialogue

        if not normalized.get("lip_sync_director_note"):
            if cleaned_dialogue:
                normalized["lip_sync_director_note"] = "Lip sync theo thoại từng nhân vật; nhân vật không có thoại giữ mouth_locked=true"
            else:
                normalized["lip_sync_director_note"] = "mouth_locked=true, no_lip_sync, keep mouth closed and neutral"

        if not normalized.get("summary"):
            summary_source = str(normalized.get("content", "")).strip()
            normalized["summary"] = (summary_source[:220] + "...") if len(summary_source) > 220 else summary_source

        return normalized
    
    def parse_step2_response(resp_text):
        if not resp_text:
            return []
        resp_text = resp_text.replace('```json', '').replace('```', '').strip()

        output_objects = []

        # Try parsing as single JSON array first
        try:
            data = json.loads(resp_text)
            if isinstance(data, list):
                for scene in data:
                    if isinstance(scene, dict):
                        output_objects.append(scene)
            elif isinstance(data, dict) and "scenes" in data:
                for scene in data.get("scenes", []):
                    if isinstance(scene, dict):
                        output_objects.append(scene)
            elif isinstance(data, dict):
                output_objects.append(data)
            if output_objects:
                return output_objects
        except json.JSONDecodeError:
            pass

        # Strategy 2: brace counting
        current_obj = ""
        brace_count = 0
        in_string = False
        escape_next = False

        for char in resp_text:
            if escape_next:
                current_obj += char
                escape_next = False
                continue
            if char == '\\' and in_string:
                current_obj += char
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                current_obj += char
                continue

            if not in_string:
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1

            current_obj += char

            if brace_count == 0 and current_obj.strip() and not in_string:
                try:
                    scene_json = json.loads(current_obj.strip())
                    if isinstance(scene_json, dict):
                        output_objects.append(scene_json)
                except json.JSONDecodeError:
                    pass
                current_obj = ""

        if output_objects:
            return output_objects

        # Strategy 3: line-by-line parsing
        for line in resp_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                scene_json = json.loads(line)
                if isinstance(scene_json, dict):
                    output_objects.append(scene_json)
            except json.JSONDecodeError:
                pass

        return output_objects

    def build_step2_prompt(scene_ids):
        scene_id_list = ", ".join([str(sid) for sid in scene_ids])
        outline_items = []
        for sid in scene_ids:
            outline_item = scene_outline_map.get(str(sid))
            if outline_item:
                outline_items.append(outline_item)
        outline_text = json.dumps(outline_items, ensure_ascii=False, indent=2) if outline_items else "[]"
        return f"""You are an expert screenwriter for animated films. Your task is to create detailed scene descriptions and dialogue based on characters, environments, and the provided scene outline.

⚠️ IMPORTANT LANGUAGE REQUIREMENTS:
- All scene descriptions and content MUST be written in ENGLISH
- Only dialogue/lines should be in {language_str} ({lang_code})

STORY/IDEA:
{script}

STORY SUMMARY (from Step 1):
{story_summary}

SCENE OUTLINE (from Step 1):
{outline_text}

CHARACTER DATA:
{char_data}

BACKGROUND DATA:
{bg_data}

SAFETY & COMPLIANCE RULES:
- All characters must be fictional. Do NOT reference real people, celebrities, or public figures.
- Do NOT use labels that imply a real person (avoid terms like "human-girl").
- For child characters: keep descriptions age-appropriate; avoid precise measurements.
- Do NOT include logos, brand names, mascots, or "official/advertisement" language.
- Do NOT reference voice cloning or real voice likeness.

========== CREATE DETAILED SCENES ==========

For each scene, create (expand from the scene outline above):
1. scene_id: Scene number
2. content: Detailed action description in ENGLISH combining character actions with environment
3. chars: List of character IDs used (CHAR_1, CHAR_2, etc.)
4. background: Background ID used (BACKGROUND_1, etc.)
5. char_items: Items each character uses/holds
6. bg_landmarks: Key background elements visible
7. dialogue: Dialogue lines with speaker, language code, and line content
8. duration_sec: Always 8
9. character_lock: Per-scene character lock map (prefer reuse from Step 1)
10. background_lock: Per-scene background lock map (prefer reuse from Step 1)
11. camera: framing, angle, movement, focus
12. foley_and_ambience: ambience/fx/music
13. lip_sync_director_note: note for lip sync behavior
14. summary: 1–3 sentence concise scene summary

DIALOGUE FORMAT (dialogue MUST be in {language_str}):
- Speaker: CHAR_ID or character name
- Language: {lang_code}
- Line: Dialogue ONLY in {language_str}, in brackets like [Hôm nay là ngày tốt lành!]

⚠️ CRITICAL:
- "content" field → ENGLISH description of what happens
- "dialogue.line" field → {language_str} dialogue/speech

========== OUTPUT FORMAT ==========

CRITICAL RULES:
- You MUST output EXACTLY {len(scene_ids)} scenes. NOT LESS. NOT MORE.
- If you are unsure, still output placeholders that match the required JSON structure.
- Each scene_id must be unique and be one of: [{scene_id_list}].
- Keep the same order as the list above.
- Return ONLY valid JSON objects (one per line). No markdown, no extra text.

For each scene, output ONE complete JSON object per line (JSON Lines format):

{{
    "scene_id": "1",
    "content": "[ENGLISH: Detailed action mixing character and environment - describe what's happening]",
    "chars": ["CHAR_1", "CHAR_2"],
    "background": "BACKGROUND_1",
    "char_items": {{"CHAR_1": ["item1", "item2"]}},
    "bg_landmarks": ["landmark1", "landmark2"],
    "duration_sec": 8,
    "character_lock": {{
        "CHAR_1": {{"id": "CHAR_1", "name": "...", "pose": "...", "expression": "..."}}
    }},
    "background_lock": {{
        "BACKGROUND_1": {{"id": "BACKGROUND_1", "name": "...", "setting": "...", "lighting": "..."}}
    }},
    "camera": {{"framing": "...", "angle": "...", "movement": "...", "focus": "..."}},
    "foley_and_ambience": {{"ambience": ["..."], "fx": ["..."], "music": "..."}},
    "dialogue": [
        {{"speaker": "CHAR_1", "language": "{lang_code}", "line": "[{language_str}: Lời thoại của nhân vật]"}},
        {{"speaker": "CHAR_2", "language": "{lang_code}", "line": "[{language_str}: Lời thoại của nhân vật]"}}
    ],
    "lip_sync_director_note": "For characters WITH dialogue: detailed lip sync instructions. For characters WITHOUT dialogue: mouth_locked=true, no_lip_sync",
    "summary": "[1–3 sentence scene summary]"
}}

OUTPUT EXACTLY {len(scene_ids)} scenes, one JSON object per line. Do NOT wrap in array. Do NOT add markdown. Return ONLY JSON Lines format.

STRICT REQUIRED KEYS (must exist in every scene object):
- legacy keys: scene_id, content, chars, background, char_items, bg_landmarks, dialogue
- step3 keys: scene_id, duration_sec, character_lock, background_lock, camera, foley_and_ambience, dialogue, lip_sync_director_note, summary"""

    # ✅ CHUNKING STEP 2
    max_scenes_per_request = 10
    scene_ids = list(range(1, num_scenes + 1))
    chunks = [scene_ids[i:i + max_scenes_per_request] for i in range(0, num_scenes, max_scenes_per_request)]

    log(f"\n📤 Sẽ gửi {len(chunks)} request(s) cho Step 2...")
    output_map = {}
    max_attempts_per_chunk = 3
    minimal_required_fields = legacy_scene_fields

    for chunk_idx, chunk_scene_ids in enumerate(chunks, 1):
        if stop_check and stop_check():
            log("⏹️ Đã dừng trước khi xử lý Step 2 chunk")
            return None

        pending_scene_ids = [str(sid) for sid in chunk_scene_ids]
        attempt = 0

        while pending_scene_ids and attempt < max_attempts_per_chunk:
            attempt += 1
            prompt = build_step2_prompt(pending_scene_ids)

            log(f"\n📤 Gửi request Step 2 chunk {chunk_idx}/{len(chunks)} ({len(pending_scene_ids)} scenes) - lần {attempt}/{max_attempts_per_chunk}...")

            response_text, current_key_index = call_gemini_with_retry(
                prompt, api_keys, current_key_index, log, project_dir, stop_check=stop_check
            )

            # ✅ LƯU API KEY INDEX CHO LẦN CHẠY TIẾP THEO
            save_current_api_key_index(current_key_index, project_dir)

            if not response_text:
                log(f"❌ Step 2 chunk {chunk_idx} thất bại (lần {attempt})")
                continue

            parsed_objects = parse_step2_response(response_text)
            if not parsed_objects:
                log(f"⚠️ Step 2 chunk {chunk_idx}: Không parse được JSON (lần {attempt})")
                continue

            for obj in parsed_objects:
                scene_id = str(obj.get("scene_id", "")).strip()
                if any(field_name not in obj for field_name in minimal_required_fields):
                    continue
                missing_step3 = [field_name for field_name in step3_required_fields if field_name not in obj]
                if missing_step3:
                    log(f"⚠️ Step2 chunk {chunk_idx}, scene {scene_id}: thiếu so với Step3 {missing_step3}")
                if scene_id in pending_scene_ids and scene_id not in output_map:
                    output_map[scene_id] = normalize_scene_for_step3(obj)

            pending_scene_ids = [sid for sid in pending_scene_ids if sid not in output_map]
            if pending_scene_ids:
                log(f"⚠️ Step 2 chunk {chunk_idx}: Missing scene_ids sau lần {attempt}: {pending_scene_ids}")

        if pending_scene_ids:
            log(f"❌ Step 2 chunk {chunk_idx} không đủ scenes sau {max_attempts_per_chunk} lần. Dừng Step 2.")
            return None

    # ✅ Tổng hợp output theo thứ tự scene_id
    output_objects = [output_map[str(sid)] for sid in scene_ids if str(sid) in output_map]

    if len(output_objects) != num_scenes:
        log(f"⚠️ Step 2 thiếu scenes: {len(output_objects)}/{num_scenes}")
        return None

    # ✅ CHECK CUỐI: Step2 còn thiếu field nào so với schema Step3 không
    missing_summary = {}
    for scene_obj in output_objects:
        if not isinstance(scene_obj, dict):
            continue
        scene_id = str(scene_obj.get("scene_id", "?")).strip()
        missing_fields = [field_name for field_name in step3_required_fields if field_name not in scene_obj]
        if missing_fields:
            missing_summary[scene_id] = missing_fields
    if missing_summary:
        log("⚠️ Kiểm tra Step2 vs Step3: vẫn còn thiếu fields")
        for sid, fields in missing_summary.items():
            log(f"   - Scene {sid}: {fields}")
    else:
        log("✅ Kiểm tra Step2 vs Step3: đủ fields (bao gồm camera)")
    
    # Lưu file (JSON array - mỗi phần tử là 1 scene)
    output_file = Path(project_dir) / "step2_shot_file.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_objects, f, ensure_ascii=False, indent=2)
    
    log(f"✅ Bước 2 hoàn thành!")
    log(f"   Lưu: {output_file}")
    log(f"   Số cảnh: {len(output_objects)}")
    return output_objects


def parse_response_to_prompts(response_text, visual_style, log, chunk_idx=None):
    """
    Parse Gemini response thành JSON Lines format
    Xử lý markdown wrappers, brace counting, line-by-line parsing
    """
    def sanitize_json_text(text):
        if text is None:
            return ""
        text = text.strip()
        text = text.replace('\ufeff', '')
        text = text.replace('“', '"').replace('”', '"').replace('’', "'")
        # Remove control chars except \n, \r, \t
        text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
        # Trim to first/last JSON-like bracket
        first_candidates = [i for i in [text.find('{'), text.find('[')] if i != -1]
        last_candidates = [i for i in [text.rfind('}'), text.rfind(']')] if i != -1]
        if first_candidates and last_candidates:
            first_idx = min(first_candidates)
            last_idx = max(last_candidates)
            if last_idx > first_idx:
                text = text[first_idx:last_idx + 1]
        # Remove trailing commas
        text = re.sub(r',\s*([}\]])', r'\1', text)
        return text.strip()

    def try_parse_json(text):
        if not text:
            return None
        candidates = [text, sanitize_json_text(text)]
        if "'" in text and '"' not in text:
            candidates.append(sanitize_json_text(text).replace("'", '"'))
        for cand in candidates:
            try:
                return json.loads(cand)
            except Exception:
                continue
        return None

    output_lines = []
    response_text = (response_text or "").strip()
    
    # ✅ STRIP MARKDOWN WRAPPERS
    if response_text.startswith('```') and response_text.endswith('```'):
        lines = response_text.split('\n')
        if lines[0].startswith('```') and lines[-1].strip() == '```':
            lines = lines[1:-1]
            if lines and lines[0].startswith('json'):
                lines[0] = lines[0][4:]
            response_text = '\n'.join(lines).strip()
            if chunk_idx:
                log(f"   ✅ Removed markdown wrapper (chunk {chunk_idx})")
    
    # ✅ IMPROVED MARKDOWN CLEANUP
    response_text = response_text.replace('```\n```json', '\n')
    response_text = response_text.replace('```json', '').replace('```', '')
    response_text = response_text.strip()

    raw_lines = [line.strip() for line in response_text.split('\n') if line.strip()]
    if raw_lines and all(line.startswith('{') and line.endswith('}') for line in raw_lines):
        for line in raw_lines:
            try:
                scene_json = try_parse_json(line)
                if isinstance(scene_json, dict):
                    scene_json["visual_style"] = visual_style
                    output_lines.append(json.dumps(scene_json, ensure_ascii=False))
            except Exception:
                pass
        if output_lines and chunk_idx:
            log(f"   ✅ Strategy 0: Parsed {len(output_lines)} scenes (chunk {chunk_idx})")
        if output_lines:
            return output_lines
    
    cleaned_lines = raw_lines
    response_text = '\n'.join(cleaned_lines)
    
    # Strategy 1: Try parse as single array JSON
    try:
        data = try_parse_json(response_text)
        if data is None:
            raise json.JSONDecodeError("Invalid JSON", response_text, 0)
        if isinstance(data, list):
            for scene in data:
                if isinstance(scene, dict):
                    scene["visual_style"] = visual_style
                    output_lines.append(json.dumps(scene, ensure_ascii=False))
        elif isinstance(data, dict) and "scenes" in data:
            for scene in data.get("scenes", []):
                if isinstance(scene, dict):
                    scene["visual_style"] = visual_style
                    output_lines.append(json.dumps(scene, ensure_ascii=False))
        elif isinstance(data, dict):
            data["visual_style"] = visual_style
            output_lines.append(json.dumps(data, ensure_ascii=False))
        if output_lines and chunk_idx:
            log(f"   ✅ Strategy 1: Parsed {len(output_lines)} scenes (chunk {chunk_idx})")
        return output_lines
    except json.JSONDecodeError:
        pass
    
    # Strategy 2: Brace counting
    current_obj = ""
    brace_count = 0
    in_string_double = False
    in_string_single = False
    escape_next = False
    
    for char in response_text:
        if escape_next:
            current_obj += char
            escape_next = False
            continue
        
        if char == '\\' and (in_string_double or in_string_single):
            current_obj += char
            escape_next = True
            continue
        
        if char == '"' and not in_string_single:
            in_string_double = not in_string_double
            current_obj += char
            continue
        if char == "'" and not in_string_double:
            in_string_single = not in_string_single
            current_obj += char
            continue
        
        if not in_string_double and not in_string_single:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
        
        current_obj += char
        
        if brace_count == 0 and current_obj.strip() and not in_string_double and not in_string_single:
            try:
                scene_json = try_parse_json(current_obj.strip())
                if isinstance(scene_json, dict):
                    scene_json["visual_style"] = visual_style
                    output_lines.append(json.dumps(scene_json, ensure_ascii=False))
            except json.JSONDecodeError:
                pass
            current_obj = ""
    
    if output_lines and chunk_idx:
        log(f"   ✅ Strategy 2: Parsed {len(output_lines)} scenes (chunk {chunk_idx})")
    if output_lines:
        return output_lines
    
    # Strategy 3: Line-by-line
    lines = response_text.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            scene_json = try_parse_json(line)
            if isinstance(scene_json, dict):
                scene_json["visual_style"] = visual_style
                output_lines.append(json.dumps(scene_json, ensure_ascii=False))
        except json.JSONDecodeError:
            pass
    
    if output_lines and chunk_idx:
        log(f"   ✅ Strategy 3: Parsed {len(output_lines)} scenes (chunk {chunk_idx})")
    
    return output_lines


def gemini_step_3(step1_data, step2_data, script, project_dir, scene_count, style, language, log_callback=None, stop_check=None):
    """
    BƯỚC 3: Tạo prompt chi tiết
    Input: step1_data, step2_data + config từ UI, log_callback
    Output: step3_prompt_file.json (JSON lines với visual_style + prompt)
    
    📖 Dùng config từ UI: scene, style, language
    ✅ OPTIMIZATION: Nếu > 5 scenes, chia thành nhiều requests (max 5 scenes/request)
    """
    project_dir = str(project_dir or "")
    num_scenes = int(max(1, min(100, int(scene_count or 5))))
    style = str(style or "3d_Pixar")
    language_str = str(language or "Tiếng Việt (vi-VN)")
    # Extract language code from "Tiếng Việt (vi-VN)" → "vi-VN"
    lang_code = language_str.split('(')[-1].rstrip(')') if '(' in language_str else "vi-VN"
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    if stop_check and stop_check():
        log("⏹️ Đã dừng trước khi bắt đầu Step 3")
        return None
    
    log("\n" + "="*70)
    log("🎬 BƯỚC 3: TẠO PROMPT CHI TIẾT")
    log("="*70)

    
    if not step1_data or not step2_data:
        log("❌ Cần dữ liệu từ bước 1 & 2")
        return None
    
    api_keys = load_api_keys()
    if not api_keys:
        log("❌ Không tìm thấy API keys")
        return None
    
    # ✅ LẤY API KEY INDEX TỪ LẦN CHẠY TRƯỚC (NẾU CÓ)
    current_key_index = get_current_api_key_index(project_dir)
    
    # Format dữ liệu (KHÔNG gửi visual_style)
    char_data = json.dumps(step1_data.get("character_lock", {}), ensure_ascii=False, indent=2)
    bg_data = json.dumps(step1_data.get("background_lock", {}), ensure_ascii=False, indent=2)
    
    # ✅ PARSE ALL SCENES FROM STEP2_DATA (array of objects)
    all_scenes = []
    if isinstance(step2_data, list):
        all_scenes = [s for s in step2_data if isinstance(s, dict)]
    else:
        try:
            parsed = json.loads(step2_data)
            if isinstance(parsed, list):
                all_scenes = [s for s in parsed if isinstance(s, dict)]
            elif isinstance(parsed, dict) and "scenes" in parsed:
                all_scenes = [s for s in parsed.get("scenes", []) if isinstance(s, dict)]
            elif isinstance(parsed, dict):
                all_scenes = [parsed]
        except Exception:
            all_scenes = []

    total_scenes = len(all_scenes)
    log(f"📊 Tổng số cảnh: {total_scenes}")
    
    if total_scenes == 0:
        log("❌ Không tìm thấy scenes từ step 2")
        return None
    
    # ✅ CHIA THÀNH CHUNKS CỦA 5 SCENES
    max_scenes_per_request = 5
    chunks = []
    for i in range(0, total_scenes, max_scenes_per_request):
        if stop_check and stop_check():
            log("⏹️ Đã dừng khi chia chunks")
            return None
        chunk = all_scenes[i:i + max_scenes_per_request]
        chunks.append(chunk)
    
    num_requests = len(chunks)
    log(f"📋 Sẽ gửi {num_requests} request(s):")
    for idx, chunk in enumerate(chunks, 1):
        log(f"   Request {idx}: {len(chunk)} scenes (scenes {chunk[0].get('scene_id', '?')}-{chunk[-1].get('scene_id', '?')})")
    
    # Load visual_style để thêm vào mỗi scene
    visual_style = load_visual_style(style)
    
    # ✅ PROCESS EACH CHUNK
    all_output_objects = []
    responses_log = {}  # ✅ LƯU RESPONSE ĐỂ DEBUG
    output_file = Path(project_dir) / "step3_prompts_file.json"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    response_log_file = Path(project_dir) / "respon_step3.json"
    # Reset file để ghi incremental dạng JSON array
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    def append_response_log(entry):
        """Append raw response log for step3 (JSON lines, no overwrite)."""
        try:
            entry["timestamp"] = datetime_module.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(response_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass
    
    for chunk_idx, chunk in enumerate(chunks, 1):
        if stop_check and stop_check():
            log("⏹️ Đã dừng trước khi xử lý chunk")
            return None
        # log(f"\n⏳ Đang xử lý chunk {chunk_idx}/{num_requests}...")
        
        # Convert chunk scenes back to step2_data format
        chunk_step2_data = '\n'.join([json.dumps(scene, ensure_ascii=False) for scene in chunk])
        
        # Build scene summaries for this chunk
        scene_summaries = []
        for scene in chunk:
            if stop_check and stop_check():
                log("⏹️ Đã dừng khi chuẩn bị chunk")
                return None
            scene_id = scene.get('scene_id', '?')
            content = scene.get('content', '')[:150]
            scene_summaries.append(f"Scene {scene_id}: {content}...")
        scenes_info = '\n'.join(scene_summaries)
        
        # Build prompt for this chunk
        scene_id_list = ", ".join([str(scene.get('scene_id', '?')) for scene in chunk])

        prompt = f"""You are an expert prompt engineer for AI video generators.

    Your job is to transform EACH input scene into ONE output JSON object with a strict schema.

    IMPORTANT:
    - Output language for descriptions: ENGLISH
    - Dialogue line language: {language_str} ({lang_code})
    - Output count MUST be exactly {len(chunk)} objects
    - Allowed scene_id list: [{scene_id_list}]
    - Use each scene_id exactly once. No missing, no duplicates, no extra scene_ids.

    SOURCE OF TRUTH:
    - FULL SCENE DATA is the primary source for scene_id, dialogue, characters, background context.
    - Keep structural consistency with input scene data.

🌍 IMPORTANT: All descriptions and prompts should be in ENGLISH.
📝 Dialogue will be in {language_str} ({lang_code}) separately.

CHARACTER DATA:
{char_data}

BACKGROUND DATA:
{bg_data}

SCENE BREAKDOWN ({len(chunk)} scenes):
{scenes_info}

FULL SCENE DATA (authoritative):
{chunk_step2_data}

SAFETY & COMPLIANCE RULES:
- All characters must be fictional. Do NOT reference real people, celebrities, or public figures.
- Do NOT use labels that imply a real person (avoid terms like "human-girl").
- For child characters: keep descriptions age-appropriate; avoid precise measurements or sexualized details.
- Do NOT include logos, brand names, mascots, or "official/advertisement" language.
- Do NOT reference voice cloning or real voice likeness.

BODY CONSISTENCY RULES (VERY IMPORTANT):
- Keep the same body proportions for each recurring character across all scenes.
- Do NOT make any scene chubbier/heavier than others unless explicitly requested by story.
- Avoid low-angle body exaggeration; prefer eye-level camera for character shots.
- Avoid poses that imply chest enlargement (e.g., chest puffed out).
- Add in each character_lock item:
    - proportion_rule: "{BODY_PROPORTION_RULE}"
    - body_constraint: "{BODY_CONSTRAINT_RULE}"
- Add in camera:
    - lens: "{CAMERA_LENS_RULE}"

REQUIRED OUTPUT KEYS (all mandatory for each scene):
- scene_id
- duration_sec
- character_lock
- background_lock
- camera
- foley_and_ambience
- dialogue
- lip_sync_director_note
- summary

SCHEMA NOTES:
- duration_sec MUST be number 8
- dialogue MUST be a JSON array (can be empty)
- camera MUST be object with: framing, angle, movement, focus
- foley_and_ambience MUST be object with: ambience, fx, music
- summary MUST be concise 1-3 sentences

OUTPUT FORMAT (JSON Lines, one object per line):

{{
  "scene_id": "1",
    "duration_sec": 8,
  "character_lock": {{
    "CHAR_1": {{
      "id": "...",
      "name": "...",
      "species": "...",
      "gender": "...",
      "age": "...",
      "voice_personality": "...",
      "body_build": "...",
      "face_shape": "[Detailed: eye shape/color/size, nose description, mouth shape, ear shape, chin, facial markings]",
      "hair": "...",
      "skin_or_fur_color": "...",
      "signature_feature": "...",
      "outfit_top": "[Detailed pattern, color, material, decorations]",
      "outfit_bottom": "[Detailed pattern, color, material, decorations]",
      "helmet_or_hat": "[If applicable, detailed description]",
      "shoes_or_footwear": "[Detailed description, color, wear]",
      "props": ["[Each prop with detail]"],
      "body_metrics": "...",
      "position": "...",
      "orientation": "...",
      "pose": "...",
      "foot_placement": "...",
      "hand_detail": "...",
      "expression": "[Detailed facial expression]",
      "action_flow": {{
        "pre_action": "...",
        "main_action": "...",
        "post_action": "..."
      }}
    }}
  }},
  "background_lock": {{
    "BACKGROUND_1": {{
      "id": "...",
      "name": "...",
      "setting": "...",
      "scenery": "...",
      "props": "...",
      "lighting": "..."
    }}
  }},
  "camera": {{
    "framing": "...",
    "angle": "...",
    "movement": "...",
    "focus": "..."
  }},
  "foley_and_ambience": {{
    "ambience": ["...", "..."],
    "fx": ["...", "..."],
    "music": "..."
  }},
  "dialogue": [
    {{
      "speaker": "CHAR_1",
      "voice": "[Voice description]",
      "language": "{lang_code}",
      "line": "[Dialogue in {language_str}]"
    }}
  ],
  "lip_sync_director_note": "For characters WITH dialogue: detailed lip sync instructions. For characters WITHOUT dialogue: 'mouth_locked=true, no_lip_sync, keep mouth closed and neutral'",
    "summary": "[1–3 sentence scene summary focused on story intent]"
}}

STRICT OUTPUT CONTRACT:
- Return ONLY JSON Lines. No markdown. No explanations. No extra text.
- The first character of your response MUST be '{{' and every line MUST be a single JSON object ending with '}}'.
- Each object MUST include ALL required keys: scene_id, duration_sec, character_lock, background_lock, camera, foley_and_ambience, dialogue, lip_sync_director_note, summary.
- scene_id MUST be one of [{scene_id_list}] and MUST match exactly one input scene.
- If unsure, still output placeholders but keep the full structure and required keys.
- duration_sec MUST be 8 for every scene.

OUTPUT {len(chunk)} scenes, one JSON object per line. Do NOT wrap in array. Return ONLY JSON Lines format."""
        
        log(f"📤 Gửi request {chunk_idx}/{num_requests} tới Gemini ({len(chunk)} scenes)...")
        
        expected_prompts = len(chunk)  # ✅ Track expected count
        response_text, current_key_index = call_gemini_with_retry(
            prompt, api_keys, current_key_index, log, project_dir, stop_check=stop_check
        )

        append_response_log({
            "chunk": chunk_idx,
            "attempt": 1,
            "expected_scenes": expected_prompts,
            "raw_response": response_text or "",
            "response_length": len(response_text) if response_text else 0
        })
        
        # ✅ LƯU RAW RESPONSE ĐỂ DEBUG
        responses_log[f"chunk_{chunk_idx}"] = {
            "expected_scenes": expected_prompts,
            "raw_response": response_text[:500] if response_text else None,
            "response_length": len(response_text) if response_text else 0
        }
        
        if not response_text:
            log(f"❌ Request {chunk_idx} thất bại")
            continue
        
        # ✅ VALIDATE & PARSE RESPONSE
        chunk_output_lines = parse_response_to_prompts(response_text, visual_style, log, chunk_idx)
        
        # ✅ VALIDATION: Kiểm tra cấu trúc từng prompt + chỉ nhận đúng scene_id trong chunk
        expected_scene_ids = [str(scene.get("scene_id", "")).strip() for scene in chunk if scene.get("scene_id") is not None]
        valid_map = {}
        required_fields = [
            "scene_id", "duration_sec", "character_lock", "background_lock",
            "camera", "foley_and_ambience", "dialogue", "lip_sync_director_note", "summary"
        ]
        
        for idx, prompt_line in enumerate(chunk_output_lines, 1):
            if stop_check and stop_check():
                log("⏹️ Đã dừng khi validate prompts")
                return None
            try:
                prompt_obj = json.loads(prompt_line) if isinstance(prompt_line, str) else prompt_line
                if isinstance(prompt_obj, dict) and "summary" not in prompt_obj and "prompt" in prompt_obj:
                    prompt_obj["summary"] = prompt_obj.get("prompt", "")
                if isinstance(prompt_obj, dict):
                    prompt_obj = _enforce_scene_body_consistency(prompt_obj)
                missing_fields = [f for f in required_fields if f not in prompt_obj]
                scene_id = str(prompt_obj.get("scene_id", "")).strip()
                
                if missing_fields:
                    log(f"⚠️ Chunk {chunk_idx}, Prompt {idx}: Thiếu fields {missing_fields}")
                    continue
                if scene_id not in expected_scene_ids:
                    log(f"⚠️ Chunk {chunk_idx}, Prompt {idx}: scene_id không thuộc chunk ({scene_id})")
                    continue
                if scene_id not in valid_map:
                    valid_map[scene_id] = prompt_obj
            except json.JSONDecodeError as e:
                log(f"❌ Chunk {chunk_idx}, Prompt {idx}: JSON không hợp lệ - {str(e)[:50]}")
            except Exception as e:
                log(f"❌ Chunk {chunk_idx}, Prompt {idx}: Lỗi validate - {str(e)[:50]}")

        valid_prompts = [valid_map[sid] for sid in expected_scene_ids if sid in valid_map]
        missing_scene_ids = [sid for sid in expected_scene_ids if sid not in valid_map]
        
        # ✅ REPORT VALIDATION
        actual_prompts = len(valid_prompts)
        if actual_prompts < expected_prompts:
            log(f"⚠️ Chunk {chunk_idx}: Expected {expected_prompts}, got {actual_prompts} valid prompts")
            log(f"   Missing scene_ids: {missing_scene_ids}")

            # ✅ RE-REQUEST CHỈ NHỮNG SCENES THIẾU (KỂ CẢ KHI 0 VALID)
            if missing_scene_ids:
                retry_attempt = 1
                max_retry_missing = 3
                while missing_scene_ids and retry_attempt <= max_retry_missing:
                    if stop_check and stop_check():
                        log("⏹️ Đã dừng trước khi retry missing scenes")
                        return None

                    log(
                        f"🔄 Re-requesting {len(missing_scene_ids)} missing scenes từ chunk {chunk_idx} "
                        f"(attempt {retry_attempt}/{max_retry_missing})..."
                    )
                    # Lấy những scenes bị thiếu
                    failed_scenes = [scene for scene in chunk if str(scene.get("scene_id", "")).strip() in missing_scene_ids]
                    if not failed_scenes:
                        break

                    failed_chunk_step2_data = '\n'.join([json.dumps(scene, ensure_ascii=False) for scene in failed_scenes])
                    missing_scene_id_list = ", ".join(missing_scene_ids)

                    retry_prompt = f"""RETRY: Fix the following {len(failed_scenes)} scenes that had invalid JSON structure.

You are an expert prompt engineer for AI video generators. Your task is to output complete, valid JSON objects for the missing scenes.

🌍 IMPORTANT: All descriptions and prompts should be in ENGLISH.
📝 Dialogue will be in {language_str} ({lang_code}) separately.

CHARACTER DATA:
{char_data}

BACKGROUND DATA:
{bg_data}

SCENES TO FIX:
{failed_chunk_step2_data}

ALLOWED SCENE IDS FOR THIS RETRY: [{missing_scene_id_list}]

SAFETY & COMPLIANCE RULES:
- All characters must be fictional. Do NOT reference real people, celebrities, or public figures.
- Do NOT use labels that imply a real person (avoid terms like "human-girl").
- For child characters: keep descriptions age-appropriate; avoid precise measurements or sexualized details.
- Do NOT include logos, brand names, mascots, or "official/advertisement" language.
- Do NOT reference voice cloning or real voice likeness.

SUMMARY REQUIREMENTS:
- 1–3 sentences, concise and high-level.
- Describe: where the scene takes place, which characters are involved, the main action/interaction, the overall emotion or tone, and the narrative purpose.
- Focus on story/intent, not visual or technical details.
- Do NOT describe camera, lighting, character dimensions, poses, or props.
- Do NOT repeat detailed information already specified in structured fields.
- Do NOT include brand/style references.
- Do NOT include dialogue quotes; refer to dialogue generally.

REQUIREMENTS:
- Output exactly {len(failed_scenes)} valid JSON objects
- One complete scene JSON per line (JSON Lines format)
- Each must have: scene_id, duration_sec, character_lock, background_lock, camera, foley_and_ambience, dialogue, lip_sync_director_note, summary
- scene_id must be one of [{missing_scene_id_list}], each id exactly once (no duplicates, no extra id)
- Summary must be 1–3 sentences and follow the summary rules above
- NO array wrapper, NO markdown, pure JSON Lines only
- Enforce body consistency: no chubby/fat drift between scenes, no chest enlargement, no wide-angle distortion.
- Set camera angle to eye level unless story strictly requires otherwise.
- Include camera lens: "{CAMERA_LENS_RULE}".

STRICT OUTPUT CONTRACT:
- The first character of your response MUST be '{{'.
- Every line MUST be a single JSON object ending with '}}'.
- Do NOT include any extra text, headers, or explanations.
- If unsure, still output placeholders but keep the full structure and required keys.
- duration_sec MUST be 8 for every scene.

OUTPUT FORMAT (JSON Lines, one object per line):
{{
    "scene_id": "1",
    "duration_sec": 8,
    "character_lock": {{
        "CHAR_1": "[Full character description with position/orientation/pose/hand/face/clothing details]"
    }},
    "background_lock": {{
        "id": "BACKGROUND_1",
        "name": "...",
        "setting": "...",
        "scenery": "...",
        "props": "...",
        "lighting": "..."
    }},
    "camera": {{
        "framing": "...",
        "angle": "...",
        "movement": "...",
        "focus": "..."
    }},
    "foley_and_ambience": {{
        "ambience": ["...", "..."],
        "fx": ["...", "..."],
        "music": "..."
    }},
    "dialogue": [
        {{
            "speaker": "CHAR_1",
            "voice": "[Voice description]",
            "language": "{lang_code}",
            "line": "[Dialogue in {language_str}]"
        }}
    ],
    "lip_sync_director_note": "For characters WITH dialogue: detailed lip sync instructions. For characters WITHOUT dialogue: 'mouth_locked=true, no_lip_sync, keep mouth closed and neutral'",
    "summary": "[1–3 sentence scene summary focused on story intent]"
}}
"""
                        
                    if stop_check and stop_check():
                        log("⏹️ Đã dừng trước khi gọi retry API")
                        return None
                    retry_response, current_key_index = call_gemini_with_retry(
                        retry_prompt, api_keys, current_key_index, log, project_dir, stop_check=stop_check
                    )

                    append_response_log({
                        "chunk": chunk_idx,
                        "attempt": 1 + retry_attempt,
                        "expected_scenes": len(failed_scenes),
                        "missing_scene_ids": list(missing_scene_ids),
                        "raw_response": retry_response or "",
                        "response_length": len(retry_response) if retry_response else 0
                    })

                    if retry_response:
                        retry_prompts = parse_response_to_prompts(retry_response, visual_style, log, f"{chunk_idx}_retry_{retry_attempt}")

                        # Validate retry prompts
                        for retry_prompt_line in retry_prompts:
                            if stop_check and stop_check():
                                log("⏹️ Đã dừng khi validate retry prompts")
                                return None
                            try:
                                retry_obj = json.loads(retry_prompt_line) if isinstance(retry_prompt_line, str) else retry_prompt_line
                                if isinstance(retry_obj, dict) and "summary" not in retry_obj and "prompt" in retry_obj:
                                    retry_obj["summary"] = retry_obj.get("prompt", "")
                                if isinstance(retry_obj, dict):
                                    retry_obj = _enforce_scene_body_consistency(retry_obj)
                                retry_scene_id = str(retry_obj.get("scene_id", "")).strip()
                                if all(f in retry_obj for f in required_fields) and retry_scene_id in missing_scene_ids:
                                    if retry_scene_id not in valid_map:
                                        valid_map[retry_scene_id] = retry_obj
                                        log(f"✅ Fixed scene {retry_scene_id}")
                            except Exception:
                                pass

                    valid_prompts = [valid_map[sid] for sid in expected_scene_ids if sid in valid_map]
                    missing_scene_ids = [sid for sid in expected_scene_ids if sid not in valid_map]

                    retry_attempt += 1

                if missing_scene_ids:
                    log(f"❌ Chunk {chunk_idx}: vẫn thiếu scene sau retry: {missing_scene_ids}")
                else:
                    log(f"✅ Chunk {chunk_idx}: đã fix đủ scenes sau retry")
        else:
            log(f"✅ Got all {expected_prompts} valid prompts for chunk {chunk_idx}")

        if not valid_prompts:
            log(f"❌ Request {chunk_idx} không return prompts hợp lệ nào")
        
        # ✅ Append valid prompts -> lưu dạng list object
        if valid_prompts:
            for prompt_line in valid_prompts:
                try:
                    prompt_obj = json.loads(prompt_line) if isinstance(prompt_line, str) else prompt_line
                except Exception:
                    prompt_obj = None
                if isinstance(prompt_obj, dict):
                    if "summary" not in prompt_obj and "prompt" in prompt_obj:
                        prompt_obj["summary"] = prompt_obj.get("prompt", "")
                    if "summary" in prompt_obj and "prompt" in prompt_obj:
                        prompt_obj.pop("prompt", None)
                    all_output_objects.append(prompt_obj)

            # ✅ Lưu incremental vào file sau mỗi chunk
            try:
                with open(output_file, 'w', encoding='utf-8') as f:
                    json.dump(all_output_objects, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        
        if valid_prompts:
            log(f"✅ Chunk {chunk_idx} hoàn thành: {len(valid_prompts)} valid prompts")
        else:
            log(f"❌ Chunk {chunk_idx} không có prompts valid")
    
    if not all_output_objects:
        log(f"⚠️ Không tạo được prompts nào")
        return None

    
    # ✅ LƯU API KEY INDEX CHO LẦN CHẠY TIẾP THEO
    save_current_api_key_index(current_key_index, project_dir)
    
    log(f"\n✅ Bước 3 hoàn thành! Tổng prompts valid: {len(all_output_objects)}/{total_scenes}")

    
    return all_output_objects


def _extract_character_overrides_from_idea(idea_text):
    """
    Parse freeform idea text to override character_lock.

    Expected patterns (examples):
    - Nhan vat:
      Nguoi linh: mo ta...
      Co giao: mo ta...
    - Nguoi linh: mo ta...
      Co giao: mo ta...
    """
    if not idea_text or not str(idea_text).strip():
        return {}

    lines = [line.strip() for line in str(idea_text).splitlines()]
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(r"(nhan\s*vat|characters?)", line, re.IGNORECASE):
            start_idx = i + 1
            break

    candidate_lines = lines[start_idx:] if start_idx is not None else lines
    items = []

    for line in candidate_lines:
        if not line:
            continue
        cleaned = re.sub(r"^[\-\*\u2022]\s*", "", line)
        cleaned = re.sub(r"^\d+[\.\)]\s*", "", cleaned)
        if ":" not in cleaned:
            continue
        name, desc = cleaned.split(":", 1)
        name = name.strip()
        desc = desc.strip()
        if name and desc and len(name) <= 40 and len(desc) >= 5:
            items.append((name, desc))

    if not items:
        return {}

    character_lock = {}
    for idx, (name, desc) in enumerate(items, 1):
        char_id = f"CHAR_{idx}"
        character_lock[char_id] = {
            "id": char_id,
            "name": name,
            "description": desc,
            "raw_description": desc,
        }

    return character_lock


def idea_to_video_workflow(
    project_name,
    idea,
    scene_count=5,
    style="3d_Pixar",
    language="Tiếng Việt (vi-VN)",
    log_callback=None,
    stop_check=None,
):
    """
    Chạy workflow Idea to Video
    
    Args:
        project_name: Tên project hiện tại
        idea: Ý tưởng từ user
        scene_count/style/language: lấy từ UI tại thời điểm start
        log_callback: Function để log lên UI (nếu None thì dùng print)
        stop_check: Function để kiểm tra STOP flag (nếu trả về True = dừng)
    
    Return: 
        {
            "success": bool,
            "message": str,
            "prompts": list[{"id": int, "prompt": str}]  # Nếu success
        }
    """
    def log(msg):
        """Helper để log"""
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    def should_stop():
        """Helper để check STOP"""
        if stop_check and stop_check():
            return True
        return False
    
    try:
        project_name = str(project_name or "default_project").strip() or "default_project"
        project_dir = str(WORKFLOWS_DIR / project_name)
        Path(project_dir).mkdir(parents=True, exist_ok=True)

        num_scenes = int(max(1, min(100, int(scene_count or 5))))
        style = str(style or "3d_Pixar")
        language = str(language or "Tiếng Việt (vi-VN)")

        api_key_file = DATA_GENERAL_DIR / "gemini_api_key.txt"
        legacy_api_key_file = DATA_GENERAL_DIR / "gemini_API" / "gemini_api.txt"
        api_keys = load_api_keys()
        if (not api_key_file.exists()) and (not legacy_api_key_file.exists()):
            msg = "❌ Không tìm thấy file API: data_general/gemini_api_key.txt"
            _show_api_key_error(msg)
            return {"success": False, "message": msg}
        if not api_keys:
            msg = "❌ Không có API key nào cần thêm API key vào trong phần cài đặt và lưu lại rồi chạy lại"
            _show_api_key_error(msg)
            return {"success": False, "message": msg}
        
        log(f"\n🎬 IDEA TO VIDEO WORKFLOW")
        log(f"📝 Ý tưởng: {idea}")
        log(f"🎨 Style: {style}")
        log(f"🎯 Số cảnh: {num_scenes}")
        log(f"🗣️ Ngôn ngữ thoại: {language}")
        
        # ===== STEP 1: Tạo nhân vật + background =====
        # ✅ CHECK STOP trước step 1
        if should_stop():
            log("⏹️ Đã dừng tại STEP 1")
            return {"success": False, "message": "⏹️ Quy trình bị dừng"}
        
        log(f"\n⏳ Step 1: Tạo nhân vật và bối cảnh...")
        step1_result = gemini_step_1(
            idea,
            project_dir,
            num_scenes,
            style,
            log_callback,
            stop_check=stop_check,
        )
        if not step1_result:
            return {
                "success": False,
                "message": "❌ Lỗi Step 1: Không thể tạo nhân vật"
            }
        log(f"✅ Step 1 hoàn thành")
        
        # ===== STEP 2: Tạo script + dialogue =====
        # ✅ CHECK STOP trước step 2
        if should_stop():
            log("⏹️ Đã dừng tại STEP 2")
            return {"success": False, "message": "⏹️ Quy trình bị dừng"}
        
        log(f"\n⏳ Step 2: Tạo kịch bản và đối thoại...")
        step2_result = gemini_step_2(
            step1_result,
            idea,
            project_dir,
            num_scenes,
            style,
            language,
            log_callback,
            stop_check=stop_check,
        )
        if not step2_result:
            return {
                "success": False,
                "message": "❌ Lỗi Step 2: Không thể tạo kịch bản"
            }
        log(f"✅ Step 2 hoàn thành")

        # ===== OVERRIDE CHARACTER_LOCK FROM IDEA (if provided) =====
        character_overrides = _extract_character_overrides_from_idea(idea)
        if character_overrides:
            step1_result = dict(step1_result)
            step1_result["character_lock"] = character_overrides
            log(f"✅ Override character_lock từ idea: {len(character_overrides)} nhân vật")
        
        # ===== STEP 3: Tạo detailed prompts =====
        # ✅ CHECK STOP trước step 3
        if should_stop():
            log("⏹️ Đã dừng tại STEP 3")
            return {"success": False, "message": "⏹️ Quy trình bị dừng"}
        
        log(f"\n⏳ Step 3: Tạo prompts chi tiết...")
        step3_result = gemini_step_3(
            step1_result,
            step2_result,
            idea,
            project_dir,
            num_scenes,
            style,
            language,
            log_callback,
            stop_check=stop_check,
        )
        if not step3_result:
            return {
                "success": False,
                "message": "❌ Lỗi Step 3: Không thể tạo prompts"
            }
        log(f"✅ Step 3 hoàn thành")
        
        # ===== Step 4: Parse Step 3 output và format thành prompts =====
        log(f"\n⏳ Step 4: Format prompts...")
        prompts_list = []

        def sanitize_prompt_text(text):
            if text is None:
                return ""
            if not isinstance(text, str):
                text = str(text)
            text = text.replace('\ufeff', '')
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            return text.strip()
        
        # Step 3 trả về list object
        if isinstance(step3_result, list):
            for idx, prompt_json in enumerate(step3_result, 1):
                prompt_obj = prompt_json if isinstance(prompt_json, dict) else None
                if not isinstance(prompt_obj, dict):
                    try:
                        prompt_obj = json.loads(prompt_json)
                    except Exception:
                        prompt_obj = None

                if isinstance(prompt_obj, dict):
                    scene_id = prompt_obj.get("scene_id", idx)
                    try:
                        prompt_id = int(scene_id)
                    except Exception:
                        prompt_id = idx
                    prompt_text = sanitize_prompt_text(json.dumps(prompt_obj, ensure_ascii=False))
                else:
                    prompt_id = idx
                    prompt_text = sanitize_prompt_text(prompt_json)

                prompts_list.append({
                    "id": prompt_id,
                    "prompt": prompt_text
                })
        else:
            # Nếu là string, split by newline
            lines = str(step3_result).strip().split('\n')
            for idx, line in enumerate(lines, 1):
                if line.strip():
                    prompts_list.append({
                        "id": idx,
                        "prompt": sanitize_prompt_text(line)
                    })
        
        log(f"✅ Đã tạo {len(prompts_list)} prompts")
        
        log("✅ Bỏ lưu prompt vào test.json theo cấu hình luồng mới")
        
        # ===== Success =====
        return {
            "success": True,
            "message": f"✅ Tạo xong {len(prompts_list)} prompts từ ý tưởng",
            "prompts": prompts_list
        }
    
    except Exception as e:
        log(f"❌ Lỗi idea_to_video_workflow: {e}")
        return {
            "success": False,
            "message": f"❌ Lỗi: {str(e)}"
        }


if __name__ == "__main__":
    pass
