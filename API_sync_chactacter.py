import json
import urllib.error
import urllib.request

from settings_manager import SettingsManager

URL_GENERATE_REFERENCE_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoReferenceImages"
URL_STATUS_REFERENCE_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"
URL_UPLOAD_IMAGE = "https://aisandbox-pa.googleapis.com/v1:uploadUserImage"

IMAGE_ASPECT_RATIO_LANDSCAPE = "IMAGE_ASPECT_RATIO_LANDSCAPE"
IMAGE_ASPECT_RATIO_PORTRAIT = "IMAGE_ASPECT_RATIO_PORTRAIT"

VIDEO_ASPECT_RATIO_LANDSCAPE = "VIDEO_ASPECT_RATIO_LANDSCAPE"
VIDEO_ASPECT_RATIO_PORTRAIT = "VIDEO_ASPECT_RATIO_PORTRAIT"

DEFAULT_SEED = 9797

MODEL_KEY_ULTRA_PORTRAIT = "veo_3_1_r2v_fast_portrait_ultra"
MODEL_KEY_ULTRA_PORTRAIT_RELAXED = "veo_3_1_r2v_fast_portrait_ultra_relaxed"
MODEL_KEY_NORMAL_PRO_PORTRAIT = "veo_3_1_r2v_fast_portrait"


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
    config = SettingsManager.load_config()
    account = config.get("account1", {}) if isinstance(config, dict) else {}

    custom_portrait = account.get("video_model_key_portrait") or account.get("VIDEO_MODEL_KEY_PORTRAIT")

    if account_type == "NORMAL":
        return {
            "type_account": "NORMAL",
            "video_model_key_portrait": custom_portrait or MODEL_KEY_NORMAL_PRO_PORTRAIT,
            "user_paygate_tier": "PAYGATE_TIER_NOT_PAID",
        }

    if account_type == "PRO":
        return {
            "type_account": "PRO",
            "video_model_key_portrait": custom_portrait or MODEL_KEY_NORMAL_PRO_PORTRAIT,
            "user_paygate_tier": "PAYGATE_TIER_ONE",
        }

    return {
        "type_account": "ULTRA",
        "video_model_key_portrait": custom_portrait or MODEL_KEY_ULTRA_PORTRAIT,
        "user_paygate_tier": "PAYGATE_TIER_TWO",
    }


_TYPE_ACCOUNT = "ULTRA"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = MODEL_KEY_ULTRA_PORTRAIT
USER_PAYGATE_TIER = "PAYGATE_TIER_TWO"


def refresh_account_context():
    global _TYPE_ACCOUNT, DEFAULT_VIDEO_MODEL_KEY_PORTRAIT, USER_PAYGATE_TIER

    context = _load_account_context()
    _TYPE_ACCOUNT = context["type_account"]
    DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = context["video_model_key_portrait"]
    USER_PAYGATE_TIER = context["user_paygate_tier"]
    return context


refresh_account_context()


def _is_fast_2_mode(veo_model):
    return "fast 2.0" in str(veo_model or "").strip().lower()


def select_video_model_key(aspect_ratio, veo_model=None):
    refresh_account_context()

    if _TYPE_ACCOUNT == "ULTRA":
        if _is_fast_2_mode(veo_model):
            return MODEL_KEY_ULTRA_PORTRAIT_RELAXED
        return MODEL_KEY_ULTRA_PORTRAIT

    return MODEL_KEY_NORMAL_PRO_PORTRAIT


def build_payload_upload_image(
    base64_image,
    mime_type,
    session_id,
    aspect_ratio=IMAGE_ASPECT_RATIO_PORTRAIT,
):
    return {
        "imageInput": {
            "rawImageBytes": base64_image,
            "mimeType": mime_type,
            "isUserUploaded": True,
            "aspectRatio": aspect_ratio,
        },
        "clientContext": {
            "sessionId": session_id,
            "tool": "ASSET_MANAGER",
        },
    }


def build_payload_generate_video_reference(
    token,
    session_id,
    project_id,
    prompt,
    seed,
    video_model_key,
    reference_media_ids,
    scene_id=None,
    aspect_ratio=VIDEO_ASPECT_RATIO_PORTRAIT,
    output_count=1,
):
    refs = []
    for media_id in list(reference_media_ids or [])[:3]:
        mid = str(media_id or "").strip()
        if not mid:
            continue
        refs.append(
            {
                "mediaId": mid,
                "imageUsageType": "IMAGE_USAGE_TYPE_ASSET",
            }
        )

    if not refs:
        raise ValueError("reference_media_ids is required")

    request_item = {
        "aspectRatio": aspect_ratio,
        "seed": int(seed or DEFAULT_SEED),
        "textInput": {
            "structuredPrompt": {
                "parts": [
                    {
                        "text": str(prompt or ""),
                    }
                ]
            }
        },
        "videoModelKey": str(video_model_key or "").strip(),
        "metadata": {},
        "referenceImages": refs,
    }

    if scene_id:
        request_item["metadata"]["sceneId"] = str(scene_id)

    count = int(output_count or 1)
    if count < 1:
        count = 1

    requests = [json.loads(json.dumps(request_item)) for _ in range(count)]

    return {
        "clientContext": {
            "projectId": str(project_id or ""),
            "tool": "PINHOLE",
            "userPaygateTier": USER_PAYGATE_TIER,
            "sessionId": str(session_id or ""),
            "recaptchaContext": {
                "token": str(token or ""),
                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
            },
        },
        "requests": requests,
    }


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


async def request_upload_image(payload, token, cookie=None):
    return await send_request_with_token(URL_UPLOAD_IMAGE, payload, token, method="POST", cookie=cookie)


async def request_create_video(payload, token, cookie=None):
    return await send_request_with_token(URL_GENERATE_REFERENCE_VIDEO, payload, token, method="POST", cookie=cookie)


async def request_check_status(payload, token, cookie=None):
    return await send_request_with_token(URL_STATUS_REFERENCE_VIDEO, payload, token, method="POST", cookie=cookie)


async def request_create_video_via_browser(page, payload, cookie, access_token):
    import json

    url = URL_GENERATE_REFERENCE_VIDEO
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }
        data = json.dumps(payload)

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
