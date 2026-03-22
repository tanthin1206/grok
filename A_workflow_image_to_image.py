import asyncio
import threading
import importlib
import json
import os
import re
import base64
import mimetypes
import shutil
import time
import uuid
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import requests

_qtcore = None
try:
	_qtcore = importlib.import_module("PySide6.QtCore")
except Exception:
	_qtcore = importlib.import_module("PyQt6.QtCore")

QThread = _qtcore.QThread
Signal = getattr(_qtcore, "Signal", None) or getattr(_qtcore, "pyqtSignal")

from settings_manager import SettingsManager, WORKFLOWS_DIR
from A_workflow_get_token import TokenCollector
from API_image_to_image import (
	build_generate_image_payload,
	build_generate_image_url,
	request_generate_images,
	request_generate_images_via_browser,
	build_payload_upload_image,
	request_upload_image,
	request_upload_image_via_browser,
	extract_media_id,
	parse_media_from_response,
	IMAGE_ASPECT_RATIO_LANDSCAPE,
	IMAGE_ASPECT_RATIO_PORTRAIT,
	refresh_account_context,
)
from workflow_run_control import get_running_video_count, get_max_in_flight


class GenerateImageWorkflow(QThread):
	"""Workflow tạo ảnh qua API flowMedia:batchGenerateImages."""

	log_message = Signal(str)
	video_updated = Signal(dict)
	automation_complete = Signal()

	def __init__(self, project_name=None, project_data=None, parent=None, prompt_ids_filter=None):
		super().__init__(parent)
		self.project_name = project_name or (project_data or {}).get("project_name", "Unknown")
		self.project_data = project_data or {}
		self._keep_chrome_open = bool(self.project_data.get("_keep_chrome_open"))
		self.STOP = 0
		self._token_timeouts = 0
		self._prompt_ids_filter = set(str(x) for x in prompt_ids_filter) if prompt_ids_filter else None
		self._preserve_existing_data = bool(self._prompt_ids_filter)
		self._in_flight_block_start_ts = 0
		self._image_mode = str(self.project_data.get("image_mode") or "prompt").strip().lower()
		self._active_prompt_ids = set()

	def run(self):
		try:
			running_loop = asyncio.get_running_loop()
		except RuntimeError:
			running_loop = None

		if running_loop and running_loop.is_running():
			self._log("⚠️  Đang có event loop chạy, tạo luồng mới cho Generate Image...")
			worker = threading.Thread(target=self._run_with_new_loop, daemon=True)
			worker.start()
			worker.join()
			return

		self._run_with_new_loop()

	def _run_with_new_loop(self):
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(self._run_workflow())
		except asyncio.CancelledError:
			self._log("🛑 Workflow bị cancel")
		except Exception as exc:
			self._log(f"❌ Lỗi workflow: {type(exc).__name__}: {exc}")
			self._log(traceback.format_exc()[:500])
		finally:
			try:
				loop.close()
			except Exception:
				pass
			self.automation_complete.emit()

	def _log(self, message):
		try:
			self.log_message.emit(message)
		except Exception:
			pass

	def stop(self):
		self.STOP = 1

	def _should_stop(self):
		return bool(self.STOP)

	async def _sleep_with_stop(self, seconds):
		end_ts = time.time() + float(seconds)
		while time.time() < end_ts:
			if self._should_stop():
				return False
			await asyncio.sleep(0.2)
		return True

	def _get_state_file_path(self):
		state_dir = WORKFLOWS_DIR / self.project_name
		state_dir.mkdir(parents=True, exist_ok=True)
		return state_dir / "state.json"

	def _load_state_json(self):
		state_file = self._get_state_file_path()
		if not state_file.exists():
			return {}
		try:
			with open(state_file, "r", encoding="utf-8") as f:
				return json.load(f)
		except Exception:
			return {}

	def _save_state_json(self, state_data):
		state_file = self._get_state_file_path()
		try:
			tmp_file = state_file.with_suffix(".json.tmp")
			with open(tmp_file, "w", encoding="utf-8") as f:
				json.dump(state_data, f, ensure_ascii=False, indent=2)
				f.flush()
				os.fsync(f.fileno())
			os.replace(tmp_file, state_file)
			return True
		except Exception:
			try:
				tmp_file.unlink(missing_ok=True)
			except Exception:
				pass
			return False

	def _count_in_progress_from_state(self):
		state_data = self._load_state_json()
		prompts = state_data.get("prompts", {}) if isinstance(state_data, dict) else {}
		count = 0
		running_markers = {"PENDING", "ACTIVE", "REQUESTED", "DOWNLOADING", "TOKEN", "QUEUED", "SUBMIT", "CREATING", "GENERATING", "RUNNING", "PROCESS", "PROGRESS", "STARTED"}
		active_prompt_ids = {str(pid).strip() for pid in (self._active_prompt_ids or set()) if str(pid).strip()}
		for prompt_key, prompt_data in prompts.items():
			if active_prompt_ids and str(prompt_key).strip() not in active_prompt_ids:
				continue
			statuses = prompt_data.get("statuses", []) if isinstance(prompt_data, dict) else []
			if any(any(marker in str(status or "").upper() for marker in running_markers) for status in statuses):
				count += 1
		return count

	def _count_in_progress(self):
		worker_count = get_running_video_count(default_value=-1)
		if int(worker_count) >= 0:
			return int(worker_count)
		return int(self._count_in_progress_from_state())

	def _resolve_worker_max_in_flight(self, fallback_value):
		return max(1, int(get_max_in_flight(default_value=int(fallback_value or 1))))

	def _ensure_prompt_entry(self, state_data, prompt_id, prompt_text):
		if "prompts" not in state_data:
			state_data["prompts"] = {}
		prompt_key = str(prompt_id)
		if prompt_key not in state_data["prompts"]:
			state_data["prompts"][prompt_key] = {
				"id": prompt_id,
				"prompt": prompt_text,
				"scene_ids": [],
				"statuses": [],
				"image_paths": [],
				"video_paths": [],
				"image_urls": [],
				"video_urls": [],
				"errors": [],
				"messages": [],
				"created_at": "",
			}
		return state_data["prompts"][prompt_key]

	def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, image_url="", image_path="", error="", message=""):
		state_data = self._load_state_json()
		prompt_data = self._ensure_prompt_entry(state_data, prompt_id, prompt_text)

		if "scene_id_map" not in state_data:
			state_data["scene_id_map"] = {}

		while len(prompt_data["scene_ids"]) <= idx:
			prompt_data["scene_ids"].append("")
		prompt_data["scene_ids"][idx] = scene_id
		state_data["scene_id_map"][scene_id] = prompt_id

		while len(prompt_data["statuses"]) <= idx:
			prompt_data["statuses"].append("PENDING")
		prompt_data["statuses"][idx] = status

		while len(prompt_data["image_paths"]) <= idx:
			prompt_data["image_paths"].append("")
		if image_path:
			prompt_data["image_paths"][idx] = image_path

		while len(prompt_data["image_urls"]) <= idx:
			prompt_data["image_urls"].append("")
		if image_url:
			prompt_data["image_urls"][idx] = image_url

		while len(prompt_data["errors"]) <= idx:
			prompt_data["errors"].append("")
		prompt_data["errors"][idx] = error if error else ""

		if "error_codes" not in prompt_data:
			prompt_data["error_codes"] = []
		while len(prompt_data["error_codes"]) <= idx:
			prompt_data["error_codes"].append("")
		prompt_data["error_codes"][idx] = error if error else ""

		while len(prompt_data["messages"]) <= idx:
			prompt_data["messages"].append("")
		prompt_data["messages"][idx] = message if message else ""

		if "error_messages" not in prompt_data:
			prompt_data["error_messages"] = []
		while len(prompt_data["error_messages"]) <= idx:
			prompt_data["error_messages"].append("")
		prompt_data["error_messages"][idx] = message if message else ""

		self._save_state_json(state_data)
		self._log(f"🧾 Update state: prompt {prompt_id} scene {scene_id[:8]} -> {status}")

	def _save_auth_to_state(self, access_token, session_id, project_id):
		state_data = self._load_state_json()
		state_data["auth"] = {
			"access_token": access_token,
			"sessionId": session_id,
			"projectId": project_id,
		}
		self._save_state_json(state_data)

	def _assign_scene_ids(self, payload, prompt_id, prompt_text):
		scene_ids = []
		requests = payload.get("requests", [])
		for idx, _ in enumerate(requests):
			scene_id = str(uuid.uuid4())
			scene_ids.append(scene_id)
			self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "PENDING")
		return scene_ids

	def _short_status(self, status):
		if not status:
			return "PENDING"
		if "PENDING" in status:
			return "PENDING"
		if "ACTIVE" in status:
			return "ACTIVE"
		if "SUCCESSFUL" in status:
			return "SUCCESSFUL"
		if "FAILED" in status:
			return "FAILED"
		return status

	def _prompt_key(self):
		if self._image_mode == "reference":
			return "create_image_reference"
		return "text_to_video"

	def _load_text_prompts(self):
		prompt_key = self._prompt_key()
		if self.project_data.get("_use_project_prompts"):
			items = self.project_data.get("prompts", {}).get(prompt_key, [])
			if not items and prompt_key != "text_to_video":
				items = self.project_data.get("prompts", {}).get("text_to_video", [])
			if self._prompt_ids_filter:
				items = [p for p in items if str(p.get("id")) in self._prompt_ids_filter]
			return items or []

		project_dir = WORKFLOWS_DIR / self.project_name
		test_file = project_dir / "test.json"
		if not test_file.exists():
			items = self.project_data.get("prompts", {}).get("text_to_video", [])
			if self._prompt_ids_filter:
				items = [p for p in items if str(p.get("id")) in self._prompt_ids_filter]
			return items or []
		try:
			with open(test_file, "r", encoding="utf-8") as f:
				data = json.load(f)
		except Exception:
			return []
		prompts_data = data.get("prompts", {}) if isinstance(data, dict) else {}
		text_prompts = prompts_data.get(prompt_key, []) if isinstance(prompts_data, dict) else []
		if (not text_prompts) and prompt_key != "text_to_video":
			text_prompts = prompts_data.get("text_to_video", []) if isinstance(prompts_data, dict) else []
		if self._prompt_ids_filter:
			text_prompts = [p for p in text_prompts if str(p.get("id")) in self._prompt_ids_filter]
		return text_prompts or []

	def _load_character_profiles(self):
		roots = []
		chars = self.project_data.get("characters")
		if isinstance(chars, list):
			roots.extend(chars)

		test_file = WORKFLOWS_DIR / self.project_name / "test.json"
		if test_file.exists():
			try:
				data = json.loads(test_file.read_text(encoding="utf-8"))
				test_chars = data.get("characters")
				if isinstance(test_chars, list):
					roots.extend(test_chars)
			except Exception:
				pass

		seen = set()
		profiles = []
		for item in roots:
			if not isinstance(item, dict):
				continue
			name = str(item.get("name") or item.get("character_name") or item.get("label") or "").strip()
			path = str(item.get("path") or item.get("image") or item.get("image_path") or "").strip()
			if not name or not path:
				continue
			key = name.lower()
			if key in seen:
				continue
			seen.add(key)
			profiles.append({"name": name, "name_key": key, "path": path})
		return profiles

	def _find_profiles_in_prompt(self, prompt_text, profiles):
		text = str(prompt_text or "")
		lowered = text.lower()
		hits = []
		for profile in profiles:
			name = str(profile.get("name") or "").strip()
			if not name:
				continue
			escaped = re.escape(name.lower())
			pattern = r"(?<![\w])" + escaped + r"(?![\w])"
			m = re.search(pattern, lowered, flags=re.IGNORECASE)
			if not m and name.lower() in lowered:
				pos = lowered.find(name.lower())
				if pos >= 0:
					hits.append((pos, profile))
				continue
			if m:
				hits.append((m.start(), profile))
		hits.sort(key=lambda x: x[0])
		return [h[1] for h in hits]

	def _build_prompt_reference_map(self, prompts, profiles):
		mapping = {}
		for idx, item in enumerate(prompts):
			if not isinstance(item, dict):
				continue
			prompt_id = str(item.get("id") or idx + 1)
			prompt_text = str(item.get("prompt") or item.get("description") or "")
			found = self._find_profiles_in_prompt(prompt_text, profiles)
			mapping[prompt_id] = found[:3]
		return mapping

	async def _upload_all_character_media(self, profiles, project_id, access_token, cookie, use_browser_upload=False, page=None):
		sem = asyncio.Semaphore(5)

		async def _upload_one(profile):
			async with sem:
				return await self._upload_profile_media(profile, project_id, access_token, cookie, use_browser_upload=use_browser_upload, page=page)

		tasks = [asyncio.create_task(_upload_one(p)) for p in profiles]
		results = await asyncio.gather(*tasks)

		media_cache = {}
		for ok, key, media_id, message in results:
			if ok and key and media_id:
				media_cache[key] = media_id
			else:
				self._log(f"❌ Upload ảnh tham chiếu lỗi: {message}")
		return media_cache

	async def _upload_profile_media(self, profile, project_id, access_token, cookie, *, use_browser_upload=False, page=None):
		key = str(profile.get("name_key") or "")
		path = str(profile.get("path") or "")
		name = str(profile.get("name") or key)
		if not key or not path:
			return False, key, "", f"{name}: thiếu dữ liệu"

		image_bytes, mime_type = self._read_image_bytes(path)
		if not image_bytes:
			return False, key, "", f"{name}: không đọc được ảnh"

		b64 = base64.b64encode(image_bytes).decode("utf-8")
		parsed = urlparse(path)
		filename = Path(parsed.path).name if parsed.scheme in {"http", "https"} else Path(path).name
		if not filename:
			filename = "reference.jpg"
		payload = build_payload_upload_image(
			b64,
			mime_type,
			project_id,
			file_name=filename,
		)
		try:
			if use_browser_upload and page is not None:
				response = await request_upload_image_via_browser(page, payload, access_token)
			else:
				response = await request_upload_image(payload, access_token, cookie=cookie)
		except Exception as exc:
			return False, key, "", f"{name}: upload exception {exc}"

		body = response.get("body", "")
		media_id = extract_media_id(body)
		if not response.get("ok", True) or not media_id:
			status = str(response.get("status") or "")
			reason = str(response.get("reason") or response.get("error") or "")
			preview = str(body or "")[:220].replace("\n", " ").strip()
			detail = f"status={status} reason={reason}".strip()
			if preview:
				detail = f"{detail} body={preview}".strip()
			return False, key, "", f"{name}: upload thất bại ({detail})"

		self._log(f"✅ Upload ảnh thành công: {name}")
		return True, key, str(media_id), ""

	def _read_image_bytes(self, image_link):
		parsed = urlparse(str(image_link or ""))
		is_url = parsed.scheme in {"http", "https"}
		try:
			if is_url:
				resp = requests.get(str(image_link), timeout=30)
				resp.raise_for_status()
				mime_type = resp.headers.get("Content-Type") or ""
				if ";" in mime_type:
					mime_type = mime_type.split(";", 1)[0].strip()
				if not mime_type:
					mime_type = self._guess_mime_type(str(image_link))
				return resp.content, mime_type

			path = Path(str(image_link))
			if not path.exists():
				return b"", ""
			return path.read_bytes(), self._guess_mime_type(str(path))
		except Exception:
			return b"", ""

	def _guess_mime_type(self, path_value):
		mime_type, _ = mimetypes.guess_type(path_value)
		return mime_type or "image/jpeg"

	def _load_auth_config(self):
		try:
			config = SettingsManager.load_config()
			account = config.get("account1", {}) if isinstance(config, dict) else {}
			session_id = account.get("sessionId")
			project_id = account.get("projectId")
			access_token = account.get("access_token")
			cookie = account.get("cookie")
			if not (session_id and project_id and access_token):
				return None
			return {
				"sessionId": session_id,
				"projectId": project_id,
				"access_token": access_token,
				"cookie": cookie,
				"URL_GEN_TOKEN": account.get("URL_GEN_TOKEN"),
				"folder_user_data_get_token": account.get("folder_user_data_get_token"),
			}
		except Exception:
			return None

	def _resolve_int_config(self, config, key, default_value):
		try:
			return int(config.get(key, default_value))
		except Exception:
			return default_value

	def _resolve_output_count(self, config):
		try:
			value = self.project_data.get("output_count") or config.get("output_count")
			return int(value)
		except Exception:
			return 1

	def _resolve_aspect_ratio(self, aspect_source=None):
		source = aspect_source or {}
		text = str(source.get("aspect_ratio") or self.project_data.get("aspect_ratio") or "").lower()
		if "9:16" in text or "dọc" in text or "doc" in text:
			return IMAGE_ASPECT_RATIO_PORTRAIT
		return IMAGE_ASPECT_RATIO_LANDSCAPE

	def _output_root_dir(self) -> Path:
		raw = str(self.project_data.get("video_output_dir") or "").strip()
		if not raw:
			raw = str(self.project_data.get("output_dir") or "").strip()
		if not raw:
			raw = str(WORKFLOWS_DIR / self.project_name / "Download")
		path = Path(raw)
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _image_output_dir(self) -> Path:
		path = self._output_root_dir() / "image"
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
		timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
		base_name = f"{prompt_idx}_{timestamp}"
		file_path = output_dir / f"{base_name}{suffix}"
		counter = 1
		while file_path.exists():
			file_path = output_dir / f"{base_name}_{counter}{suffix}"
			counter += 1
		return file_path

	def _download_image(self, url, prompt_idx):
		if not url:
			return ""
		url_text = str(url or "").strip()
		if not (url_text.startswith("http://") or url_text.startswith("https://")):
			self._log(f"⚠️ Không tải image: URL không hợp lệ ({url_text[:140]})")
			return ""
		image_dir = self._image_output_dir()
		image_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(image_dir, str(prompt_idx), ".jpg")
		try:
			with requests.get(url_text, stream=True, timeout=60) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 256):
						if chunk:
							f.write(chunk)
			return str(file_path.resolve())
		except requests.exceptions.RequestException as exc:
			self._log(f"⚠️ Không tải được image: {exc} | url={url_text[:140]}")
			return ""
		except Exception as exc:
			self._log(f"⚠️ Không tải được image: {exc} | url={url_text[:140]}")
			return ""

	def _clear_previous_data(self):
		project_dir = WORKFLOWS_DIR / self.project_name
		if not project_dir.exists():
			return
		keep_files = {"test.json", "status.json"}
		keep_dirs = {"Download", "thumbnails"}
		for item in project_dir.iterdir():
			if item.name in keep_files or item.name in keep_dirs:
				continue
			try:
				if item.is_dir():
					shutil.rmtree(item, ignore_errors=True)
				else:
					item.unlink(missing_ok=True)
			except Exception as e:
				self._log(f"⚠️ Không thể xóa {item.name}: {e}")

	def _save_request_json(self, payload, prompt_id, prompt_text):
		try:
			project_dir = WORKFLOWS_DIR / str(self.project_name)
			project_dir.mkdir(parents=True, exist_ok=True)
			request_file = project_dir / "request.json"
			request_data = {
				"timestamp": int(time.time()),
				"project_name": self.project_name,
				"flow": "generate_image",
				"prompt_id": prompt_id,
				"prompt_text": prompt_text,
				"request": payload,
			}
			entries = []
			if request_file.exists():
				try:
					raw_text = request_file.read_text(encoding="utf-8").strip()
					if raw_text:
						parsed = json.loads(raw_text)
						if isinstance(parsed, list):
							entries = parsed
						elif isinstance(parsed, dict):
							entries = [parsed]
				except Exception:
					pass
			entries.append(request_data)
			with open(request_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu request.json: {e}")

	def _save_response_json(self, response, prompt_id, prompt_text):
		try:
			project_dir = WORKFLOWS_DIR / str(self.project_name)
			project_dir.mkdir(parents=True, exist_ok=True)
			response_file = project_dir / "respone_anh.json"
			entry = {
				"timestamp": int(time.time()),
				"project_name": self.project_name,
				"flow": "generate_image",
				"prompt_id": prompt_id,
				"prompt_text": prompt_text,
				"ok": response.get("ok"),
				"status": response.get("status"),
				"reason": response.get("reason"),
				"error": response.get("error"),
				"body": response.get("body"),
			}
			entries = []
			if response_file.exists():
				try:
					raw_text = response_file.read_text(encoding="utf-8").strip()
					if raw_text:
						parsed = json.loads(raw_text)
						if isinstance(parsed, list):
							entries = parsed
						elif isinstance(parsed, dict):
							entries = [parsed]
				except Exception:
					pass
			entries.append(entry)
			with open(response_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu respone_anh.json: {e}")

	def _extract_error_info(self, response_body):
		try:
			body_json = json.loads(response_body)
		except Exception:
			return "", ""
		error = body_json.get("error") if isinstance(body_json, dict) else None
		if not isinstance(error, dict):
			return "", ""
		code = str(error.get("code", "")) if error.get("code") is not None else ""
		message = str(error.get("message", "")) if error.get("message") is not None else ""
		return code, message

	async def _init_token_collector(self, project_link, chrome_userdata_root, profile_name, clear_data_interval, idle_timeout, token_timeout):
		return TokenCollector(
			project_link,
			chrome_userdata_root=chrome_userdata_root,
			profile_name=profile_name,
			debug_port=9222,
			headless=False,
			token_timeout=token_timeout,
			idle_timeout=idle_timeout,
			log_callback=self._log,
			stop_check=self._should_stop,
			clear_data_interval=clear_data_interval,
			keep_chrome_open=self._keep_chrome_open,
			mode="generate_image",
		)

	async def _run_workflow(self):
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy workflow")
			return

		self._in_flight_block_start_ts = 0

		self._log(f"🚀 Bắt đầu workflow Tạo Ảnh cho project '{self.project_name}'")
		if self._preserve_existing_data:
			self._log("🧹 Bỏ qua xóa dữ liệu cũ (resend giữ lại state/ảnh/video hiện có)")
		else:
			self._clear_previous_data()

		prompts = self._load_text_prompts()
		if not prompts:
			self._log("❌ Không có prompts text_to_video trong test.json")
			return
		self._active_prompt_ids = {
			str((p or {}).get("id") or (idx + 1)).strip()
			for idx, p in enumerate(prompts)
			if str((p or {}).get("id") or (idx + 1)).strip()
		}
		if self._prompt_ids_filter:
			self._log(f"🧾 Đã nạp {len(prompts)} / {len(self._prompt_ids_filter)} prompt được chọn từ test.json")
		else:
			self._log(f"🧾 Đã nạp {len(prompts)} prompt từ test.json")

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		refresh_account_context()

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN") or "https://labs.google/fx/vi/tools/flow"
		chrome_userdata_root = auth.get("folder_user_data_get_token")
		profile_name = self.project_data.get("veo_profile") or SettingsManager.load_settings().get("current_profile")

		config = SettingsManager.load_config()
		output_count = self._resolve_output_count(config)
		wait_between = int(config.get("WAIT_GEN_IMAGE", config.get("WAIT_GEN_VIDEO", 15)))
		# Tăng thời gian chờ giữa các request theo số lượng ảnh cần tạo
		extra_wait = 0
		if output_count == 2:
			extra_wait = 15
		elif output_count == 3:
			extra_wait = 25
		elif output_count >= 4:
			extra_wait = 40
		wait_between_effective = wait_between + extra_wait
		max_token_retries = int(config.get("TOKEN_RETRY", 3))
		token_retry_delay = int(config.get("TOKEN_RETRY_DELAY", 2))
		retry_with_error = int(config.get("RETRY_WITH_ERROR", 3))
		wait_resend_image = int(config.get("WAIT_RESEND_IMAGE", 20))
		clear_data_token_image = int(config.get("CLEAR_DATA_TOKEN_IMAGE", config.get("CLEAR_DATA", 50)))
		clear_data_wait = int(config.get("CLEAR_DATA_WAIT", 2))
		response_timeout = int(config.get("IMAGE_RESPONSE_TIMEOUT", 80))
		get_token_timeout = 60
		max_in_flight = self._resolve_worker_max_in_flight(max(self._resolve_int_config(config, "MULTI_VIDEO", 3), 1))

		self._log(
			f"⚙️  Cấu hình: output_count={output_count}, "
			f"timeout_ảnh={response_timeout}s, token_timeout={get_token_timeout}s, wait_between={wait_between_effective}s, max_in_flight={max_in_flight}"
		)

		prompt_reference_names: dict[str, list[str]] = {}

		collector = await self._init_token_collector(
			project_link,
			chrome_userdata_root,
			profile_name,
			clear_data_wait,
			40,
			get_token_timeout,
		)

		token_option = str(SettingsManager.load_config().get("TOKEN_OPTION", "Option 2"))
		use_browser_upload = token_option == "Option 2"

		token_lock = asyncio.Lock()
		inflight_lock = asyncio.Lock()
		token_counter = {"count": 0}

		async with collector:
			if self._image_mode == "reference":
				profiles = self._load_character_profiles()
				if not profiles:
					self._log("❌ Không có ảnh tham chiếu hoặc tên ảnh tham chiếu")
					return

				prompt_profile_map = self._build_prompt_reference_map(prompts, profiles)
				used_profiles: dict[str, dict] = {}
				for _, profs in prompt_profile_map.items():
					for prof in profs:
						used_profiles[str(prof.get("name_key") or "")] = prof

				self._log(f"⬆️ Đang Upload ảnh tham chiếu {len(used_profiles)} ảnh (tối đa 5 luồng)")
				media_cache = await self._upload_all_character_media(
					list(used_profiles.values()),
					project_id,
					access_token,
					cookie,
					use_browser_upload=use_browser_upload,
					page=getattr(collector, "page", None),
				)

				for key, profile in used_profiles.items():
					if key not in media_cache:
						self._log(f"❌ Upload ảnh tham chiếu thất bại: {profile.get('name')}")
						return

				for prompt_id, profs in prompt_profile_map.items():
					ref_names = []
					for prof in profs:
						mid = media_cache.get(str(prof.get("name_key") or ""), "")
						if mid:
							ref_names.append(str(mid))
					prompt_reference_names[str(prompt_id)] = ref_names

			tasks = []
			for idx_prompt, prompt in enumerate(prompts):
				if self._should_stop():
					self._log("🛑 STOP trong vòng lặp prompt")
					break

				if idx_prompt > 0:
					if not await self._sleep_with_stop(wait_between_effective):
						break

				prompt_id = prompt.get("id", idx_prompt + 1)
				prompt_text = prompt.get("description", "") or prompt.get("prompt", "") or ""
				aspect_ratio = self._resolve_aspect_ratio(self.project_data)
				reference_input_names = prompt_reference_names.get(str(prompt_id), [])

				tasks.append(
					asyncio.create_task(
						self._process_prompt(
							collector,
							prompt_id,
							prompt_text,
							aspect_ratio,
							reference_input_names,
							self._image_mode == "reference",
							session_id,
							project_id,
							access_token,
							cookie,
							output_count,
							response_timeout,
							max_token_retries,
							token_retry_delay,
							max_in_flight,
							inflight_lock,
							token_lock,
							token_counter,
							clear_data_token_image,
							get_token_timeout,
							retry_with_error,
							wait_resend_image,
						)
					)
				)

			if tasks:
				try:
					await asyncio.gather(*tasks)
				except Exception:
					pass

		# Sau khi gửi hết prompt, chủ động đóng collector (Chrome + thread token)
		try:
			await collector.close_after_workflow()
		except Exception:
			pass

	async def _process_prompt(
		self,
		collector,
		prompt_id,
		prompt_text,
		aspect_ratio,
		reference_input_names,
		reference_required,
		session_id,
		project_id,
		access_token,
		cookie,
		output_count,
		response_timeout,
		max_token_retries,
		token_retry_delay,
		max_in_flight,
		inflight_lock,
		token_lock,
		token_counter,
		clear_data_token_image,
		get_token_timeout,
		retry_with_error,
		wait_resend_image,
	):
		if self._should_stop():
			return

		scene_ids = None
		last_error_msg = ""
		consecutive_403_count = 0
		clear_403_cooldown_until = 0.0
		token_timeout_streak = 0
		for retry_count in range(retry_with_error):
			try:
				if self._should_stop():
					return
				token = None
				for attempt in range(max_token_retries):
					if self._should_stop():
						return
					try:
						async with token_lock:
							token_counter["count"] += 1
							clear_storage = clear_data_token_image > 0 and (token_counter["count"] % clear_data_token_image == 0)
							token_timeout_for_call = max(get_token_timeout, 60) if clear_storage else get_token_timeout
							token = await asyncio.wait_for(
								collector.get_token(clear_storage=clear_storage, token_timeout_override=token_timeout_for_call),
								timeout=token_timeout_for_call,
							)
						if token:
							token_timeout_streak = 0
							break
					except asyncio.TimeoutError:
						self._log(f"⏱️ Timeout lấy token (prompt {prompt_id}, lần {attempt + 1})")
						token_timeout_streak += 1
						if token_timeout_streak >= 2:
							self._log("⚠️ Timeout lấy token liên tiếp, khởi động lại Chrome...")
							try:
								await collector.restart_browser()
							except Exception as e:
								self._log(f"⚠️ Restart Chrome lỗi: {e}")
							token_timeout_streak = 0
					except Exception as e:
						self._log(f"⚠️ Lỗi lấy token: {e}")
					if attempt < max_token_retries - 1:
						await asyncio.sleep(token_retry_delay)

				if not token:
					last_error_msg = "Không lấy được token recaptcha"
					self._log(f"❌ {last_error_msg} (prompt {prompt_id})")
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				if reference_required and not list(reference_input_names or []):
					last_error_msg = "Prompt không khớp tên ảnh tham chiếu"
					self._log(f"❌ {last_error_msg} (prompt {prompt_id})")
					for idx in range(max(1, int(output_count or 1))):
						scene_id = str(uuid.uuid4())
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="NO_REFERENCE", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": "NO_REFERENCE",
							"error_message": last_error_msg,
						})
					return

				wait_start_ts = time.time()
				payload = None
				while True:
					if self._should_stop():
						return
					async with inflight_lock:
						in_progress = self._count_in_progress()
						if in_progress < max_in_flight:
							payload = build_generate_image_payload(
								prompt_text,
								session_id,
								project_id,
								token,
								aspect_ratio=aspect_ratio,
								output_count=output_count,
								reference_input_names=reference_input_names,
							)
							if scene_ids is None:
								scene_ids = self._assign_scene_ids(payload, prompt_id, prompt_text)
							else:
								for idx, scene_id in enumerate(scene_ids or []):
									self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "PENDING")
							break
					elapsed = int(time.time() - wait_start_ts)
					self._log(f"⏳ Đang tạo ảnh đủ giới hạn {max_in_flight}, chờ {elapsed}s...")
					await asyncio.sleep(5)

				self._save_request_json(payload, prompt_id, prompt_text)

				self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Gửi request tạo ảnh (prompt {prompt_id}), retry {retry_count + 1}/{retry_with_error}...")
				send_started = time.time()

				# ✅ CHECK TOKEN_OPTION: Option 1 (urllib) hay Option 2 (browser via Playwright)
				token_option = str(SettingsManager.load_config().get("TOKEN_OPTION", "Option 2"))
				if token_option == "Option 2":
					self._log(f"🔧 Token Option: {token_option}")
					image_api_url = build_generate_image_url(project_id)
					browser_req_timeout_ms = max(30000, int(response_timeout * 1000))
					send_task = asyncio.create_task(request_generate_images_via_browser(
						collector.page,
						image_api_url,
						payload,
						access_token,
						timeout_ms=browser_req_timeout_ms,
					))
				else:
					send_task = asyncio.create_task(request_generate_images(payload, access_token, cookie=cookie, project_id=project_id))

				try:
					await asyncio.sleep(3)
					if not send_task.done():
						for idx, scene_id in enumerate(scene_ids or []):
							self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "ACTIVE")
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "ACTIVE",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
							})

					remaining = response_timeout - (time.time() - send_started)
					if remaining <= 0:
						raise asyncio.TimeoutError()
					response = await asyncio.wait_for(send_task, timeout=remaining)
					self._save_response_json(response, prompt_id, prompt_text)
				except asyncio.TimeoutError:
					last_error_msg = "Timeout chờ ảnh"
					self._log(f"⏱️ {response_timeout}s timeout tạo ảnh (prompt {prompt_id})")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="TIMEOUT", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": "TIMEOUT",
							"error_message": last_error_msg,
						})
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return
				except Exception as e:
					last_error_msg = str(e)
					self._log(f"❌ Lỗi gửi request tạo ảnh: {e}")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error="REQUEST", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": "REQUEST",
							"error_message": last_error_msg,
						})
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				response_body = response.get("body", "")
				error_code, error_message = self._extract_error_info(response_body)
				error_code_str = str(response.get("status") or error_code or "").strip()

				# ✅ Handle 403 error: 2 lần liên tiếp = clear storage, 3+ lần = restart chrome
				if not response.get("ok", True) and error_code_str in {"403", "3", "13", "53"}:
					if error_code_str == "403":
						consecutive_403_count += 1
					else:
						consecutive_403_count = 0

					msg = error_message or response.get("reason") or response.get("error") or "Unknown error"
					last_error_msg = msg
					self._log(f"⚠️ Lỗi {error_code_str} (prompt {prompt_id}): {msg}")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error=error_code_str, message=msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": error_code_str,
							"error_message": msg,
						})

					# 🔧 Lần 2 consecutive 403: clear storage (có cooldown để tránh clear liên tục)
					if error_code_str == "403" and consecutive_403_count == 2:
						now_ts = time.time()
						if now_ts < clear_403_cooldown_until:
							self._log("⚠️ Vừa clear storage gần đây, bỏ qua clear và restart Chrome...")
							await collector.restart_browser()
							consecutive_403_count = 0
							continue
						self._log(f"⚠️ Lỗi 403 lần {consecutive_403_count}, chạy clear storage...")
						try:
							await asyncio.wait_for(
								collector.get_token(clear_storage=True),
								timeout=60
							)
							consecutive_403_count = 0
							clear_403_cooldown_until = time.time() + 120
							try:
								token_counter["count"] = 0
							except Exception:
								pass
							self._log("✅ Clear storage xong, retry prompt")
						except Exception as e:
							self._log(f"⚠️ Clear storage lỗi: {e}")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue

					# 🔧 Lần 3+ consecutive 403: restart chrome
					if error_code_str == "403" and consecutive_403_count >= 3:
						self._log("⚠️ Lỗi 403 liên tiếp, khởi động lại Chrome...")
						try:
							await collector.restart_browser()
						except Exception as e:
							self._log(f"⚠️ Restart Chrome lỗi: {e}")
						consecutive_403_count = 0
						continue

					# Other retryable errors
					if retry_count < retry_with_error - 1:
						self._log(f"⚠️ Chờ {wait_resend_image}s rồi retry prompt {prompt_id} ({retry_count + 1}/{retry_with_error})")
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				if not response.get("ok", True) or error_message:
					code = str(response.get("status") or error_code or "")
					msg = error_message or response.get("reason") or response.get("error") or "Unknown error"
					last_error_msg = msg
					self._log(f"❌ API lỗi (prompt {prompt_id}): {msg}")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", error=code, message=msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_code": code,
							"error_message": msg,
						})
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				medias = parse_media_from_response(response_body)
				if not medias:
					last_error_msg = "Không nhận được ảnh"
					self._log(f"⚠️ API không trả về ảnh (prompt {prompt_id})")
					for idx, scene_id in enumerate(scene_ids or []):
						self._update_state_entry(prompt_id, prompt_text, scene_id, idx, "FAILED", message=last_error_msg)
						self.video_updated.emit({
							"prompt_idx": f"{prompt_id}_{idx + 1}",
							"status": "FAILED",
							"scene_id": scene_id,
							"prompt": prompt_text,
							"_prompt_id": prompt_id,
							"error_message": last_error_msg,
						})
					if retry_count < retry_with_error - 1:
						if not await self._sleep_with_stop(wait_resend_image):
							return
						continue
					return

				for idx, scene_id in enumerate(scene_ids or []):
					media = medias[idx] if idx < len(medias) else {}
					image_url = media.get("downloadUrl") or media.get("uri") or ""
					if not image_url:
						self._log(f"⚠️ Prompt {prompt_id} scene {idx + 1}: API không có downloadUrl/uri")
					image_path = self._download_image(image_url, f"{prompt_id}_{idx + 1}") if image_url else ""
					self._update_state_entry(
						prompt_id,
						prompt_text,
						scene_id,
						idx,
						"SUCCESSFUL",
						image_url=image_url,
						image_path=image_path,
					)
					self.video_updated.emit({
						"prompt_idx": f"{prompt_id}_{idx + 1}",
						"status": "SUCCESSFUL",
						"scene_id": scene_id,
						"prompt": prompt_text,
						"image_path": image_path,
						"_prompt_id": prompt_id,
					})

				self._save_auth_to_state(access_token, session_id, project_id)
				return
			finally:
				pass

		self._log(f"❌ Hết số lần retry ({retry_with_error}) cho prompt {prompt_id}: {last_error_msg}")


def start_generate_image(app, project_name, project_data, project_file, *, manage_buttons=True, prompt_ids_filter=None):
	"""Start image generation workflow from UI app context."""
	try:
		if hasattr(app, "add_log"):
			app.add_log(f"🚦 Bắt đầu Tạo Ảnh cho project '{project_name}'")
		app.workflow = GenerateImageWorkflow(
			project_name=project_name,
			project_data=project_data,
			prompt_ids_filter=prompt_ids_filter,
		)
		app.workflow.log_message.connect(app.add_log)
		app.workflow.video_updated.connect(app.on_video_updated)
		app.workflow.automation_complete.connect(app.on_automation_complete)
		app.workflow.start()

		if manage_buttons:
			if hasattr(app, "btn_run_all"):
				app.btn_run_all.setEnabled(False)
				app.btn_run_all.setStyleSheet(
					"background-color: #888888; color: #cccccc; border: 1px solid #666666; border-radius: 6px;"
				)

			if hasattr(app, "btn_start"):
				app.btn_start.setEnabled(False)
				app.btn_start.setStyleSheet(
					"background-color: #888888; color: #cccccc; border: 1px solid #666666; border-radius: 6px;"
				)

			if hasattr(app, "btn_stop"):
				app.btn_stop.setEnabled(True)
	except Exception as e:
		try:
			app.add_log(f"❌ Lỗi chạy Generate Image: {e}")
		except Exception:
			pass
		return False

	return True
