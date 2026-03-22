
import json
import urllib.error
import urllib.request

from settings_manager import SettingsManager

URL_IMGAE_TO_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartImage"
URL_IMAGE_TO_VIDEO_START_END = "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoStartAndEndImage"
URL_STATUS_IMAGE_TO_VIDEO = "https://aisandbox-pa.googleapis.com/v1/video:batchCheckAsyncVideoGenerationStatus"
URL_UPLOAD_IMAGE = "https://aisandbox-pa.googleapis.com/v1:uploadUserImage"

IMAGE_ASPECT_RATIO_LANDSCAPE = "IMAGE_ASPECT_RATIO_LANDSCAPE"
IMAGE_ASPECT_RATIO_PORTRAIT = "IMAGE_ASPECT_RATIO_PORTRAIT"

VIDEO_ASPECT_RATIO_LANDSCAPE = "VIDEO_ASPECT_RATIO_LANDSCAPE"
VIDEO_ASPECT_RATIO_PORTRAIT = "VIDEO_ASPECT_RATIO_PORTRAIT"

DEFAULT_SEED = 9797

# type_account ULTRA
DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_ULTRA = "veo_3_1_i2v_s_fast_ultra"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA = "veo_3_1_i2v_s_fast_portrait_ultra"
DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_ULTRA_RELAXED = "veo_3_1_i2v_s_fast_ultra_relaxed"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA_RELAXED = "veo_3_1_i2v_s_fast_portrait_ultra_relaxed"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_ULTRA = "veo_3_1_i2v_s_fast_portrait_fl_ultra"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_ULTRA_RELAXED = "veo_3_1_i2v_s_fast_portrait_fl_ultra_relaxed"

# type_account NORMAL
DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_NORMAL = "veo_3_1_i2v_s_fast"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL = "veo_3_1_i2v_s_fast_portrait"
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_NORMAL = "veo_3_1_i2v_s_fast_portrait_fl"


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

	# Cho phép override model key theo account config
	custom_landscape = account.get("video_model_key_landscape") or account.get("VIDEO_MODEL_KEY_LANDSCAPE")
	custom_portrait = account.get("video_model_key_portrait") or account.get("VIDEO_MODEL_KEY_PORTRAIT")

	if account_type == "NORMAL":
		return {
			"type_account": "NORMAL",
			"video_model_key_landscape": custom_landscape or DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_NORMAL,
			"video_model_key_portrait": custom_portrait or DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL,
			"user_paygate_tier": "PAYGATE_TIER_NOT_PAID",
		}

	if account_type == "PRO":
		return {
			"type_account": "PRO",
			"video_model_key_landscape": custom_landscape or DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_NORMAL,
			"video_model_key_portrait": custom_portrait or DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_NORMAL,
			"user_paygate_tier": "PAYGATE_TIER_ONE",
		}
	if account_type == "ULTRA":
		return {
		"type_account": "ULTRA",
		"video_model_key_landscape": custom_landscape or DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_ULTRA,
		"video_model_key_portrait": custom_portrait or DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA,
		"user_paygate_tier": "PAYGATE_TIER_TWO",
	}

# --- Xác định loại tài khoản và userPaygateTier (refresh khi import) ---
_TYPE_ACCOUNT = "ULTRA"
DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE = DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_ULTRA
DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA
USER_PAYGATE_TIER = "PAYGATE_TIER_TWO"


def refresh_account_context():
	global _TYPE_ACCOUNT, DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE, DEFAULT_VIDEO_MODEL_KEY_PORTRAIT, USER_PAYGATE_TIER

	context = _load_account_context()
	_TYPE_ACCOUNT = context["type_account"]
	DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE = context["video_model_key_landscape"]
	DEFAULT_VIDEO_MODEL_KEY_PORTRAIT = context["video_model_key_portrait"]
	USER_PAYGATE_TIER = context["user_paygate_tier"]
	return context


refresh_account_context()

def _is_fast_2_mode(veo_model):
	return "fast 2.0" in str(veo_model or "").strip().lower()


def select_video_model_key(aspect_ratio, veo_model=None, is_start_end=False):
	use_relaxed = _TYPE_ACCOUNT == "ULTRA" and _is_fast_2_mode(veo_model)
	if bool(is_start_end) and aspect_ratio == VIDEO_ASPECT_RATIO_PORTRAIT:
		if _TYPE_ACCOUNT == "ULTRA":
			if use_relaxed:
				return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_ULTRA_RELAXED
			return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_ULTRA
		return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_FL_NORMAL
	if aspect_ratio == VIDEO_ASPECT_RATIO_PORTRAIT:
		if use_relaxed:
			return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT_ULTRA_RELAXED
		return DEFAULT_VIDEO_MODEL_KEY_PORTRAIT
	if use_relaxed:
		return DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE_ULTRA_RELAXED
	return DEFAULT_VIDEO_MODEL_KEY_LANDSCAPE

def build_payload_upload_image(
	base64_image,
	mime_type,
	session_id,
	aspect_ratio=IMAGE_ASPECT_RATIO_LANDSCAPE,
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


def build_payload_generate_video_start_end(
	token,
	session_id,
	project_id,
	prompt,
	seed,
	video_model_key,
	start_media_id,
	scene_id,
	aspect_ratio=VIDEO_ASPECT_RATIO_LANDSCAPE,
	end_media_id=None,
	output_count=1,
):
	request_item = {
		"aspectRatio": aspect_ratio,
		"seed": seed,
		"videoModelKey": video_model_key,
		"startImage": {"mediaId": start_media_id},
		"metadata": {"sceneId": scene_id},
	}

	if end_media_id:
		request_item["endImage"] = {"mediaId": end_media_id}
		request_item["textInput"] = {
			"structuredPrompt": {
				"parts": [
					{
						"text": str(prompt or ""),
					}
				],
			},
		}
	else:
		request_item["textInput"] = {"prompt": prompt}

	count = output_count if isinstance(output_count, int) and output_count > 0 else 1
	requests = [request_item.copy() for _ in range(count)]

	return {
		"clientContext": {
			"recaptchaContext": {"token": token},
			"sessionId": session_id,
			"projectId": project_id,
			"tool": "PINHOLE",
			"userPaygateTier": USER_PAYGATE_TIER,
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


async def request_create_video(payload, token, cookie=None, url=None):
	target_url = str(url or URL_IMGAE_TO_VIDEO)
	return await send_request_with_token(target_url, payload, token, method="POST", cookie=cookie)


async def request_check_status(payload, token, cookie=None):
	return await send_request_with_token(URL_STATUS_IMAGE_TO_VIDEO, payload, token, method="POST", cookie=cookie)


async def request_create_video_via_browser(page, url, payload, cookie, access_token):
	"""Gửi request tạo video qua browser (Playwright) với cookie + access_token
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


