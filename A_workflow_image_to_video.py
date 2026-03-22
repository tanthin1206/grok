import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import mimetypes
import time
import threading
import uuid
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import requests

_qtcore = None
_qtwidgets = None
try:
	_qtcore = importlib.import_module("PySide6.QtCore")
	_qtwidgets = importlib.import_module("PySide6.QtWidgets")
except Exception:
	_qtcore = importlib.import_module("PyQt6.QtCore")
	_qtwidgets = importlib.import_module("PyQt6.QtWidgets")

QThread = _qtcore.QThread
Signal = getattr(_qtcore, "Signal", None) or getattr(_qtcore, "pyqtSignal")
QMessageBox = _qtwidgets.QMessageBox

import API_image_to_video as i2v_api

from settings_manager import SettingsManager, WORKFLOWS_DIR
from A_workflow_get_token import TokenCollector
from API_image_to_video import (
	build_payload_generate_video_start_end,
	build_payload_upload_image,
	request_check_status,
	request_create_video,
	request_create_video_via_browser,
	request_upload_image,
	DEFAULT_SEED,
	IMAGE_ASPECT_RATIO_LANDSCAPE,
	IMAGE_ASPECT_RATIO_PORTRAIT,
	VIDEO_ASPECT_RATIO_LANDSCAPE,
	VIDEO_ASPECT_RATIO_PORTRAIT,
	URL_IMGAE_TO_VIDEO,
	URL_IMAGE_TO_VIDEO_START_END,
)
from chrome_process_manager import ChromeProcessManager
from workflow_run_control import get_running_video_count, get_max_in_flight


# Toggle token Chrome window visibility for debug.
# True  -> move window off-screen
# False -> keep window on-screen (easier to debug)
TOKEN_CHROME_HIDE_WINDOW = True


class ImageToVideoWorkflow(QThread):
	"""Workflow Image to Video qua API (khong Playwright)."""

	log_message = Signal(str)
	video_updated = Signal(dict)
	automation_complete = Signal()
	project_link_updated = Signal(str)
	video_folder_updated = Signal(str)

	def __init__(self, project_name=None, project_data=None, parent=None):
		super().__init__(parent)
		self.project_name = project_name or (project_data or {}).get("project_name", "Unknown")
		self.project_data = project_data or {}
		self._auto_noi_canh = bool(self.project_data.get("_auto_noi_canh"))
		self._keep_chrome_open = bool(self.project_data.get("_keep_chrome_open")) or self._auto_noi_canh
		self.STOP = 0
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._last_submit_ts = 0
		self._status_log_ts = 0
		self._pending_log_interval = 3
		self._resend_items = None  # ✅ KHỞI TẠO RESEND ITEMS
		self._all_prompts_submitted = False  # ✅ Flag - tất cả prompts đã gửi xong
		self._complete_wait_timeout = 0
		self._complete_wait_start_ts = 0
		self._status_poll_fail_streak = 0
		self._last_status_change_ts = 0
		self._in_flight_block_start_ts = 0
		self._scene_next_check_at = {}
		self._scene_status_change_ts = {}
		self._upload_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="i2v-upload")
		self._active_prompt_ids = set()
		self._worker_controls_lifecycle = bool(self.project_data.get("_worker_controls_lifecycle", False))

	def run(self):
		# ✅ XÓA EVENT LOOP CŨ NẾU CÓ (fix "Cannot run the event loop while another loop is running")
		try:
			existing_loop = asyncio.get_event_loop()
			if existing_loop.is_running():
				self._log("⚠️  Found running event loop, closing it...")
				existing_loop.stop()
			existing_loop.close()
		except RuntimeError:
			pass  # No existing event loop
		
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			# ✅ KIỂM TRA RESEND_ITEMS - NẾU CÓ THÌ CHẠY RESEND, KHÔNG THÌ CHẠY WORKFLOW BÌNH THƯỜNG
			if hasattr(self, '_resend_items') and self._resend_items:
				self._log(f"🔄 Chạy resend workflow với {len(self._resend_items)} item(s)")
				loop.run_until_complete(self._run_resend_workflow(self._resend_items))
			else:
				self._log("📝 Chạy workflow Tạo Video Từ Ảnh")
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
			try:
				if self._upload_executor is not None:
					self._upload_executor.shutdown(wait=False)
					self._upload_executor = None
			except Exception:
				pass
			if self.STOP and not self._auto_noi_canh:
				try:
					ChromeProcessManager.close_chrome_gracefully(stop_check=self._should_stop)
				except Exception as e:
					self._log(f"⚠️  Lỗi Thoát luồng lấy token {e}")
			self.automation_complete.emit()

	def _log(self, message):
		try:
			self.log_message.emit(message)
		except Exception:
			pass

	def stop(self):
		if self.STOP:
			return
		self.STOP = 1
		try:
			self.requestInterruption()
		except Exception:
			pass
		self._log("🛑 Nhận lệnh dừng: set STOP=1")

		def _close_chrome_async():
			try:
				ChromeProcessManager.close_chrome_gracefully(stop_check=self._should_stop)
				self._log("✅ Đã gửi lệnh tắt Chrome")
			except Exception as e:
				self._log(f"⚠️ Lỗi tắt Chrome khi dừng: {e}")

		try:
			threading.Thread(target=_close_chrome_async, daemon=True).start()
		except Exception:
			pass

	def _save_request_json(self, payload, prompt_id, prompt_text, flow="image_to_video"):
		"""Lưu lịch sử request payload vào Workflows/{project}/request.json (dễ đọc, có format)."""
		try:
			project_dir = WORKFLOWS_DIR / str(self.project_name)
			project_dir.mkdir(parents=True, exist_ok=True)
			request_file = project_dir / "request.json"
			request_data = {
				"timestamp": int(time.time()),
				"project_name": self.project_name,
				"flow": flow,
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
					try:
						with open(request_file, "r", encoding="utf-8") as f:
							for line in f:
								line = line.strip()
								if not line:
									continue
								try:
									obj = json.loads(line)
									if isinstance(obj, dict):
										entries.append(obj)
								except Exception:
									pass
					except Exception:
						entries = []

			entries.append(request_data)
			with open(request_file, "w", encoding="utf-8") as f:
				json.dump(entries, f, ensure_ascii=False, indent=2)
		except Exception as e:
			self._log(f"⚠️ Không thể lưu request.json: {e}")

	def _should_stop(self):
		return bool(self.STOP)

	async def _sleep_with_stop(self, seconds):
		end_ts = time.time() + float(seconds)
		while time.time() < end_ts:
			if self._should_stop():
				return False
			await asyncio.sleep(0.2)
		return True

	async def _run_workflow(self):
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy workflow")
			return
		
		# ✅ RESET FLAG CHO WORKFLOW MỚI
		self._all_prompts_submitted = False
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._status_poll_fail_streak = 0
		self._last_status_change_ts = 0
		self._in_flight_block_start_ts = 0
		self._scene_next_check_at = {}
		self._scene_status_change_ts = {}
		
		# ✅ XÓA HếT Dữ LIẾU CŨ TRở KHÔNG PHẢI test.json
		self._cleanup_workflow_data()
		
		prompts = self._load_image_prompts()
		self._active_prompt_ids = {
			str((p or {}).get("id") or (idx + 1)).strip()
			for idx, p in enumerate(prompts or [])
			if str((p or {}).get("id") or (idx + 1)).strip()
		}
		start_end_mode = self._is_start_end_mode()
		create_video_url = URL_IMAGE_TO_VIDEO_START_END if start_end_mode else URL_IMGAE_TO_VIDEO
		if not prompts:
			self._log("❌ Không có dữ liệu prompts image_to_video để chạy")
			return

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		i2v_api.refresh_account_context()

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN")
		chrome_userdata_root = auth.get("folder_user_data_get_token")

		config = SettingsManager.load_config()
		wait_gen_video = int(config.get("WAIT_GEN_VIDEO", 25))
		output_count = self._resolve_output_count(config)
		max_token_retries = self._resolve_int_config(config, "TOKEN_RETRY", 3)
		token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
		retry_with_error = self._resolve_int_config(config, "RETRY_WITH_ERROR", 3)
		wait_resend = self._resolve_int_config(config, "WAIT_RESEND_VIDEO", 20)  # ✅ Wait time for 403 retry
		max_in_flight = self._resolve_worker_max_in_flight(self._resolve_int_config(config, "MULTI_VIDEO", 1))
		clear_data_every = self._resolve_int_config(config, "CLEAR_DATA", 1)
		clear_data_wait = self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2)
		base_token_timeout = self._resolve_int_config(config, "TOKEN_TIMEOUT", 60)
		token_timeout = max(60, base_token_timeout)
		get_token_timeout = 60
		self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)
		workflow_timeout = self._resolve_int_config(config, "WORKFLOW_TIMEOUT", 0)
		workflow_start_ts = time.time()

		profile_name = self.project_data.get("veo_profile")
		if not profile_name:
			profile_name = SettingsManager.load_settings().get("current_profile")

		if not project_link:
			project_link = self.project_data.get("project_link") or "https://labs.google/fx/vi/tools/flow"
		if not chrome_userdata_root:
			chrome_userdata_root = SettingsManager.create_chrome_userdata_folder(profile_name)

		upload_tasks = self._schedule_upload_tasks(
			prompts,
			session_id,
			access_token,
			cookie,
			max_parallel=5,
		)
		if upload_tasks:
			self._log(f"⬆️ Khởi động upload ảnh nền ({len(upload_tasks)} prompt, tối đa 5 luồng)...")


		status_task = asyncio.create_task(self._status_poll_loop(access_token, session_id, cookie))

		prompt_retry_counts = {}
		token_request_count = 0
		self._complete_wait_start_ts = 0
		
		# ✅ WRAP TOKENCOLLECTOR STARTUP TRONG TIMEOUT ĐỂ TRÁNH HANG
		token_startup_timeout = 30  # 30 seconds để Chrome startup + connect CDP
		
		try:
			# ✅ Khởi động TokenCollector với timeout
			async def _create_token_collector():
				return await asyncio.wait_for(
					self._init_token_collector(
						project_link,
						chrome_userdata_root,
						profile_name,
						clear_data_wait,
						40,
						token_timeout,
					),
					timeout=token_startup_timeout
				)
			
			try:
				collector = await _create_token_collector()
			except asyncio.TimeoutError:
				self._log(f"❌ TokenCollector startup timeout ({token_startup_timeout}s) - Chrome không connect được CDP port")
				self._log("❌ Workflow dừng do không thể lấy token")
				if status_task:
					status_task.cancel()
				return
			except Exception as e:
				self._log(f"❌ TokenCollector error: {e}")
				if status_task:
					status_task.cancel()
				return
			
			# ✅ TokenCollector đã startup thành công, chạy workflow
			async with collector:
				for idx_prompt, prompt in enumerate(prompts):
					if self.STOP:
						self._log("🛑 STOP trong vòng lặp prompt, dừng workflow")
						break
					if workflow_timeout and (time.time() - workflow_start_ts) >= workflow_timeout:
						self._log("⏳ Workflow timeout, dừng Image to Video")
						break

					try:
						prompt_id = prompt["id"]
						prompt_text = prompt.get("prompt", "")
						self._log(f"📝 Gửi prompt {prompt_id},(đầu vào {idx_prompt + 1}/{len(prompts)})")

						retry_count = prompt_retry_counts.get(prompt_id, 0)
						while True:
							if self.STOP:
								self._log("🛑 STOP trong prompt, dừng workflow")
								break
							wait_start_ts = time.time()
							while self._count_in_progress() >= max_in_flight:
								if self.STOP:
									self._log("🛑 STOP trong lúc chờ, dừng workflow")
									break
								if not self._check_in_flight_block():
									break
								elapsed = int(time.time() - wait_start_ts)
								self._log(f"⏳ Đang tạo video đủ giới hạn, chờ {elapsed}s...")
								if not await self._sleep_with_stop(5):
									break
							if self.STOP:
								break
							if self._count_in_progress() < max_in_flight:
								self._in_flight_block_start_ts = 0

							image_aspect, video_aspect, model_key = self._resolve_aspect_ratio_and_model()
							upload_task = upload_tasks.get(str(prompt_id))
							if upload_task is None:
								self._log(f"❌ Không tìm thấy task upload cho prompt {prompt_id}")
								fail_scene_ids = [str(uuid.uuid4()) for _ in range(output_count)]
								self._mark_prompt_failed(
									prompt_id,
									prompt_text,
									fail_scene_ids,
									"UPLOAD",
									"Upload task missing",
								)
								break

							if not upload_task.done():
								self._log(f"⏳ Prompt {prompt_id}: chờ upload ảnh hoàn tất...")
							upload_result = await upload_task
							if not upload_result.get("ok"):
								message = str(upload_result.get("message") or "Upload image failed")
								self._log(f"❌ Upload thất bại (prompt {prompt_id}): {message}")
								fail_scene_ids = [str(uuid.uuid4()) for _ in range(output_count)]
								self._mark_prompt_failed(
									prompt_id,
									prompt_text,
									fail_scene_ids,
									"UPLOAD",
									message,
								)
								break

							start_media_id = upload_result.get("start_media_id")
							end_media_id = upload_result.get("end_media_id") if start_end_mode else None

							token = None
							for attempt in range(max_token_retries):
								if self.STOP:
									self._log(f"🛑 STOP trong lấy token prompt {prompt_id}, dừng workflow")
									break
								try:
									# ✅ Timeout get_token() để tránh treo vô thời hạn
									token_request_count += 1
									clear_storage = (
										clear_data_every > 0
										and (token_request_count % clear_data_every == 0)
									)
									token = await asyncio.wait_for(
										collector.get_token(clear_storage=clear_storage),
										timeout=get_token_timeout,
									)
									if token:
										break
								except asyncio.TimeoutError:
									self._log(f"⏱️ Timeout lấy token (prompt {prompt_id}, lần {attempt + 1})")
								except Exception as e:
									self._log(f"⚠️ Lỗi lấy token: {e}")
								if attempt < max_token_retries - 1:
									if self.STOP:
										break
									await asyncio.sleep(token_retry_delay)
						
							# ✅ Kiểm tra token
							if not token:
								self._log(f"❌ Không lấy được token recaptcha (prompt {prompt_id})")
								break

							# ✅ CÓ TOKEN RỒI, KHỞI TẠO PAYLOAD
							payload = build_payload_generate_video_start_end(
								token,
								session_id,
								project_id,
								prompt_text,
								DEFAULT_SEED,
								model_key,
								start_media_id,
								"temp",
								aspect_ratio=video_aspect,
								end_media_id=end_media_id,
								output_count=output_count,
							)

							# ✅ GỬI REQUEST
							scene_ids = self._assign_scene_ids(payload, prompt_id, output_count)
							self._last_submit_ts = time.time()
							flow_name = "image_to_video_start_end" if start_end_mode else "image_to_video"
							self._save_request_json(payload, prompt_id, prompt_text, flow=flow_name)

							self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Bắt đầu gửi request tạo video (prompt {prompt_id})...")
							
							token_option = "Option 2"
							self._log(f"🔧 Token Option (forced): {token_option}")
							response = await request_create_video_via_browser(
								collector.page,
								create_video_url,
								payload,
								cookie,
								access_token
							)
							
							response_body = response.get("body", "")
							error_code, error_message = self._extract_error_info(response_body)
							retryable_errors = {"403", "3", "13", "53"}
							error_code_str = str(error_code or "").strip()
						
							# ✅ Handle 403 error: 2 lần = clear storage, 3+ lần = restart chrome
							if not response.get("ok", True) and error_code_str in retryable_errors:
								if self.STOP:
									self._log(f"🛑 STOP, không retry prompt {prompt_id}")
									break
								
								# Track consecutive 403
								if error_code_str == "403":
									consecutive_403_count = prompt_retry_counts.get(f"{prompt_id}_403_count", 0) + 1
									prompt_retry_counts[f"{prompt_id}_403_count"] = consecutive_403_count
								else:
									# Reset 403 count nếu error khác
									prompt_retry_counts[f"{prompt_id}_403_count"] = 0
									consecutive_403_count = 0
								
								retry_count += 1
								prompt_retry_counts[prompt_id] = retry_count
								self._discard_scene_ids(prompt_id, scene_ids)
								
								# 🔧 Lần 2 consecutive 403: clear storage
								if error_code_str == "403" and consecutive_403_count == 2:
									cooldown_key = f"{prompt_id}_403_clear_cooldown_until"
									cooldown_until = float(prompt_retry_counts.get(cooldown_key, 0) or 0)
									now_ts = time.time()
									if now_ts < cooldown_until:
										self._log("⚠️ Vừa clear storage gần đây, bỏ qua clear và restart Chrome...")
										await collector.restart_browser()
										prompt_retry_counts[f"{prompt_id}_403_count"] = 0
										retry_count = 0
										prompt_retry_counts[prompt_id] = 0
										continue
									self._log(f"⚠️ Lỗi 403 lần {consecutive_403_count}, chạy clear storage...")
									try:
										await asyncio.wait_for(
											collector.get_token(clear_storage=True),
											timeout=60
										)
										prompt_retry_counts[f"{prompt_id}_403_count"] = 0
										prompt_retry_counts[cooldown_key] = time.time() + 120
										token_request_count = 0
										retry_count = 0
										prompt_retry_counts[prompt_id] = 0
										self._log("✅ Clear storage xong, retry prompt")
									except Exception as e:
										self._log(f"⚠️ Clear storage lỗi: {e}")
									if not await self._sleep_with_stop(wait_resend):
										return
									continue
								
								# 🔧 Lần 3+ consecutive 403: restart chrome
								if error_code_str == "403" and consecutive_403_count >= 3:
									self._log("⚠️ Lỗi 403 liên tiếp, khởi động lại Chrome...")
									await collector.restart_browser()
									prompt_retry_counts[f"{prompt_id}_403_count"] = 0
									retry_count = 0
									prompt_retry_counts[prompt_id] = 0
									continue
								
								# Other retryable errors
								self._log(
									f"⚠️ Lỗi {error_code_str or 'UNKNOWN'}, chờ {wait_resend}s rồi retry prompt {prompt_id} ({retry_count}/{retry_with_error})"
								)
								if not await self._sleep_with_stop(wait_resend):
									return
								# ✅ Wait WAIT_RESEND_VIDEO seconds
								continue

							operations = self._parse_operations(response_body)
							
							# If API returned error (0 operations + error message), mark as FAILED and skip
							if not operations and error_message:
								self._log(f"❌ API lỗi, không tạo được video: {error_message[:100]}")
								for idx, scene_id in enumerate(scene_ids):
									self._update_state_entry(
										prompt_id,
										prompt_text,
										scene_id,
										idx,
										"FAILED",
										error=error_code,
										message=error_message,
									)
									self.video_updated.emit({
										"prompt_idx": f"{prompt_id}_{idx + 1}",
										"status": "FAILED",
										"scene_id": scene_id,
										"prompt": prompt_text,
										"_prompt_id": prompt_id,
										"error_code": error_code_str,
										"error_message": error_message,
									})
								self._discard_scene_ids(prompt_id, scene_ids)
								break
							
							self._log(f"📨 Đã gửi create video (prompt {prompt_id}), operations: {len(operations)}")
							self._handle_create_response(
								prompt_id,
								prompt_text,
								scene_ids,
								operations,
								access_token,
								session_id,
								project_id,
								response=response,
							)
							if not await self._sleep_with_stop(wait_gen_video):
								return
							break
					
					except Exception as e:
						self._log(f"❌ Lỗi xử lý prompt {prompt_id}: {e}")
						self._log(f"Traceback: {traceback.format_exc()}")
						continue
							
		except RuntimeError as exc:
			message = str(exc)
			if "URL GEN TOKEN" in message:
				self._log(message)
				try:
					QMessageBox.critical(None, "ERROR", message)
				except Exception:
					pass
				self.STOP = 1
				return

		# ✅ ĐẠT CỜ - HẾT TẤT CẢ PROMPTS, SẮP CHỜ HOÀN THÀNH
		if not self._auto_noi_canh:
			self._log("✅ Hết tất cả prompts, chờ video hoàn thành...")
		self._all_prompts_submitted = True
		self._complete_wait_start_ts = time.time()
		
		# ✅ LUÔN TẮT CHROME SAU KHI GỬI HẾT PROMPTS (trừ khi auto_noi_canh)
		if (not self._auto_noi_canh) and (not self._worker_controls_lifecycle):
			try:
				await collector.close_after_workflow()
				self._log("🔒 Đã đóng Chrome sau khi gửi hết prompts")
			except Exception:
				pass
		
		await self._wait_for_completion()
		if status_task:
			status_task.cancel()

	async def _run_resend_workflow(self, resend_items):
		"""Resend workflow cho video đã chọn - resend_items = [(prompt_id, prompt_text, scene_id, idx), ...]"""
		if self._should_stop():
			self._log("🛑 STOP trước khi chạy resend workflow")
			return

		# ✅ RESET FLAG CHO RESEND WORKFLOW MỚI
		self._all_prompts_submitted = False
		self._scene_status = {}
		self._scene_to_prompt = {}
		self._prompt_scene_order = {}
		self._active_prompt_ids = {str(item[0]).strip() for item in (resend_items or []) if str(item[0]).strip()}

		auth = self._load_auth_config()
		if not auth:
			self._log("❌ Thiếu sessionId/projectId/access_token trong config.json")
			return

		i2v_api.refresh_account_context()

		session_id = auth["sessionId"]
		project_id = auth["projectId"]
		access_token = auth["access_token"]
		cookie = auth.get("cookie")
		project_link = auth.get("URL_GEN_TOKEN")
		chrome_userdata_root = auth.get("folder_user_data_get_token")

		config = SettingsManager.load_config()
		start_end_mode = self._is_start_end_mode()
		create_video_url = URL_IMAGE_TO_VIDEO_START_END if start_end_mode else URL_IMGAE_TO_VIDEO
		wait_gen_video = int(config.get("WAIT_GEN_VIDEO", 25))
		output_count = self._resolve_output_count(config)
		max_token_retries = self._resolve_int_config(config, "TOKEN_RETRY", 3)
		token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
		retry_with_error = self._resolve_int_config(config, "RETRY_WITH_ERROR", 3)
		wait_resend = self._resolve_int_config(config, "WAIT_RESEND_VIDEO", 20)  # ✅ Wait time for 403 retry
		max_in_flight = self._resolve_worker_max_in_flight(self._resolve_int_config(config, "MULTI_VIDEO", 1))
		clear_data_every = self._resolve_int_config(config, "CLEAR_DATA", 1)
		clear_data_wait = self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2)
		base_token_timeout = self._resolve_int_config(config, "TOKEN_TIMEOUT", 60)
		token_timeout = max(60, base_token_timeout)
		get_token_timeout = 60
		self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)

		profile_name = self.project_data.get("veo_profile")
		if not profile_name:
			profile_name = SettingsManager.load_settings().get("current_profile")

		if not project_link:
			project_link = self.project_data.get("project_link") or "https://labs.google/fx/vi/tools/flow"
		if not chrome_userdata_root:
			chrome_userdata_root = SettingsManager.create_chrome_userdata_folder(profile_name)

		self._log(f"📝 Số video cần gen lại: {len(resend_items)}")

		status_task = asyncio.create_task(self._status_poll_loop(access_token, session_id, cookie))
		prompt_retry_counts = {}
		token_request_count = 0
		self._complete_wait_start_ts = 0

		# ✅ WRAP TOKENCOLLECTOR STARTUP TRONG TIMEOUT ĐỂ TRÁNH HANG
		token_startup_timeout = 30  # 30 seconds để Chrome startup + connect CDP
		
		try:
			# ✅ Khởi động TokenCollector với timeout
			try:
				collector = await asyncio.wait_for(
					self._init_token_collector(
						project_link,
						chrome_userdata_root,
						profile_name,
						clear_data_wait,
						40,
						token_timeout,
					),
					timeout=token_startup_timeout,
				)
			except asyncio.TimeoutError:
				self._log(f"❌ TokenCollector startup timeout ({token_startup_timeout}s) - Chrome không connect được CDP port")
				self._log("❌ Resend workflow dừng do không thể lấy token")
				if status_task:
					status_task.cancel()
				return
			except Exception as e:
				self._log(f"❌ TokenCollector error: {e}")
				if status_task:
					status_task.cancel()
				return
			
			# ✅ TokenCollector đã startup thành công, chạy workflow
			async with collector:
				for prompt_id, prompt_text, scene_id, idx in resend_items:
					if self.STOP:
						self._log("🛑 STOP trong resend workflow")
						break

					start_end_mode = self._is_start_end_mode()
					image_link = self._get_image_link(prompt_id)
					end_image_link = self._get_end_image_link(prompt_id)
					if not image_link:
						self._log(f"❌ Thiếu link ảnh cho prompt {prompt_id}")
						break
					if start_end_mode and not end_image_link:
						self._log(f"❌ Thiếu ảnh kết thúc cho prompt {prompt_id}")
						break

					# ✅ KIỂM TRA IN-FLIGHT LIMIT TRƯỚC KHI GỬI RESEND REQUEST
					wait_start_ts = time.time()
					while self._count_in_progress() >= max_in_flight:
						if self.STOP:
							self._log("🛑 STOP trong lúc chờ, dừng resend")
							break
						if not self._check_in_flight_block():
							break
						elapsed = int(time.time() - wait_start_ts)
						self._log(f"⏳ Đang tạo video đủ giới hạn {max_in_flight}, chờ {elapsed}s...")
						if not await self._sleep_with_stop(5):
							break
					if self.STOP:
						break
					if self._count_in_progress() < max_in_flight:
						self._in_flight_block_start_ts = 0

					retry_count = prompt_retry_counts.get(prompt_id, 0)

					for resend_attempt in range(retry_with_error):
						if self.STOP:
							self._log("🛑 STOP, dừng resend")
							break

						image_aspect, video_aspect, model_key = self._resolve_aspect_ratio_and_model()
						start_upload_task = self._upload_image_media_id_threaded(
							image_link,
							session_id,
							access_token,
							cookie,
							image_aspect,
							prompt_id,
							"ảnh bắt đầu",
						)

						if start_end_mode:
							end_upload_task = self._upload_image_media_id_threaded(
								end_image_link,
								session_id,
								access_token,
								cookie,
								image_aspect,
								prompt_id,
								"ảnh kết thúc",
							)
							start_result, end_result = await asyncio.gather(start_upload_task, end_upload_task)
						else:
							start_result = await start_upload_task
							end_result = {"ok": True, "media_id": ""}

						if not start_result.get("ok"):
							self._log(f"❌ Upload ảnh thất bại (prompt {prompt_id}): {start_result.get('message') or 'Upload image failed'}")
							break

						media_id = str(start_result.get("media_id") or "")
						if not media_id:
							self._log(f"❌ Upload ảnh thất bại (prompt {prompt_id}): thiếu media_id")
							break

						end_media_id = None
						if start_end_mode:
							if not end_result.get("ok"):
								self._log(f"❌ Upload ảnh kết thúc thất bại (prompt {prompt_id}): {end_result.get('message') or 'Upload end image failed'}")
								break
							end_media_id = str(end_result.get("media_id") or "")
							if not end_media_id:
								self._log(f"❌ Upload ảnh kết thúc thất bại (prompt {prompt_id}): thiếu media_id")
								break

						token = None

						for attempt in range(max_token_retries):
							if self.STOP:
								break
							try:
								# ✅ Timeout get_token() để tránh treo vô thời hạn
								token_request_count += 1
								clear_storage = (
									clear_data_every > 0
									and (token_request_count % clear_data_every == 0)
								)
								token = await asyncio.wait_for(
									collector.get_token(clear_storage=clear_storage),
									timeout=get_token_timeout
								)
								if token:
									break
							except asyncio.TimeoutError:
								self._log(f"⏱️ Timeout lấy token (lần {attempt + 1})")
							except Exception as e:
								self._log(f"⚠️ Lỗi lấy token: {e}")
							
							if attempt < max_token_retries - 1:
								if self.STOP:
									break
								await asyncio.sleep(token_retry_delay)
						
						# ✅ Kiểm tra token trước khi tiếp tục
						if not token:
							self._log(f"❌ Không lấy được token (prompt {prompt_id})")
							break
						
						payload = build_payload_generate_video_start_end(
							token,
							session_id,
							project_id,
							prompt_text,
							DEFAULT_SEED,
							model_key,
							media_id,
							scene_id,
							aspect_ratio=video_aspect,
							end_media_id=end_media_id,
							output_count=1,
						)
						flow_name = "image_to_video_start_end_resend" if start_end_mode else "image_to_video_resend"
						self._save_request_json(payload, prompt_id, prompt_text, flow=flow_name)

						self._log(f"🚀 [{time.strftime('%H:%M:%S')}] Gen lại request (prompt {prompt_id}, scene {scene_id[:8]})...")
						
						token_option = "Option 2"
						self._log(f"🔧 Token Option (forced): {token_option}")
						response = await request_create_video_via_browser(
							collector.page,
							create_video_url,
							payload,
							cookie,
							access_token
						)
						
						response_body = response.get("body", "")
						error_code, error_message = self._extract_error_info(response_body)
						retryable_errors = {"403", "3", "13", "53"}
						error_code_str = str(error_code or "").strip()

						# Handle 403 error with consecutive tracking: 2nd = clear storage, 3rd+ = restart chrome
						if not response.get("ok", True) and error_code_str in retryable_errors:
							if self.STOP:
								break
							retry_count += 1
							prompt_retry_counts[prompt_id] = retry_count
							
							# Track consecutive 403s using scene key
							scene_key = f"{prompt_id}_{scene_id}"
							if error_code_str == "403":
								consecutive_403_count = prompt_retry_counts.get(f"{scene_key}_403_count", 0) + 1
								prompt_retry_counts[f"{scene_key}_403_count"] = consecutive_403_count
							else:
								prompt_retry_counts[f"{scene_key}_403_count"] = 0
							
							# Handle consecutive 403s
							if error_code_str == "403":
								if consecutive_403_count == 2:
									cooldown_key = f"{scene_key}_403_clear_cooldown_until"
									cooldown_until = float(prompt_retry_counts.get(cooldown_key, 0) or 0)
									now_ts = time.time()
									if now_ts < cooldown_until:
										self._log("⚠️ Vừa clear storage gần đây, bỏ qua clear và restart Chrome...")
										await collector.restart_browser()
										prompt_retry_counts[f"{scene_key}_403_count"] = 0
										continue
									self._log("⚠️ Lỗi 403 lần 2 liên tiếp, clear storage...")
									try:
										await asyncio.wait_for(
											collector.get_token(clear_storage=True),
											timeout=60,
										)
									except Exception as e:
										self._log(f"⚠️ Clear storage lỗi: {e}")
									prompt_retry_counts[f"{scene_key}_403_count"] = 0
									prompt_retry_counts[cooldown_key] = time.time() + 120
									token_request_count = 0
								elif consecutive_403_count >= 3:
									self._log("⚠️ Lỗi 403 lần 3+ liên tiếp, khởi động lại Chrome...")
									await collector.restart_browser()
									prompt_retry_counts[f"{scene_key}_403_count"] = 0
							
							self._log(
								f"⚠️ Lỗi {error_code_str or 'UNKNOWN'}, chờ {wait_resend}s rồi retry ({retry_count}/{retry_with_error})"
							)
							if not await self._sleep_with_stop(wait_resend):
								return
							# ✅ Wait WAIT_RESEND_VIDEO seconds
							continue

						operations = self._parse_operations(response_body)

						# If API error, mark as FAILED and skip
						if not operations and error_message:
							self._log(f"❌ API lỗi: {error_message[:100]}")
							self._update_state_entry(
								prompt_id,
								prompt_text,
								scene_id,
								idx,
								"FAILED",
								error=error_code,
								message=error_message,
							)
							self.video_updated.emit({
								"prompt_idx": f"{prompt_id}_{idx + 1}",
								"status": "FAILED",
								"scene_id": scene_id,
								"prompt": prompt_text,
								"_prompt_id": prompt_id,
								"error_code": error_code_str,
								"error_message": error_message,
							})
							break

						self._log(f"📨 Resend create video (prompt {prompt_id}), operations: {len(operations)}")
						
						# Track for status polling
						self._scene_status[scene_id] = {
							"status": "MEDIA_GENERATION_STATUS_PENDING",
							"operation_name": "",
						}
						self._scene_to_prompt[scene_id] = {"prompt_id": prompt_id, "index": idx}

						# Update state with pending status
						for op in operations:
							op_scene_id = op.get("sceneId")
							if op_scene_id == scene_id:
								status = op.get("status") or "MEDIA_GENERATION_STATUS_PENDING"
								op_name = op.get("operation", {}).get("name", "")
								self._scene_status[scene_id]["operation_name"] = op_name
								self._update_state_entry(
									prompt_id,
									prompt_text,
									scene_id,
									idx,
									self._short_status(status),
								)
								self._last_status_change_ts = time.time()
								self._scene_next_check_at[scene_id] = time.time() + 6
								self._scene_status_change_ts[scene_id] = time.time()

						if not await self._sleep_with_stop(wait_gen_video):
							return
						break

		except RuntimeError as exc:
			message = str(exc)
			if "URL GEN TOKEN" in message:
				self._log(message)
				self.STOP = 1
				return

		# ✅ ĐẠT CỜ - HẾT TẤT CẢ ITEMS RESEND, SẮP CHỜ HOÀN THÀNH
		if not self._auto_noi_canh:
			self._log("✅ Hết tất cả items resend, chờ video hoàn thành...")
		self._all_prompts_submitted = True
		self._complete_wait_start_ts = time.time()
		
		# ✅ LUÔN TẮT CHROME SAU KHI GỬI HẾT PROMPTS (trừ khi auto_noi_canh)
		if (not self._auto_noi_canh) and (not self._worker_controls_lifecycle):
			try:
				await collector.close_after_workflow()
				self._log("🔒 Đã đóng Chrome sau khi gửi hết prompts")
			except Exception:
				pass
		
		await self._wait_for_completion()
		if status_task:
			status_task.cancel()

	def get_failed_scenes(self):
		"""Lấy danh sách video lỗi từ state.json - return [(prompt_id, prompt_text, scene_id, idx), ...]"""
		state_data = self._load_state_json()
		failed_items = []
		prompts = state_data.get("prompts", {})
		
		for prompt_key, prompt_data in prompts.items():
			if not isinstance(prompt_data, dict):
				continue
			prompt_id = prompt_data.get("id")
			prompt_text = prompt_data.get("prompt", "")
			statuses = prompt_data.get("statuses", [])
			scene_ids = prompt_data.get("scene_ids", [])

			for idx, status in enumerate(statuses):
				if status and status != "SUCCESSFUL":
					scene_id = scene_ids[idx] if idx < len(scene_ids) else ""
					if scene_id:
						failed_items.append((prompt_id, prompt_text, scene_id, idx))

		return failed_items

	def run_resend(self, resend_items):
		"""Start resend workflow với danh sách items được chọn"""
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(self._run_resend_workflow(resend_items))
		except Exception as exc:
			self.log_message.emit(f"❌ Lỗi resend workflow: {exc}")
			self.log_message.emit(traceback.format_exc()[:200])
		finally:
			try:
				loop.close()
			except Exception:
				pass
			if self.STOP:
				try:
					self._log("🛑 Stop, Thoát luồng lấy token...")
					ChromeProcessManager.close_chrome_gracefully(stop_check=self._should_stop)
				except Exception:
					pass
			self.automation_complete.emit()

	def _resolve_output_count(self, config):
		value = self.project_data.get("output_count")
		if value is None or value == "":
			value = config.get("MULTI_VIDEO", 1)
		try:
			count = int(value)
		except Exception:
			count = 1
		return max(1, count)

	def _resolve_int_config(self, config, key, default_value):
		value = config.get(key, default_value)
		try:
			return int(value)
		except Exception:
			return default_value

	def _output_root_dir(self) -> Path:
		raw = str(self.project_data.get("video_output_dir") or "").strip()
		if not raw:
			raw = str(self.project_data.get("output_dir") or "").strip()
		if not raw:
			raw = str(WORKFLOWS_DIR / self.project_name / "Download")
		path = Path(raw)
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _video_output_dir(self) -> Path:
		path = self._output_root_dir() / "video"
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _image_output_dir(self) -> Path:
		path = self._output_root_dir() / "image"
		path.mkdir(parents=True, exist_ok=True)
		return path

	def _upload_image_media_id_sync(self, image_link: str, session_id: str, access_token: str, cookie, aspect_ratio: str, prompt_id: str, stage_label: str) -> dict:
		image_bytes, mime_type = self._read_image_bytes(image_link)
		if not image_bytes:
			return {"ok": False, "error": "UPLOAD", "message": f"Cannot read {stage_label}"}

		base64_image = base64.b64encode(image_bytes).decode("utf-8")
		upload_payload = build_payload_upload_image(
			base64_image,
			mime_type,
			session_id,
			aspect_ratio=aspect_ratio,
		)

		self._log(f"⬆️  [Upload] prompt {prompt_id}: đang upload {stage_label}...")
		try:
			upload_response = asyncio.run(request_upload_image(upload_payload, access_token, cookie=cookie))
		except Exception as exc:
			return {"ok": False, "error": "UPLOAD", "message": f"Upload {stage_label} failed: {exc}"}

		upload_body = upload_response.get("body", "")
		media_id = self._extract_media_id(upload_body)
		if not upload_response.get("ok", True) or not media_id:
			status = upload_response.get("status")
			reason = upload_response.get("reason")
			self._log(f"❌ [Upload] prompt {prompt_id}: upload {stage_label} lỗi (status={status}, reason={reason})")
			return {"ok": False, "error": "UPLOAD", "message": f"Upload {stage_label} failed"}

		self._log(f"✅ [Upload] prompt {prompt_id}: upload {stage_label} xong")
		return {"ok": True, "media_id": str(media_id)}

	async def _upload_image_media_id_threaded(self, image_link: str, session_id: str, access_token: str, cookie, aspect_ratio: str, prompt_id: str, stage_label: str) -> dict:
		loop = asyncio.get_running_loop()
		executor = self._upload_executor
		if executor is None:
			executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="i2v-upload")
			self._upload_executor = executor
		return await loop.run_in_executor(
			executor,
			self._upload_image_media_id_sync,
			str(image_link or ""),
			str(session_id or ""),
			str(access_token or ""),
			cookie,
			str(aspect_ratio or ""),
			str(prompt_id or ""),
			str(stage_label or "image"),
		)

	async def _upload_prompt_media(self, prompt: dict, session_id: str, access_token: str, cookie, sem: asyncio.Semaphore) -> dict:
		prompt_id = str(prompt.get("id") or "")
		prompt_text = str(prompt.get("prompt") or "")
		start_image_link = str(prompt.get("start_image_link") or prompt.get("image_link") or prompt.get("image") or "").strip()
		end_image_link = str(prompt.get("end_image_link") or prompt.get("end_image") or "").strip()
		start_end_mode = self._is_start_end_mode()
		image_aspect, _, _ = self._resolve_aspect_ratio_and_model()

		async with sem:
			if self._should_stop():
				return {"ok": False, "error": "STOP", "message": "Stopped"}

			if not start_image_link:
				return {"ok": False, "error": "UPLOAD", "message": "Missing image link"}
			if start_end_mode and not end_image_link:
				return {"ok": False, "error": "UPLOAD", "message": "Missing end image link"}

			start_task = self._upload_image_media_id_threaded(
				start_image_link,
				session_id,
				access_token,
				cookie,
				image_aspect,
				prompt_id,
				"ảnh bắt đầu",
			)
			if start_end_mode:
				end_task = self._upload_image_media_id_threaded(
					end_image_link,
					session_id,
					access_token,
					cookie,
					image_aspect,
					prompt_id,
					"ảnh kết thúc",
				)
				start_result, end_result = await asyncio.gather(start_task, end_task)
			else:
				start_result = await start_task
				end_result = {"ok": True, "media_id": ""}

			if not start_result.get("ok"):
				return {"ok": False, "error": "UPLOAD", "message": str(start_result.get("message") or "Upload image failed")}

			start_media_id = str(start_result.get("media_id") or "")
			if not start_media_id:
				return {"ok": False, "error": "UPLOAD", "message": "Upload image failed"}

			end_media_id = None
			if start_end_mode:
				if not end_result.get("ok"):
					return {"ok": False, "error": "UPLOAD", "message": str(end_result.get("message") or "Upload end image failed")}
				end_media_id = str(end_result.get("media_id") or "")
				if not end_media_id:
					return {"ok": False, "error": "UPLOAD", "message": "Upload end image failed"}

			return {
				"ok": True,
				"prompt_id": prompt_id,
				"prompt_text": prompt_text,
				"start_media_id": start_media_id,
				"end_media_id": end_media_id,
				"image_path": start_image_link,
			}

	def _schedule_upload_tasks(self, prompts: list[dict], session_id: str, access_token: str, cookie, max_parallel: int = 5) -> dict:
		limit = int(max_parallel or 1)
		if limit < 1:
			limit = 1
		sem = asyncio.Semaphore(limit)
		tasks: dict[str, asyncio.Task] = {}
		for prompt in prompts or []:
			prompt_id = str((prompt or {}).get("id") or "").strip()
			if not prompt_id:
				continue
			tasks[prompt_id] = asyncio.create_task(
				self._upload_prompt_media(prompt, session_id, access_token, cookie, sem)
			)
		return tasks

	def _i2v_mode(self):
		mode = str(self.project_data.get("i2v_mode") or self.project_data.get("image_mode") or "single").strip().lower()
		return "start_end" if mode in {"start_end", "start-end", "startend"} else "single"

	def _is_start_end_mode(self):
		return self._i2v_mode() == "start_end"

	def _prompt_bucket_key(self):
		return "image_to_video_start_end" if self._is_start_end_mode() else "image_to_video"

	def _load_image_prompts(self):
		prompt_key = self._prompt_bucket_key()
		if self.project_data.get("_use_project_prompts"):
			items = self.project_data.get("prompts", {}).get(prompt_key, [])
			if not items and prompt_key != "image_to_video":
				items = self.project_data.get("prompts", {}).get("image_to_video", [])
			return self._build_image_prompt_list(items)

		test_file = WORKFLOWS_DIR / self.project_name / "test.json"
		if test_file.exists():
			try:
				with open(test_file, "r", encoding="utf-8") as f:
					data = json.load(f)
				items = data.get("prompts", {}).get(prompt_key, [])
				if not items and prompt_key != "image_to_video":
					items = data.get("prompts", {}).get("image_to_video", [])
			except Exception:
				items = []
		else:
			items = self.project_data.get("prompts", {}).get(prompt_key, [])
			if not items and prompt_key != "image_to_video":
				items = self.project_data.get("prompts", {}).get("image_to_video", [])

		return self._build_image_prompt_list(items)

	def _build_image_prompt_list(self, items):
		prompts_list = []
		start_end_mode = self._is_start_end_mode()

		for item in items:
			prompt_id = item.get("id")
			if not prompt_id:
				continue
			prompt_text = item.get("description") or item.get("prompt") or ""
			if not str(prompt_text).strip():
				prompt_text = self.project_data.get("idea") or "Sinh video từ ảnh"

			if start_end_mode:
				start_image_link = item.get("start_image_link") or item.get("image_link") or item.get("image") or ""
				end_image_link = item.get("end_image_link") or item.get("end_image") or ""
				if start_image_link or end_image_link:
					prompts_list.append(
						{
							"id": prompt_id,
							"prompt": prompt_text,
							"image_link": start_image_link,
							"start_image_link": start_image_link,
							"end_image_link": end_image_link,
						}
					)
				continue

			image_link = item.get("image_link") or item.get("image") or item.get("start_image_link") or ""
			if image_link:
				prompts_list.append(
					{"id": prompt_id, "prompt": prompt_text, "image_link": image_link}
				)
		return prompts_list

	def _load_auth_config(self):
		config = SettingsManager.load_config()
		account = config.get("account1", {}) if isinstance(config, dict) else {}
		session_id = account.get("sessionId")
		project_id = account.get("projectId")
		access_token = account.get("access_token")
		cookie = account.get("cookie")
		# ✅ Lấy URL_GEN_TOKEN và folder_user_data từ account1
		url_gen_token = account.get("URL_GEN_TOKEN")
		folder_user_data = account.get("folder_user_data_get_token")
		if not (session_id and project_id and access_token):
			return None
		return {
			"sessionId": session_id,
			"projectId": project_id,
			"access_token": access_token,
			"cookie": cookie,
			"URL_GEN_TOKEN": url_gen_token,
			"folder_user_data_get_token": folder_user_data,
		}

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

	def _cleanup_workflow_data(self):
		"""✅ Xóa tất cả dữ liệu cũ trong folder Workflows/{project} NGOẠI TRỪ test.json"""
		try:
			self._save_state_json({})
			if self.project_data.get("_skip_clear_download"):
				self._log("🧹 Bỏ qua xóa dữ liệu (auto_noi_canh), giữ file cũ")
				return

			project_dir = WORKFLOWS_DIR / self.project_name
			if not project_dir.exists():
				return
			
			self._log("🧹 Dọn dữ liệu cũ (giữ test.json, status.json, Download)...")
			keep_files = {"test.json", "status.json"}
			keep_dirs = {"Download", "thumbnails"}
			
			# Duyệt tất cả files/folders trong project_dir
			for item in project_dir.iterdir():
				if item.name in keep_files or item.name in keep_dirs:
					continue
				
				# Xóa files
				if item.is_file():
					try:
						item.unlink()
						self._log(f"  ✓ Xóa file: {item.name}")
					except Exception as e:
						self._log(f"  ⚠️ Không xóa được {item.name}: {e}")
				
				# Xóa folders
				elif item.is_dir():
					try:
						import shutil
						shutil.rmtree(item)
						self._log(f"  ✓ Xóa folder: {item.name}")
					except Exception as e:
						self._log(f"  ⚠️ Không xóa được folder {item.name}: {e}")
			
			self._log("✅ Dữ liệu cũ đã dọn, giữ nguyên file media cũ")
		except Exception as e:
			self._log(f"⚠️ Lỗi cleanup data: {e}")

	def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
		timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
		base_name = f"{prompt_idx}_{timestamp}"
		file_path = output_dir / f"{base_name}{suffix}"
		counter = 1
		while file_path.exists():
			file_path = output_dir / f"{base_name}_{counter}{suffix}"
			counter += 1
		return file_path

	def _count_in_progress_from_state(self):
		state_data = self._load_state_json()
		prompts = state_data.get("prompts", {}) if isinstance(state_data, dict) else {}
		count = 0
		running_markers = {"PENDING", "ACTIVE", "REQUESTED", "DOWNLOADING", "TOKEN", "QUEUED", "SUBMIT", "CREATING", "GENERATING"}
		active_prompt_ids = {str(pid).strip() for pid in (self._active_prompt_ids or set()) if str(pid).strip()}
		for prompt_key, prompt_data in prompts.items():
			if active_prompt_ids and str(prompt_key).strip() not in active_prompt_ids:
				continue
			statuses = prompt_data.get("statuses", []) if isinstance(prompt_data, dict) else []
			if any(any(marker in str(status or "").upper() for marker in running_markers) for status in statuses):
				count += 1
		return count

	def _count_in_progress(self):
		state_count = int(self._count_in_progress_from_state())
		worker_count = get_running_video_count(default_value=-1)
		if int(worker_count) >= 0:
			return max(state_count, int(worker_count))
		return state_count

	def _resolve_worker_max_in_flight(self, fallback_value):
		return max(1, int(get_max_in_flight(default_value=int(fallback_value or 1))))

	def _save_state_json(self, state_data):
		state_file = self._get_state_file_path()
		try:
			with open(state_file, "w", encoding="utf-8") as f:
				json.dump(state_data, f, ensure_ascii=False, indent=2)
			return True
		except Exception:
			return False

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

	def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, video_url="", image_url="", video_path="", image_path="", error="", message=""):
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

		while len(prompt_data["video_paths"]) <= idx:
			prompt_data["video_paths"].append("")
		if video_path:
			prompt_data["video_paths"][idx] = video_path

		while len(prompt_data["image_paths"]) <= idx:
			prompt_data["image_paths"].append("")
		if image_path:
			prompt_data["image_paths"][idx] = image_path

		while len(prompt_data["video_urls"]) <= idx:
			prompt_data["video_urls"].append("")
		if video_url:
			prompt_data["video_urls"][idx] = video_url

		while len(prompt_data["image_urls"]) <= idx:
			prompt_data["image_urls"].append("")
		if image_url:
			prompt_data["image_urls"][idx] = image_url

		while len(prompt_data["errors"]) <= idx:
			prompt_data["errors"].append("")
		# ✅ LUÔN CẬP NHẬT ERROR - nếu không có error mới thì clear lại thành ""
		prompt_data["errors"][idx] = error if error else ""

		# ✅ Đồng bộ thêm error_codes/error_messages để UI cũ đọc được
		if "error_codes" not in prompt_data:
			prompt_data["error_codes"] = []
		while len(prompt_data["error_codes"]) <= idx:
			prompt_data["error_codes"].append("")
		prompt_data["error_codes"][idx] = error if error else ""

		while len(prompt_data["messages"]) <= idx:
			prompt_data["messages"].append("")
		# ✅ LUÔN CẬP NHẬT MESSAGE - nếu không có message mới thì clear lại thành ""
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

	def _assign_scene_ids(self, payload, prompt_id, output_count):
		scene_ids = []
		requests = payload.get("requests", [])
		for idx, req in enumerate(requests):
			scene_id = str(uuid.uuid4())
			req.setdefault("metadata", {})["sceneId"] = scene_id
			scene_ids.append(scene_id)
		self._prompt_scene_order[prompt_id] = scene_ids
		for idx, scene_id in enumerate(scene_ids):
			self._scene_to_prompt[scene_id] = {"prompt_id": prompt_id, "index": idx}
			self._scene_status[scene_id] = {
				"status": "MEDIA_GENERATION_STATUS_PENDING",
				"operation_name": "",
			}
			self._scene_next_check_at[scene_id] = time.time() + 999999
		return scene_ids

	def _discard_scene_ids(self, prompt_id, scene_ids):
		for scene_id in scene_ids:
			self._scene_status.pop(scene_id, None)
			self._scene_to_prompt.pop(scene_id, None)
		if prompt_id in self._prompt_scene_order:
			self._prompt_scene_order[prompt_id] = [
				sid for sid in self._prompt_scene_order[prompt_id] if sid not in scene_ids
			]

	def _parse_operations(self, response_body):
		try:
			body_json = json.loads(response_body)
			return body_json.get("operations", [])
		except Exception:
			return []

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

	def _handle_create_response(
		self,
		prompt_id,
		prompt_text,
		scene_ids,
		operations,
		access_token,
		session_id,
		project_id,
		response=None,
	):
		op_map = {}
		for op in operations:
			scene_id = op.get("sceneId")
			if scene_id:
				op_map[scene_id] = op

		error_code = ""
		error_message = ""
		if response and not response.get("ok", True):
			response_body = response.get("body", "")
			error_code, error_message = self._extract_error_info(response_body)
			if error_message:
				self._log(f"❌ Create API lỗi: {error_message}")

		self._save_auth_to_state(access_token, session_id, project_id)

		for idx, scene_id in enumerate(scene_ids):
			op = op_map.get(scene_id)
			if op is None and idx < len(operations):
				op = operations[idx]
			if not isinstance(op, dict):
				op = {}
			status = self._normalize_status_full(op.get("status") or "MEDIA_GENERATION_STATUS_PENDING")
			operation_name = op.get("operation", {}).get("name", "")
			self._scene_status[scene_id]["status"] = status
			self._scene_status[scene_id]["operation_name"] = operation_name
			self._last_status_change_ts = time.time()
			self._scene_next_check_at[scene_id] = time.time() + 6
			self._scene_status_change_ts[scene_id] = time.time()

			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				self._short_status(status),
				error=error_code,
				message=error_message,
			)

			prompt_idx = f"{prompt_id}_{idx + 1}"
			self.video_updated.emit({
				"prompt_idx": prompt_idx,
				"status": self._short_status(status),
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	async def _status_poll_loop(self, access_token, session_id, cookie=None):
		while not self.STOP:
			pending = [
				sid for sid, info in self._scene_status.items()
				if self._is_running_status(info.get("status"))
			]
			if not pending:
				await asyncio.sleep(1)
				continue

			eligible = [
				sid for sid in pending
				if self._scene_next_check_at.get(sid, 0) <= time.time()
			]
			if not eligible:
				await asyncio.sleep(1)
				continue

			now = time.time()
			if (now - self._status_log_ts) >= self._pending_log_interval:
				self._status_log_ts = now
				prompt_ids = []
				for sid in pending:
					prompt_info = self._scene_to_prompt.get(sid) or {}
					prompt_id = prompt_info.get("prompt_id")
					if prompt_id is not None:
						prompt_ids.append(prompt_id)

			operations_payload = []
			for scene_id in eligible:
				info = self._scene_status.get(scene_id, {})
				op_name = info.get("operation_name")
				op_block = {"sceneId": scene_id, "status": info.get("status", "")}
				if op_name:
					op_block["operation"] = {"name": op_name}
				operations_payload.append(op_block)

			payload = {"operations": operations_payload}
			try:
				response = await request_check_status(payload, access_token, cookie=cookie)
			except Exception as exc:
				self._status_poll_fail_streak += 1
				self._log(
					f"⚠️ Lỗi check status (lần {self._status_poll_fail_streak}/4): {exc}"
				)
				await asyncio.sleep(5)
				continue

			if not response.get("ok", True):
				status_code = response.get("status")
				reason = response.get("reason")
				self._status_poll_fail_streak += 1
				self._log(
					"⚠️ Check status thất bại "
					f"(lần {self._status_poll_fail_streak}/4, status={status_code}, reason={reason})"
				)
				await asyncio.sleep(5)
				continue

			response_body = response.get("body", "")
			if not self._handle_status_response(response_body):
				self._status_poll_fail_streak += 1
				self._log(
					f"⚠️ Check status parse lỗi (lần {self._status_poll_fail_streak}/4)"
				)
			else:
				self._status_poll_fail_streak = 0
				self._mark_stuck_pending(time.time())
			await asyncio.sleep(5)

	def _handle_status_response(self, response_body):
		try:
			body_json = json.loads(response_body)
			operations = body_json.get("operations", [])
		except Exception:
			return False

		updated = False

		for op in operations:
			scene_id = op.get("sceneId")
			status = self._normalize_status_full(op.get("status"))
			if not scene_id:
				continue

			prev = self._scene_status.get(scene_id, {}).get("status")
			# Nếu đã SUCCESSFUL rồi thì bỏ qua mọi update sau đó để tránh tải lại
			if prev == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
				continue
			error = None
			if isinstance(op.get("error"), dict):
				error = op.get("error")
			elif isinstance(op.get("operation"), dict) and isinstance(op["operation"].get("error"), dict):
				error = op["operation"].get("error")
			if error:
				status = "MEDIA_GENERATION_STATUS_FAILED"
			self._scene_status.setdefault(scene_id, {})["status"] = status
			if prev != status:
				self._scene_status_change_ts[scene_id] = time.time()
			force_update = bool(error)
			if not force_update and (not prev or prev == status):
				continue

			prompt_info = self._scene_to_prompt.get(scene_id)
			if not prompt_info:
				continue

			prompt_id = prompt_info["prompt_id"]
			idx = prompt_info["index"]
			prompt_text = self._get_prompt_text(prompt_id)
			prompt_idx = f"{prompt_id}_{idx + 1}"

			video_url, image_url = self._extract_media_urls(op)
			video_path = ""
			image_path = ""
			error_code = ""
			error_message = ""
			if error:
				error_code = str(error.get("code", "")) if error.get("code") is not None else ""
				error_message = str(error.get("message", "")) if error.get("message") is not None else ""
				log_msg = "❌"
				if prompt_id is not None:
					log_msg += f" Prompt {prompt_id}"
				else:
					log_msg += f" Scene {scene_id[:8]}"
				if error_code:
					log_msg += f": [{error_code}]"
				if error_message:
					log_msg += f" {error_message}"
				self._log(log_msg)

			message_to_store = error_message
			if status == "MEDIA_GENERATION_STATUS_FAILED" and not message_to_store:
				op_summary = ""
				try:
					op_summary = json.dumps(op, ensure_ascii=True)
				except Exception:
					op_summary = ""
				if op_summary:
					op_summary = op_summary[:600]
				message_to_store = op_summary

			if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
				if video_url:
					video_path = self._download_video(video_url, prompt_idx)
				if image_url:
					image_path = self._download_image(image_url, prompt_idx)

			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				self._short_status(status),
				video_url=video_url,
				image_url=image_url,
				video_path=video_path,
				image_path=image_path,
				error=error_code,
				message=message_to_store,
			)

			self.video_updated.emit({
				"prompt_idx": prompt_idx,
				"status": self._short_status(status),
				"scene_id": scene_id,
				"prompt": prompt_text,
				"video_path": video_path,
				"image_path": image_path,
				"_prompt_id": prompt_id,
			})
			updated = True

		if updated:
			self._last_status_change_ts = time.time()
		return True

	def _check_in_flight_block(self):
		"""Kiểm tra kẹt giới hạn in-flight quá lâu."""
		if not self._in_flight_block_start_ts:
			self._in_flight_block_start_ts = time.time()
		elapsed = time.time() - self._in_flight_block_start_ts
		if elapsed < 420:
			return True
		if self._last_status_change_ts and self._last_status_change_ts > self._in_flight_block_start_ts:
			self._in_flight_block_start_ts = time.time()
			return True
		self._log("⚠️ Kẹt giới hạn tạo video quá 7 phút, tiếp tục chờ...")
		self._in_flight_block_start_ts = time.time()
		return True

	def _mark_stuck_pending(self, now_ts):
		"""Đánh dấu FAILED nếu status PENDING/ACTIVE không đổi quá 7 phút."""
		for scene_id, info in list(self._scene_status.items()):
			status = info.get("status")
			if not self._is_running_status(status):
				continue
			last_change = self._scene_status_change_ts.get(scene_id)
			if not last_change:
				self._scene_status_change_ts[scene_id] = now_ts
				continue
			if (now_ts - last_change) < 420:
				continue
			prompt_info = self._scene_to_prompt.get(scene_id)
			if not prompt_info:
				continue
			prompt_id = prompt_info.get("prompt_id")
			idx = prompt_info.get("index", 0)
			prompt_text = self._get_prompt_text(prompt_id)
			self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				"FAILED",
				error="STATUS_TIMEOUT",
				message="Timeout 7p khong thay doi status",
			)
			self.video_updated.emit({
				"prompt_idx": f"{prompt_id}_{idx + 1}",
				"status": "FAILED",
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	def _mark_pending_failed(self, message):
		"""Đánh dấu tất cả pending/active là FAILED và cập nhật state.json."""
		for scene_id, info in list(self._scene_status.items()):
			status = info.get("status")
			if not self._is_running_status(status):
				continue
			prompt_info = self._scene_to_prompt.get(scene_id)
			if not prompt_info:
				continue
			prompt_id = prompt_info.get("prompt_id")
			idx = prompt_info.get("index", 0)
			prompt_text = self._get_prompt_text(prompt_id)
			self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				"FAILED",
				error="STATUS",
				message=message,
			)
			self.video_updated.emit({
				"prompt_idx": f"{prompt_id}_{idx + 1}",
				"status": "FAILED",
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	def _extract_media_urls(self, op):
		fife_url = ""
		serving_base_uri = ""
		image_url = ""
		operation = op.get("operation", {}) if isinstance(op.get("operation"), dict) else {}
		metadata = operation.get("metadata", {}) if isinstance(operation.get("metadata"), dict) else {}
		video = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
		fife_url = video.get("fifeUrl", "") or ""
		serving_base_uri = video.get("servingBaseUri", "") or ""
		image = metadata.get("image", {}) if isinstance(metadata.get("image"), dict) else {}
		image_url = image.get("fifeUrl", "") or image.get("uri", "") or ""
		return fife_url, (image_url or serving_base_uri)

	def _download_video(self, url, prompt_idx):
		if not url:
			return ""
		video_dir = self._video_output_dir()
		video_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(video_dir, str(prompt_idx), ".mp4")
		try:
			with requests.get(url, stream=True, timeout=60) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 1024):
						if chunk:
							f.write(chunk)
			out_path = str(file_path.resolve())
			self._log(f"⬇️  Tải video xong: {out_path}")
			try:
				self.video_folder_updated.emit(str(video_dir.resolve()))
			except Exception:
				pass
			return out_path
		except Exception:
			self._log("⚠️  Không tải được video")
			return ""

	def _download_image(self, url, prompt_idx):
		if not url:
			return ""
		image_dir = self._image_output_dir()
		image_dir.mkdir(parents=True, exist_ok=True)
		file_path = self._build_timestamped_media_path(image_dir, str(prompt_idx), ".jpg")
		try:
			with requests.get(url, stream=True, timeout=60) as resp:
				resp.raise_for_status()
				with open(file_path, "wb") as f:
					for chunk in resp.iter_content(chunk_size=1024 * 256):
						if chunk:
							f.write(chunk)
			return str(file_path.resolve())
		except Exception:
			self._log("⚠️  Không tải được image")
			return ""

	def _get_prompt_text(self, prompt_id):
		prompts = self._load_image_prompts()
		for item in prompts:
			if item["id"] == prompt_id:
				return item.get("prompt", "")
		return ""

	def _get_image_link(self, prompt_id):
		prompts = self._load_image_prompts()
		for item in prompts:
			if item["id"] == prompt_id:
				return item.get("start_image_link") or item.get("image_link", "")
		return ""

	def _get_end_image_link(self, prompt_id):
		prompts = self._load_image_prompts()
		for item in prompts:
			if item["id"] == prompt_id:
				return item.get("end_image_link", "")
		return ""

	def _resolve_aspect_ratio_and_model(self):
		# Refresh account context mỗi lần tính model để đồng bộ loại tài khoản hiện tại
		i2v_api.refresh_account_context()
		veo_model = self.project_data.get("veo_model", "")
		is_start_end = self._is_start_end_mode()
		aspect_ratio = str(self.project_data.get("aspect_ratio", "")).lower()
		is_portrait = "dọc" in aspect_ratio or "9:16" in aspect_ratio or "portrait" in aspect_ratio
		if is_portrait:
			return (
				IMAGE_ASPECT_RATIO_PORTRAIT,
				VIDEO_ASPECT_RATIO_PORTRAIT,
				i2v_api.select_video_model_key(VIDEO_ASPECT_RATIO_PORTRAIT, veo_model, is_start_end=is_start_end),
			)
		return (
			IMAGE_ASPECT_RATIO_LANDSCAPE,
			VIDEO_ASPECT_RATIO_LANDSCAPE,
			i2v_api.select_video_model_key(VIDEO_ASPECT_RATIO_LANDSCAPE, veo_model, is_start_end=is_start_end),
		)

	def _encode_image_to_base64(self, image_link):
		image_bytes, mime_type = self._read_image_bytes(image_link)
		if not image_bytes:
			return "", ""
		base64_image = base64.b64encode(image_bytes).decode("ascii")
		return base64_image, mime_type

	def _read_image_bytes(self, image_link):
		parsed = urlparse(str(image_link))
		is_url = parsed.scheme in {"http", "https"}
		try:
			if is_url:
				resp = requests.get(image_link, timeout=30)
				resp.raise_for_status()
				mime_type = resp.headers.get("Content-Type") or ""
				if ";" in mime_type:
					mime_type = mime_type.split(";", 1)[0].strip()
				if not mime_type:
					mime_type = self._guess_mime_type(image_link)
				return resp.content, mime_type

			path = Path(image_link)
			if not path.exists():
				return b"", ""
			mime_type = self._guess_mime_type(str(path))
			return path.read_bytes(), mime_type
		except Exception:
			return b"", ""

	def _guess_mime_type(self, path_value):
		mime_type, _ = mimetypes.guess_type(path_value)
		return mime_type or "image/jpeg"

	def _extract_media_id(self, response_body):
		try:
			body_json = json.loads(response_body)
		except Exception:
			return ""
		if not isinstance(body_json, dict):
			return ""
		media_generation = body_json.get("mediaGenerationId")
		if isinstance(media_generation, dict):
			mg_id = media_generation.get("mediaGenerationId")
			if mg_id:
				return str(mg_id)
		if "mediaId" in body_json:
			return str(body_json.get("mediaId") or "")
		media = body_json.get("media")
		if isinstance(media, dict):
			return str(media.get("mediaId") or media.get("id") or "")
		return ""

	def _mark_prompt_failed(self, prompt_id, prompt_text, scene_ids, error_code, message):
		for idx, scene_id in enumerate(scene_ids):
			self._update_state_entry(
				prompt_id,
				prompt_text,
				scene_id,
				idx,
				"FAILED",
				error=error_code,
				message=message,
			)
			self.video_updated.emit({
				"prompt_idx": f"{prompt_id}_{idx + 1}",
				"status": "FAILED",
				"scene_id": scene_id,
				"prompt": prompt_text,
				"_prompt_id": prompt_id,
			})

	async def _init_token_collector(
		self,
		project_link,
		chrome_userdata_root,
		profile_name,
		clear_data_interval,
		idle_timeout,
		token_timeout,
	):
		"""✅ Khởi động TokenCollector - dùng cho timeout wrapper"""
		return TokenCollector(
			project_link,
			chrome_userdata_root=chrome_userdata_root,
			profile_name=profile_name,
			debug_port=9222,
			headless=False,
			hide_window=TOKEN_CHROME_HIDE_WINDOW,
			token_timeout=token_timeout,
			idle_timeout=idle_timeout,
			log_callback=self._log,
			stop_check=self._should_stop,
			clear_data_interval=clear_data_interval,
			keep_chrome_open=self._keep_chrome_open,
			mode="video",
		)

	async def _wait_for_completion(self):
		"""
		✅ Chờ tất cả video hoàn thành
		- Thoát khi: hết prompts (flag=True) + tất cả video đã SUCCESSFUL/FAILED
		- Hoặc khi bấm STOP
		"""
		_last_pending_log_ts = 0
		_no_pending_count = 0  # Đếm số lần liên tiếp không có pending
		
		while True:
			if self.STOP:
				self._log(f"🛑 STOP nhận được, thoát loop chờ - Thoát luồng lấy token")
				break

			# Timeout check
			if self._all_prompts_submitted and self._complete_wait_timeout > 0:
				if not self._complete_wait_start_ts:
					self._complete_wait_start_ts = time.time()
				elapsed = time.time() - self._complete_wait_start_ts
				if elapsed >= self._complete_wait_timeout:
					if not self._auto_noi_canh:
						self._log(
							f"⏱️  Quá thời gian chờ hoàn thành ({self._complete_wait_timeout}s), dừng workflow"
						)
					if not self._auto_noi_canh:
						self.STOP = 1
					break
			
			# ✅ KIỂM TRA TỪ STATE.JSON (nguồn chính xác nhất)
			state_pending = self._count_in_progress()
			
			# ✅ KIỂM TRA TỪ _scene_status (cả FULL và SHORT status)
			active_prompt_ids = {str(pid).strip() for pid in (self._active_prompt_ids or set()) if str(pid).strip()}
			scene_pending = []
			for scene_id, info in self._scene_status.items():
				if not self._is_running_status(info.get("status")):
					continue
				if active_prompt_ids:
					prompt_info = self._scene_to_prompt.get(str(scene_id), {}) or {}
					prompt_id = str(prompt_info.get("prompt_id", "")).strip()
					if prompt_id not in active_prompt_ids:
						continue
				scene_pending.append(info)
			
			# ✅ ĐIỀU KIỆN THOÁT: đã gửi hết prompts VÀ không còn video pending
			if self._all_prompts_submitted:
				# Ưu tiên kiểm tra state.json vì nó được cập nhật chính xác
				if state_pending == 0:
					_no_pending_count += 1
					self._log(f"🔍 Kiểm tra: state_pending={state_pending}, scene_pending={len(scene_pending)}, count={_no_pending_count}")
					# Chờ 2 lần liên tiếp để chắc chắn không còn pending
					if _no_pending_count >= 2:
						if not self._auto_noi_canh:
							self._log("✅ Tất cả video đã hoàn thành (từ state.json) - thoát workflow")
						self.STOP = 1
						break
				else:
					_no_pending_count = 0
				
				# Fallback: chỉ hoàn tất khi state + scene đều không còn pending
				if len(self._scene_status) > 0 and (not scene_pending) and state_pending == 0:
					if not self._auto_noi_canh:
						self._log("✅ Tất cả video đã hoàn thành (từ scene_status) - thoát workflow")
					self.STOP = 1
					break
			
			# Log trạng thái chờ mỗi 15s
			now = time.time()
			total_pending = max(state_pending, len(scene_pending))
			if total_pending > 0 and (now - _last_pending_log_ts) >= 15:
				_last_pending_log_ts = now
				self._log(f"⏳ Đang chờ {total_pending} video hoàn thành...")
			elif not self._all_prompts_submitted and (now - _last_pending_log_ts) >= 15:
				_last_pending_log_ts = now
				self._log(f"⏳ Chưa đủ prompts gửi, chờ thêm...")
			
			await asyncio.sleep(2)

	def _short_status(self, status):
		if not status:
			return "PENDING"
		upper = str(status).upper()
		if "PENDING" in upper:
			return "PENDING"
		if any(marker in upper for marker in {"RUNNING", "PROCESS", "PROGRESS", "QUEUED", "SUBMIT", "CREATING", "GENERATING", "STARTED"}):
			return "ACTIVE"
		if "ACTIVE" in upper:
			return "ACTIVE"
		if "SUCCESSFUL" in upper:
			return "SUCCESSFUL"
		if "FAILED" in upper:
			return "FAILED"
		return str(status).replace("MEDIA_GENERATION_STATUS_", "")

	def _is_running_status(self, status: str) -> bool:
		upper = str(status or "").upper()
		if not upper:
			return False
		return not self._is_terminal_status(upper)

	def _is_terminal_status(self, status: str) -> bool:
		upper = str(status or "").upper()
		if not upper:
			return False
		return any(marker in upper for marker in {"SUCCESS", "FAILED", "CANCEL", "ERROR"})

	def _normalize_status_full(self, status: str) -> str:
		upper = str(status or "").upper()
		if not upper:
			return "MEDIA_GENERATION_STATUS_PENDING"
		if upper.startswith("MEDIA_GENERATION_STATUS_"):
			return upper
		if upper in {"PENDING", "ACTIVE", "SUCCESSFUL", "FAILED"}:
			return f"MEDIA_GENERATION_STATUS_{upper}"
		return upper


TextToVideoWorkflow = ImageToVideoWorkflow


# ========= APP ENTRY HELPERS (Image to Video) =========

def _load_test_json(project_name):
	path = WORKFLOWS_DIR / project_name / "test.json"
	if not path.exists():
		return None
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return None


def _load_state_json(project_name):
	path = WORKFLOWS_DIR / project_name / "state.json"
	if not path.exists():
		return {}
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return {}


def _resolve_image_path(project_name, image_value):
	if not image_value:
		return ""
	image_path = str(image_value)
	img_path_obj = Path(image_path)
	if img_path_obj.is_absolute() and img_path_obj.exists():
		return str(img_path_obj)
	project_dir = WORKFLOWS_DIR / project_name
	candidate_paths = [
		project_dir / image_path,
		project_dir / "Download" / "ảnh" / image_path,
		project_dir / "Download" / "image" / image_path,
	]
	for candidate in candidate_paths:
		if candidate.exists():
			return str(candidate)
	return image_path


def _find_image_prompt(project_name, prompt_id):
	data = _load_test_json(project_name) or {}
	prompts_data = data.get("prompts", {}) if isinstance(data, dict) else {}
	image_prompts = prompts_data.get("image_to_video", []) if isinstance(prompts_data, dict) else []
	for prompt in image_prompts:
		if str(prompt.get("id")) == str(prompt_id):
			image_val = (
				prompt.get("image_link")
				or prompt.get("image")
				or prompt.get("image_path")
				or ""
			)
			return {
				"id": prompt.get("id"),
				"prompt": prompt.get("prompt", ""),
				"image_link": _resolve_image_path(project_name, image_val),
			}
	return None


def _start_image_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb):
	wf = ImageToVideoWorkflow(project_name=project_name, project_data=project_data, parent=parent)
	if log_cb:
		wf.log_message.connect(log_cb)
	if video_cb:
		wf.video_updated.connect(video_cb)
	if complete_cb:
		wf.automation_complete.connect(complete_cb)
	if resend_items:
		wf._resend_items = resend_items
	wf.start()
	return wf


def start_image_resend_single(parent, project_name, prompt_id, video_idx, log_cb=None, video_cb=None, complete_cb=None):
	"""Resend 1 prompt (image to video) hoàn toàn trong workflow file."""
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	prompt_info = _find_image_prompt(project_name, prompt_id)
	if not prompt_info:
		if log_cb:
			log_cb(f"⚠️ Không tìm thấy prompt ID {prompt_id} trong test.json")
		return None
	state_data = _load_state_json(project_name)
	prompt_state = (state_data.get("prompts", {}) or {}).get(str(prompt_id), {})
	scene_ids = prompt_state.get("scene_ids", []) if isinstance(prompt_state, dict) else []
	if video_idx <= 0 or video_idx > len(scene_ids):
		if log_cb:
			log_cb(f"⚠️ Không tìm thấy scene_id cho prompt {prompt_id}, video {video_idx}")
		return None
	scene_id = scene_ids[video_idx - 1]
	resend_items = [(prompt_id, prompt_info.get("prompt", ""), scene_id, video_idx - 1)]
	project_data.setdefault("prompts", {})
	project_data["prompts"]["image_to_video"] = [prompt_info]
	project_data["_is_resend"] = True
	project_data["_prompt_id"] = prompt_id
	project_data["_prompt_text"] = prompt_info.get("prompt", "")
	project_data["_resend_prompt_idx"] = f"{prompt_id}_{video_idx}"
	project_data["_resend_video_idx"] = video_idx
	return _start_image_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)


def start_image_resend_selected(parent, project_name, prompt_ids, log_cb=None, video_cb=None, complete_cb=None):
	"""Resend nhiều prompt (image to video) dựa trên danh sách prompt_ids."""
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	state_data = _load_state_json(project_name)
	selected_prompts = []
	resend_items = []
	for pid in prompt_ids:
		prompt_info = _find_image_prompt(project_name, pid)
		if not prompt_info:
			if log_cb:
				log_cb(f"⚠️ Bỏ qua ID {pid} (không tìm thấy trong test.json)")
			continue
		selected_prompts.append(prompt_info)
		prompt_state = (state_data.get("prompts", {}) or {}).get(str(pid), {})
		scene_ids = prompt_state.get("scene_ids", []) if isinstance(prompt_state, dict) else []
		for idx, scene_id in enumerate(scene_ids):
			if scene_id:
				resend_items.append((pid, prompt_info.get("prompt", ""), scene_id, idx))
	if not selected_prompts or not resend_items:
		if log_cb:
			log_cb("⚠️ Không có prompt/image nào hợp lệ để resend")
		return None
	project_data.setdefault("prompts", {})
	project_data["prompts"]["image_to_video"] = selected_prompts
	project_data["_is_resend"] = True
	return _start_image_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)


def start_image_resend_failed(parent, project_name, failed_videos_map, log_cb=None, video_cb=None, complete_cb=None):
	"""Resend các video failed cho image_to_video (map prompt_id -> list video_idx)."""
	project_data = _load_test_json(project_name)
	if not project_data:
		if log_cb:
			log_cb(f"⚠️ Không đọc được test.json cho {project_name}")
		return None
	state_data = _load_state_json(project_name)
	selected_prompts = []
	resend_items = []
	seen_ids = set()
	for pid, video_indices in (failed_videos_map or {}).items():
		prompt_info = _find_image_prompt(project_name, pid)
		if not prompt_info:
			if log_cb:
				log_cb(f"⚠️ Bỏ qua ID {pid} (không tìm thấy trong test.json)")
			continue
		if pid not in seen_ids:
			selected_prompts.append(prompt_info)
			seen_ids.add(pid)
		prompt_state = (state_data.get("prompts", {}) or {}).get(str(pid), {})
		scene_ids = prompt_state.get("scene_ids", []) if isinstance(prompt_state, dict) else []
		for vid_idx in video_indices:
			if 0 < vid_idx <= len(scene_ids):
				scene_id = scene_ids[vid_idx - 1]
				resend_items.append((pid, prompt_info.get("prompt", ""), scene_id, vid_idx - 1))
	if not selected_prompts or not resend_items:
		if log_cb:
			log_cb("⚠️ Không có dữ liệu resend hợp lệ (failed_map)")
		return None
	project_data.setdefault("prompts", {})
	project_data["prompts"]["image_to_video"] = selected_prompts
	project_data["_is_resend"] = True
	project_data["_is_resend_all"] = True
	return _start_image_workflow(parent, project_name, project_data, resend_items, log_cb, video_cb, complete_cb)
