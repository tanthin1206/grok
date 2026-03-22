import json
import urllib.error
import urllib.request
import uuid

from settings_manager import SettingsManager

URL_GENERATE_TEXT_TO_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText"
URL_STATUS_TEXT_TO_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"

VIDEO_ASPECT_RATIO_LANDSCAPE = "VIDEO_ASPECT_RATIO_LANDSCAPE"
VIDEO_ASPECT_RATIO_PORTRAIT = "VIDEO_ASPECT_RATIO_PORTRAIT"

DEFAULT_SEED = 9797

# type_account ULTRA
DEFAULT_VIDEO_MODEL_KEY_ULTRA = "veo_3_1_t2v_fast_ultra"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA = "veo_3_1_t2v_fast_portrait_ultra"
DEFAULT_VIDEO_MODEL_KEY_ULTRA_RELAXED = "veo_3_1_t2v_fast_ultra_relaxed"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA_RELAXED = "veo_3_1_t2v_fast_portrait_ultra_relaxed"

# type_account NORMAL / PRO
DEFAULT_VIDEO_MODEL_KEY_NORMAL = "veo_3_1_t2v_fast"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL = "veo_3_1_t2v_fast_portrait"


def _normalize_account_type(value):
    normalized = str(value or "").strip().upper()
    if normalized in {"NORMAL", "PRO", "ULTRA"}:
        return normalized
    return "ULTRA"


def _resolve_type_account():
    try:
        config = SettingsManager.load_config()
        account = config.get("account1", {}) if isinstance(config, dict) else {}
        return _normalize_account_type(account.get("TYPE_ACCOUNT") or account.get("type_account"))
    except Exception:
        return "ULTRA"


def _load_account_context():
    account_type = _resolve_type_account()
    SettingsManager.load_config()  # giữ tương thích, dù hiện tại không cần giá trị khác

    if account_type == "NORMAL":
        return {
            "type_account": "NORMAL",
            "video_model_key_landscape": DEFAULT_VIDEO_MODEL_KEY_NORMAL,
            "video_model_key_portrait": DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL,
            "user_paygate_tier": "PAYGATE_TIER_NOT_PAID",
        }

    if account_type == "PRO":
        return {
            "type_account": "PRO",
            "video_model_key_landscape": DEFAULT_VIDEO_MODEL_KEY_NORMAL,
            "video_model_key_portrait": DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL,
            "user_paygate_tier": "PAYGATE_TIER_ONE",
        }
    if account_type == "ULTRA":
        return {
            "type_account": "ULTRA",
            "video_model_key_landscape": DEFAULT_VIDEO_MODEL_KEY_ULTRA,
            "video_model_key_portrait": DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA,
            "user_paygate_tier": "PAYGATE_TIER_TWO",
        }


# Các biến dưới sẽ được refresh khi import và trước mỗi lần build payload
_TYPE_ACCOUNT = "ULTRA"
DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE = DEFAULT_VIDEO_MODEL_KEY_ULTRA
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA
DEFAULT_VIDEO_MODEL_KEY = DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE  # alias giữ tương thích
USER_PAYGATE_TIER = "PAYGATE_TIER_TWO"

payload_text_to_video = {
    "clientContext": {
        "recaptchaContext": {
            "token": "",
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
        },
        "sessionId": "",
        "projectId": "",
        "tool": "PINHOLE",
        "userPaygateTier": USER_PAYGATE_TIER,
    },
    "requests": [
        {
            "aspectRatio": VIDEO_ASPECT_RATIO_LANDSCAPE,
            "seed": DEFAULT_SEED,
            "textInput": {
                "prompt": "",
            },
            "videoModelKey": DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE,
            "metadata": {
                "sceneId": "",
            },
        }
    ],
}


def refresh_account_context():
    global _TYPE_ACCOUNT, DEFAULT_VIDEO_MODEL_KEY, DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE, DEFAULT_VIDEO_MODEL_KEY_PORTRAIT, USER_PAYGATE_TIER

    context = _load_account_context()
    _TYPE_ACCOUNT = context["type_account"]
    DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE = context["video_model_key_landscape"]
    DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = context["video_model_key_portrait"]
    DEFAULT_VIDEO_MODEL_KEY = DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE
    USER_PAYGATE_TIER = context["user_paygate_tier"]

    payload_text_to_video["clientContext"]["userPaygateTier"] = USER_PAYGATE_TIER
    payload_text_to_video["requests"][0]["videoModelKey"] = DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE

    return context


# Khởi tạo giá trị theo config hiện tại ngay khi import module
refresh_account_context()


def _is_fast_2_mode(veo_model):
    return "fast 2.0" in str(veo_model or "").strip().lower()


def select_video_model_key(aspect_ratio, veo_model=None):
    use_relaxed = _TYPE_ACCOUNT == "ULTRA" and _is_fast_2_mode(veo_model)
    if aspect_ratio == VIDEO_ASPECT_RATIO_PORTRAIT:
        if use_relaxed:
            return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA_RELAXED
        return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT
    if use_relaxed:
        return DEFAULT_VIDEO_MODEL_KEY_ULTRA_RELAXED
    return DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE


def _select_model_key(aspect_ratio):
    return select_video_model_key(aspect_ratio)


def build_create_payload(
    prompt,
    session_id,
    project_id,
    recaptcha_token,
    seed=None,
    model_key=None,
    aspect_ratio=VIDEO_ASPECT_RATIO_LANDSCAPE,
    output_count=1,
):
    payload = json.loads(json.dumps(payload_text_to_video))
    payload["clientContext"]["recaptchaContext"]["token"] = recaptcha_token
    payload["clientContext"]["sessionId"] = session_id
    payload["clientContext"]["projectId"] = project_id

    request_item = payload["requests"][0]
    request_item["textInput"]["prompt"] = prompt
    request_item["metadata"]["sceneId"] = str(uuid.uuid4())
    if aspect_ratio:
        request_item["aspectRatio"] = aspect_ratio
    if seed is not None:
        request_item["seed"] = seed

    if model_key:
        request_item["videoModelKey"] = model_key
    else:
        request_item["videoModelKey"] = _select_model_key(request_item.get("aspectRatio"))

    count = output_count if isinstance(output_count, int) and output_count > 0 else 1
    payload["requests"] = [request_item.copy() for _ in range(count)]
    return payload


def _send_request_with_token(url, payload, token, method="POST", cookie=None):
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "url": url,
                "status": resp.status,
                "reason": resp.reason,
                "headers": dict(resp.headers.items()),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "url": url,
            "status": exc.code,
            "reason": exc.reason,
            "headers": dict(exc.headers.items()),
            "body": body,
        }
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "url": url,
            "error": str(exc),
        }


async def send_request_with_token(url, payload, token, method="POST", cookie=None):
    import asyncio

    return await asyncio.to_thread(_send_request_with_token, url, payload, token, method, cookie)


async def request_create_video(payload, token, cookie=None):
    return await send_request_with_token(URL_GENERATE_TEXT_TO_VIDEO, payload, token, method="POST", cookie=cookie)


async def request_check_status(payload, token, cookie=None):
    return await send_request_with_token(URL_STATUS_TEXT_TO_VIDEO, payload, token, method="POST", cookie=cookie)


async def request_create_video_via_browser(page, url, payload, access_token):
	"""Gửi request tạo video qua browser (Playwright) với access_token
	Dùng khi TOKEN_OPTION = "Option 2"
	"""
	import json
	try:
		headers = {
			"Content-Type": "application/json",
			"Authorization": f"Bearer {access_token}",
		}
		data = json.dumps(payload)
		
		# Gửi request qua browser bằng Playwright page.request API
		response = await page.request.post(
			url,
			data=data,
			headers=headers,
		)
		
		body = await response.text()
		return {
			"ok": response.ok,
			"url": url,
			"status": response.status,
			"reason": response.status_text,
			"headers": dict(response.headers),
			"body": body,
		}
	except Exception as exc:
		return {
			"ok": False,
			"url": url,
			"error": str(exc),
		}


def parse_operations_from_create_response(response_body):
    try:
        body_json = json.loads(response_body)
    except Exception:
        return []
    ops = body_json.get("operations", [])
    results = []
    for item in ops:
        op = item.get("operation", {}) or {}
        name = op.get("name")
        scene_id = item.get("sceneId")
        if name and scene_id:
            results.append(
                {
                    "operation": {"name": name},
                    "sceneId": scene_id,
                    "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                }
            )
    return results