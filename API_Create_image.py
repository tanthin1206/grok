import json
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote

from settings_manager import SettingsManager


# Endpoint template: projectId must be injected per request
URL_GENERATE_IMAGES_TEMPLATE = "https://aisandbox-pa.googleapis.com/v1/projects/{project_id}/flowMedia:batchGenerateImages"

IMAGE_ASPECT_RATIO_LANDSCAPE = "IMAGE_ASPECT_RATIO_LANDSCAPE"
IMAGE_ASPECT_RATIO_PORTRAIT = "IMAGE_ASPECT_RATIO_PORTRAIT"
IMAGE_ASPECT_RATIO_SQUARE = "IMAGE_ASPECT_RATIO_SQUARE"

DEFAULT_SEED = 9797

CREATE_IMAGE_MODEL_TO_KEY = {
	"Nano Banana pro": "GEM_PIX_2",
	"Nano Banana 2": "NARWHAL",
	"Nano Banana": "GEM_PIX",
	"Imagen 4": "IMAGEN_3_5",
}


def _resolve_selected_create_image_model(config: dict | None) -> str:
	try:
		val = str((config or {}).get("CREATE_IMAGE_MODEL") or "").strip()
		if val in CREATE_IMAGE_MODEL_TO_KEY:
			return val
	except Exception:
		pass
	return "Imagen 4"


def resolve_seed_from_config():
	"""Lấy seed từ config: Random (0~294967295) hoặc Fixed (giá trị cố định)."""
	import random
	try:
		config = SettingsManager.load_config()
		seed_mode = str(config.get("SEED_MODE", "Random")).strip()
		if seed_mode == "Fixed":
			return int(config.get("SEED_VALUE", DEFAULT_SEED))
		return random.randint(0, 294967295)
	except Exception:
		return random.randint(0, 294967295)

# Account tier handling (more tiers can be added later)
PAYGATE_TIER_FOR_ACCOUNT = {
	"NORMAL": "PAYGATE_TIER_NOT_PAID",
	"PRO": "PAYGATE_TIER_ONE",
	"ULTRA": "PAYGATE_TIER_TWO",
}


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

	selected_model = _resolve_selected_create_image_model(config if isinstance(config, dict) else {})
	selected_key = CREATE_IMAGE_MODEL_TO_KEY.get(selected_model, "IMAGEN_3_5")
	custom_landscape = selected_key
	custom_portrait = selected_key
	user_paygate_tier = PAYGATE_TIER_FOR_ACCOUNT.get(account_type, "PAYGATE_TIER_TWO")

	return {
		"type_account": account_type,
		"image_model_key": custom_landscape,
		"image_model_key_portrait": custom_portrait,
		"user_paygate_tier": user_paygate_tier,
	}


_TYPE_ACCOUNT = "ULTRA"
DEFAULT_IMAGE_MODEL_KEY = "IMAGEN_3_5"
DEFAULT_IMAGE_MODEL_KEY_PORTRAIT = "IMAGEN_3_5"
USER_PAYGATE_TIER = PAYGATE_TIER_FOR_ACCOUNT.get(_TYPE_ACCOUNT, "PAYGATE_TIER_TWO")


def refresh_account_context():
	global _TYPE_ACCOUNT, DEFAULT_IMAGE_MODEL_KEY, DEFAULT_IMAGE_MODEL_KEY_PORTRAIT, USER_PAYGATE_TIER

	context = _load_account_context()
	_TYPE_ACCOUNT = context["type_account"]
	DEFAULT_IMAGE_MODEL_KEY = context["image_model_key"]
	DEFAULT_IMAGE_MODEL_KEY_PORTRAIT = context["image_model_key_portrait"]
	USER_PAYGATE_TIER = context["user_paygate_tier"]
	return context


payload_generate_image = {
	"clientContext": {
		"recaptchaContext": {
			"token": "",
			"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
		},
		"sessionId": "",
		"projectId": "",
		"tool": "PINHOLE",
	},
	"requests": [
		{
			"clientContext": {
				"recaptchaContext": {
					"token": "",
					"applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
				},
				"sessionId": "",
				"projectId": "",
				"tool": "PINHOLE",
			},
			"imageAspectRatio": IMAGE_ASPECT_RATIO_LANDSCAPE,
			"seed": DEFAULT_SEED,
			"imageModelName": "",
			"prompt": "",
			"imageInputs": [],
		}
	],
}


# Seed initial state from config when module is imported
refresh_account_context()


def _clone_payload_template():
	return json.loads(json.dumps(payload_generate_image))


def _resolve_image_model_key(aspect_ratio, override=None):
	base_default = "IMAGEN_3_5"
	if override:
		return override
	if aspect_ratio == IMAGE_ASPECT_RATIO_PORTRAIT:
		return DEFAULT_IMAGE_MODEL_KEY_PORTRAIT or DEFAULT_IMAGE_MODEL_KEY or base_default
	return DEFAULT_IMAGE_MODEL_KEY or DEFAULT_IMAGE_MODEL_KEY_PORTRAIT or base_default


def build_generate_image_payload(
	prompt,
	session_id,
	project_id,
	recaptcha_token,
	*,
	seed=None,
	model_key=None,
	aspect_ratio=IMAGE_ASPECT_RATIO_LANDSCAPE,
	output_count=1,
):
	payload = _clone_payload_template()
	payload["clientContext"]["recaptchaContext"]["token"] = recaptcha_token
	payload["clientContext"]["sessionId"] = session_id
	payload["clientContext"]["projectId"] = project_id

	request_item = payload["requests"][0]
	request_item["clientContext"] = json.loads(json.dumps(payload["clientContext"]))
	request_item["clientContext"]["recaptchaContext"]["token"] = recaptcha_token
	request_item["prompt"] = prompt
	if aspect_ratio:
		request_item["imageAspectRatio"] = aspect_ratio
	# Nếu caller không truyền seed, tự resolve từ config (Random/Fixed)
	effective_seed = seed if seed is not None else resolve_seed_from_config()
	request_item["seed"] = effective_seed

	resolved_model_key = _resolve_image_model_key(aspect_ratio, model_key)
	if resolved_model_key:
		request_item["imageModelName"] = resolved_model_key

	# Ensure imageInputs exists even when empty
	if "imageInputs" not in request_item or request_item["imageInputs"] is None:
		request_item["imageInputs"] = []

	count = output_count if isinstance(output_count, int) and output_count > 0 else 1
	# Deep-copy to ensure sceneId/metadata are isolated per request
	# Mỗi ảnh trong batch nhận seed khác nhau
	import random
	requests_list = []
	for i in range(count):
		copied = json.loads(json.dumps(request_item))
		if i > 0:
			if seed is not None:
				# Fixed seed: offset +i để khác nhau nhưng deterministic
				copied["seed"] = (effective_seed + i) % 294967296
			else:
				# Random: mỗi ảnh seed ngẫu nhiên riêng
				copied["seed"] = random.randint(0, 294967295)
		requests_list.append(copied)
	payload["requests"] = requests_list
	return payload


def build_generate_image_url(project_id):
	encoded_project = quote(str(project_id or ""), safe="")
	return URL_GENERATE_IMAGES_TEMPLATE.format(project_id=encoded_project)


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
		with urllib.request.urlopen(req, timeout=120) as resp:
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


def _resolve_project_id(payload, override_project_id=None):
	if override_project_id:
		return override_project_id
	try:
		ctx = (payload or {}).get("clientContext", {})
		if ctx.get("projectId"):
			return ctx["projectId"]
	except Exception:
		pass
	try:
		config = SettingsManager.load_config()
		account = config.get("account1", {}) if isinstance(config, dict) else {}
		return account.get("projectId")
	except Exception:
		return None


async def request_generate_images(payload, token, cookie=None, project_id=None):
	target_project_id = _resolve_project_id(payload, project_id)
	if not target_project_id:
		return {
			"ok": False,
			"error": "Missing projectId for image generation",
		}
	url = build_generate_image_url(target_project_id)
	return await send_request_with_token(url, payload, token, method="POST", cookie=cookie)


async def request_generate_images_via_browser(page, url, payload, access_token, timeout_ms=30000):
	"""Gửi request tạo ảnh qua browser (Playwright) với access_token.
	Dùng khi TOKEN_OPTION = "Option 2".
	Cookie và header được lấy từ trình duyệt, header luôn thêm access_token.
	"""
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
			timeout=int(timeout_ms or 30000),
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


def parse_media_from_response(response_body):
	try:
		body_json = json.loads(response_body)
	except Exception:
		return []

	medias = []

	def _collect(obj):
		if isinstance(obj, dict):
			url = obj.get("downloadUrl") or obj.get("uri") or obj.get("fifeUrl")
			if url:
				medias.append({
					"mediaId": obj.get("mediaId") or obj.get("mediaGenerationId") or obj.get("name"),
					"downloadUrl": url,
					"mimeType": obj.get("mimeType"),
				})
			for value in obj.values():
				_collect(value)
		elif isinstance(obj, list):
			for item in obj:
				_collect(item)

	_collect(body_json)
	return medias

