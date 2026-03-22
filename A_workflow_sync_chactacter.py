import asyncio
import base64
import importlib
import json
import mimetypes
import re
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
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

import API_sync_chactacter as sync_api

from A_workflow_get_token import TokenCollector
from chrome_process_manager import ChromeProcessManager
from settings_manager import SettingsManager, WORKFLOWS_DIR
from workflow_run_control import get_running_video_count

TOKEN_CHROME_HIDE_WINDOW = True


class CharacterSyncWorkflow(QThread):
    log_message = Signal(str)
    video_updated = Signal(dict)
    automation_complete = Signal()
    video_folder_updated = Signal(str)

    def __init__(self, project_name=None, project_data=None, parent=None):
        super().__init__(parent)
        self.project_name = project_name or (project_data or {}).get("project_name", "default_project")
        self.project_data = project_data or {}
        self.STOP = 0
        self._upload_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sync-char-upload")
        self._scene_status: dict[str, dict] = {}
        self._scene_to_prompt: dict[str, dict] = {}
        self._scene_next_check_at: dict[str, float] = {}
        self._scene_status_change_ts: dict[str, float] = {}
        self._status_log_ts = 0.0
        self._pending_log_interval = 15.0
        self._status_poll_fail_streak = 0
        self._last_status_change_ts = 0.0
        self._all_prompts_submitted = False
        self._complete_wait_start_ts = 0.0
        self._complete_wait_timeout = 0
        self._active_prompt_ids = set()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run_workflow())
        except Exception as exc:
            self._log(f"❌ Lỗi workflow sync character: {exc}")
            self._log(traceback.format_exc()[:1200])
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
            try:
                ChromeProcessManager.close_chrome_gracefully(stop_check=self._should_stop)
            except Exception:
                pass
            self.automation_complete.emit()

    def stop(self):
        self.STOP = 1

    def _should_stop(self):
        return bool(self.STOP)

    def _log(self, message):
        try:
            self.log_message.emit(str(message or ""))
        except Exception:
            pass

    async def _sleep_with_stop(self, seconds, step=0.2):
        end_ts = time.time() + max(0.0, float(seconds or 0.0))
        while time.time() < end_ts:
            if self._should_stop():
                return False
            await asyncio.sleep(min(step, max(0.01, end_ts - time.time())))
        return not self._should_stop()

    async def _run_workflow(self):
        if self._should_stop():
            return

        self._cleanup_workflow_data()
        prompts = self._load_text_prompts()
        if not prompts:
            self._log("❌ Không có prompt cho sync character")
            return

        character_profiles = self._load_character_profiles()
        if not character_profiles:
            self._log("❌ Không có ảnh nhân vật hoặc tên nhân vật")
            return

        plans, overflow_items = self._build_prompt_plans(prompts, character_profiles)
        self._active_prompt_ids = {str((p or {}).get("id") or "").strip() for p in (plans or []) if str((p or {}).get("id") or "").strip()}
        if overflow_items:
            ok = self._ask_continue_with_overflow(overflow_items)
            if not ok:
                self._log("⛔ Người dùng hủy do prompt có >3 nhân vật")
                return

        auth = self._load_auth_config()
        if not auth:
            self._log("❌ Thiếu sessionId/projectId/access_token trong config")
            return

        config = SettingsManager.load_config()
        wait_gen_video = self._resolve_int_config(config, "WAIT_GEN_VIDEO", 15)
        token_retry = self._resolve_int_config(config, "TOKEN_RETRY", 3)
        token_retry_delay = self._resolve_int_config(config, "TOKEN_RETRY_DELAY", 2)
        get_token_timeout = max(15, self._resolve_int_config(config, "TOKEN_TIMEOUT", 60))
        self._complete_wait_timeout = self._resolve_int_config(config, "WAIT_COMPLETE_TIMEOUT", 0)
        output_count = max(1, self._resolve_int_config(config, "OUTPUT_COUNT", 1))

        all_profiles: dict[str, dict] = {}
        for plan in plans:
            for prof in plan.get("profiles", []):
                all_profiles[str(prof["name_key"])] = prof

        self._log(f"⬆️ Upload ảnh nhân vật dùng chung: {len(all_profiles)} ảnh (tối đa 5 thread)")
        media_cache = await self._upload_all_character_media(
            list(all_profiles.values()),
            auth["sessionId"],
            auth["access_token"],
            auth.get("cookie"),
        )
        if self._should_stop():
            return

        for key, profile in all_profiles.items():
            if key not in media_cache:
                self._log(f"❌ Upload ảnh nhân vật thất bại: {profile.get('name')}")
                return

        status_task = asyncio.create_task(
            self._status_poll_loop(auth["access_token"], auth.get("cookie"))
        )

        profile_name = self.project_data.get("veo_profile") or SettingsManager.load_settings().get("current_profile")
        project_link = auth.get("URL_GEN_TOKEN") or self.project_data.get("project_link") or "https://labs.google/fx/vi/tools/flow"
        chrome_userdata_root = auth.get("folder_user_data_get_token") or SettingsManager.create_chrome_userdata_folder(profile_name)

        collector = None
        try:
            collector = await asyncio.wait_for(
                self._init_token_collector(
                    project_link,
                    chrome_userdata_root,
                    profile_name,
                    self._resolve_int_config(config, "CLEAR_DATA_WAIT", 2),
                    40,
                    get_token_timeout,
                ),
                timeout=30,
            )
        except Exception as exc:
            self._log(f"❌ Không khởi tạo được TokenCollector: {exc}")
            status_task.cancel()
            return

        retry_token_counter = 0
        async with collector:
            for i, plan in enumerate(plans):
                if self._should_stop():
                    break

                prompt_id = str(plan["id"])
                prompt_text = str(plan["prompt"])
                profiles = list(plan.get("profiles") or [])[:3]
                if not profiles:
                    self._mark_prompt_failed(prompt_id, prompt_text, "NO_CHARACTER", "Prompt không nhắc đến nhân vật nào")
                    continue

                ref_media_ids = []
                for prof in profiles:
                    media_id = media_cache.get(str(prof["name_key"]), "")
                    if media_id:
                        ref_media_ids.append(media_id)

                if not ref_media_ids:
                    self._mark_prompt_failed(prompt_id, prompt_text, "UPLOAD", "Không có mediaId ảnh nhân vật")
                    continue

                self.video_updated.emit(
                    {
                        "prompt_idx": f"{prompt_id}_1",
                        "status": "ACTIVE",
                        "scene_id": "",
                        "prompt": prompt_text,
                        "_prompt_id": prompt_id,
                    }
                )
                self._log(f"🔐 Prompt {prompt_id}: Đang lấy token")

                token = ""
                for attempt in range(token_retry):
                    if self._should_stop():
                        break
                    try:
                        retry_token_counter += 1
                        clear_storage = False
                        clear_every = self._resolve_int_config(config, "CLEAR_DATA", 0)
                        if clear_every > 0 and (retry_token_counter % clear_every == 0):
                            clear_storage = True
                        token = await asyncio.wait_for(
                            collector.get_token(clear_storage=clear_storage),
                            timeout=get_token_timeout,
                        )
                        if token:
                            self._log(f"✅ Prompt {prompt_id}: Lấy token thành công")
                            break
                    except Exception as exc:
                        self._log(f"⚠️ Lấy token lỗi (prompt {prompt_id}, lần {attempt + 1}): {exc}")
                    if attempt < token_retry - 1:
                        await self._sleep_with_stop(token_retry_delay)

                if not token:
                    self._mark_prompt_failed(prompt_id, prompt_text, "TOKEN", "Không lấy được token")
                    continue

                model_key = sync_api.select_video_model_key(
                    sync_api.VIDEO_ASPECT_RATIO_PORTRAIT,
                    self.project_data.get("veo_model"),
                )

                payload = sync_api.build_payload_generate_video_reference(
                    token=token,
                    session_id=auth["sessionId"],
                    project_id=auth["projectId"],
                    prompt=prompt_text,
                    seed=self._resolve_seed(config, i),
                    video_model_key=model_key,
                    reference_media_ids=ref_media_ids,
                    scene_id=None,
                    aspect_ratio=sync_api.VIDEO_ASPECT_RATIO_PORTRAIT,
                    output_count=output_count,
                )

                scene_ids = self._assign_scene_ids_to_payload(payload, prompt_id)
                self._save_request_json(payload, prompt_id, prompt_text, flow="character_sync")
                self._log(f"🚀 Gửi request sync character prompt {prompt_id} ({i + 1}/{len(plans)})")

                token_option = "Option 2"
                self._log(f"🔧 Token Option (forced): {token_option}")
                response = await sync_api.request_create_video_via_browser(
                    collector.page,
                    payload,
                    auth.get("cookie"),
                    auth["access_token"],
                )

                response_body = response.get("body", "")
                operations = self._parse_operations(response_body)
                err_code, err_msg = self._extract_error_info(response_body)

                if (not response.get("ok", True)) and err_msg and not operations:
                    self._mark_prompt_failed(prompt_id, prompt_text, err_code or "REQUEST", err_msg)
                    continue

                if not operations:
                    self._mark_prompt_failed(prompt_id, prompt_text, "REQUEST", "Không có operations trả về")
                    continue

                self._handle_create_response(prompt_id, prompt_text, scene_ids, operations)
                await self._sleep_with_stop(wait_gen_video)

        self._all_prompts_submitted = True
        self._complete_wait_start_ts = time.time()
        await self._wait_for_completion()
        status_task.cancel()

    def _resolve_seed(self, config, index):
        seed_mode = str(config.get("SEED_MODE", "Random")).strip().lower()
        if seed_mode == "fixed":
            return self._resolve_int_config(config, "SEED_VALUE", sync_api.DEFAULT_SEED)
        return int(time.time() * 1000 + int(index)) % 100000

    def _build_prompt_plans(self, prompts, profiles):
        plans = []
        overflows = []
        for idx, item in enumerate(prompts):
            prompt_id = str(item.get("id") or idx + 1)
            prompt_text = str(item.get("prompt") or item.get("description") or "").strip()
            found = self._find_profiles_in_prompt(prompt_text, profiles)
            if len(found) > 3:
                overflows.append(
                    {
                        "prompt_id": prompt_id,
                        "prompt": prompt_text,
                        "all_names": [x.get("name") for x in found],
                    }
                )
            plans.append(
                {
                    "id": prompt_id,
                    "prompt": prompt_text,
                    "profiles": found[:3],
                }
            )
        return plans, overflows

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

    def _ask_continue_with_overflow(self, overflow_items):
        try:
            detail = []
            for item in overflow_items[:5]:
                names = ", ".join(list(item.get("all_names") or []))
                detail.append(f"- Prompt {item.get('prompt_id')}: {names}")
            detail_text = "\n".join(detail)
            msg = (
                "Một số prompt nhắc tới hơn 3 nhân vật.\n"
                "Hệ thống chỉ dùng 3 nhân vật đầu tiên cho mỗi prompt.\n\n"
                f"{detail_text}\n\n"
                "Bạn có muốn tiếp tục không?"
            )
            ans = QMessageBox.question(
                None,
                "Sync Character",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            return ans == QMessageBox.StandardButton.Yes
        except Exception:
            self._log("⚠️ Không hiển thị được hộp thoại xác nhận, mặc định dừng để an toàn")
            return False

    async def _upload_all_character_media(self, profiles, session_id, access_token, cookie):
        sem = asyncio.Semaphore(5)

        async def _upload_one(profile):
            async with sem:
                return await self._upload_profile_media(profile, session_id, access_token, cookie)

        tasks = [asyncio.create_task(_upload_one(p)) for p in profiles]
        results = await asyncio.gather(*tasks)

        media_cache = {}
        for ok, key, media_id, message in results:
            if ok and key and media_id:
                media_cache[key] = media_id
            else:
                self._log(f"❌ Upload ảnh nhân vật lỗi: {message}")
        return media_cache

    async def _upload_profile_media(self, profile, session_id, access_token, cookie):
        key = str(profile.get("name_key") or "")
        path = str(profile.get("path") or "")
        name = str(profile.get("name") or key)
        if not key or not path:
            return False, key, "", f"{name}: thiếu dữ liệu"

        image_bytes, mime_type = self._read_image_bytes(path)
        if not image_bytes:
            return False, key, "", f"{name}: không đọc được ảnh"

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        payload = sync_api.build_payload_upload_image(
            b64,
            mime_type,
            session_id,
            aspect_ratio=sync_api.IMAGE_ASPECT_RATIO_PORTRAIT,
        )
        try:
            response = await asyncio.get_running_loop().run_in_executor(
                self._upload_executor,
                lambda: asyncio.run(sync_api.request_upload_image(payload, access_token, cookie=cookie)),
            )
        except Exception as exc:
            return False, key, "", f"{name}: upload exception {exc}"

        body = response.get("body", "")
        media_id = self._extract_media_id(body)
        if not response.get("ok", True) or not media_id:
            return False, key, "", f"{name}: upload thất bại"

        self._log(f"✅ Upload ảnh nhân vật: {name}")
        return True, key, str(media_id), ""

    def _assign_scene_ids_to_payload(self, payload, prompt_id):
        scene_ids = []
        for idx, req in enumerate(list(payload.get("requests") or [])):
            scene_id = str(uuid.uuid4())
            metadata = req.get("metadata") if isinstance(req.get("metadata"), dict) else {}
            metadata["sceneId"] = scene_id
            req["metadata"] = metadata
            scene_ids.append(scene_id)
            self._scene_to_prompt[scene_id] = {"prompt_id": str(prompt_id), "index": idx}
            self._scene_status[scene_id] = {
                "status": "MEDIA_GENERATION_STATUS_PENDING",
                "operation_name": "",
            }
            self._scene_next_check_at[scene_id] = time.time() + 999999
            self._scene_status_change_ts[scene_id] = time.time()
        return scene_ids

    def _handle_create_response(self, prompt_id, prompt_text, scene_ids, operations):
        op_map = {}
        for op in operations:
            scene_id = op.get("sceneId")
            if scene_id:
                op_map[str(scene_id)] = op

        for idx, scene_id in enumerate(scene_ids):
            op = op_map.get(scene_id) or (operations[idx] if idx < len(operations) else {})
            status = self._normalize_status_full(op.get("status"))
            op_name = str(((op.get("operation") or {}) if isinstance(op.get("operation"), dict) else {}).get("name") or "")
            self._scene_status[scene_id]["status"] = status
            self._scene_status[scene_id]["operation_name"] = op_name
            self._scene_next_check_at[scene_id] = time.time() + 6
            self._scene_status_change_ts[scene_id] = time.time()
            self._last_status_change_ts = time.time()
            self._log(
                f"📨 Prompt {prompt_id}_{idx + 1} create response: {self._short_status(status)}"
            )

            self._update_state_entry(
                prompt_id,
                prompt_text,
                scene_id,
                idx,
                self._short_status(status),
            )

            self.video_updated.emit(
                {
                    "prompt_idx": f"{prompt_id}_{idx + 1}",
                    "status": self._short_status(status),
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "_prompt_id": prompt_id,
                }
            )

    async def _status_poll_loop(self, access_token, cookie=None):
        while not self._should_stop():
            pending = [
                sid
                for sid, info in self._scene_status.items()
                if info.get("status") in {
                    "MEDIA_GENERATION_STATUS_ACTIVE",
                    "MEDIA_GENERATION_STATUS_PENDING",
                    "ACTIVE",
                    "PENDING",
                }
            ]
            if not pending:
                await asyncio.sleep(1)
                continue

            eligible = [sid for sid in pending if self._scene_next_check_at.get(sid, 0) <= time.time()]
            if not eligible:
                await asyncio.sleep(1)
                continue

            now = time.time()
            if (now - self._status_log_ts) >= self._pending_log_interval:
                self._status_log_ts = now
                self._log(f"🔄 Check status: {len(pending)} scene đang chờ/đang tạo")

            payload = {"operations": []}
            for scene_id in eligible:
                info = self._scene_status.get(scene_id, {})
                op = {"sceneId": scene_id, "status": info.get("status", "")}
                op_name = info.get("operation_name")
                if op_name:
                    op["operation"] = {"name": op_name}
                payload["operations"].append(op)

            try:
                response = await sync_api.request_check_status(payload, access_token, cookie=cookie)
            except Exception as exc:
                self._status_poll_fail_streak += 1
                self._log(f"⚠️ Check status lỗi (lần {self._status_poll_fail_streak}/4): {exc}")
                await asyncio.sleep(5)
                continue

            if not response.get("ok", True):
                self._status_poll_fail_streak += 1
                status_code = response.get("status")
                reason = response.get("reason")
                self._log(
                    f"⚠️ Check status thất bại (lần {self._status_poll_fail_streak}/4, status={status_code}, reason={reason})"
                )
                await asyncio.sleep(5)
                continue

            body = response.get("body", "")
            try:
                ok_parse = self._handle_status_response(body)
            except Exception as exc:
                ok_parse = False
                self._status_poll_fail_streak += 1
                self._log(f"❌ Check status exception (lần {self._status_poll_fail_streak}/4): {exc}")
                self._log(traceback.format_exc()[:800])

            if not ok_parse:
                self._status_poll_fail_streak += 1
                self._log(f"⚠️ Check status parse lỗi (lần {self._status_poll_fail_streak}/4)")
            else:
                self._status_poll_fail_streak = 0
                self._mark_stuck_pending(time.time())
            await asyncio.sleep(5)

    def _handle_status_response(self, response_body):
        try:
            operations = (json.loads(response_body) or {}).get("operations", [])
        except Exception:
            return False

        updated = False

        for op in operations:
            scene_id = str(op.get("sceneId") or "")
            if not scene_id:
                continue
            if scene_id not in self._scene_to_prompt:
                continue

            status = self._normalize_status_full(op.get("status"))
            prev = self._scene_status.get(scene_id, {}).get("status")
            if prev == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                continue
            error = op.get("error") if isinstance(op.get("error"), dict) else None
            if error is None:
                operation_obj = op.get("operation") if isinstance(op.get("operation"), dict) else {}
                op_error = operation_obj.get("error")
                if isinstance(op_error, dict):
                    error = op_error
            if error:
                status = "MEDIA_GENERATION_STATUS_FAILED"

            pinfo = self._scene_to_prompt[scene_id]
            prompt_id = str(pinfo.get("prompt_id"))
            idx = int(pinfo.get("index", 0))
            prompt_text = self._get_prompt_text(prompt_id)
            prompt_idx = f"{prompt_id}_{idx + 1}"

            self._scene_status.setdefault(scene_id, {})["status"] = status
            if prev != status:
                self._scene_status_change_ts[scene_id] = time.time()
                self._log(
                    f"🔄 Prompt {prompt_id}_{idx + 1}: {self._short_status(prev)} → {self._short_status(status)}"
                )
            self._scene_next_check_at[scene_id] = time.time() + 6

            force_update = bool(error)
            if not force_update and prev == status:
                continue

            video_url, image_url = self._extract_media_urls(op)
            video_path = ""
            image_path = ""
            error_code = ""
            error_message = ""

            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                error_message = str(error.get("message") or "")
                if error_code or error_message:
                    log_msg = f"❌ Prompt {prompt_id}"
                    if error_code:
                        log_msg += f" [{error_code}]"
                    if error_message:
                        log_msg += f" {error_message}"
                    self._log(log_msg)

            if status == "MEDIA_GENERATION_STATUS_SUCCESSFUL":
                self.video_updated.emit(
                    {
                        "prompt_idx": prompt_idx,
                        "status": "DOWNLOADING",
                        "scene_id": scene_id,
                        "prompt": prompt_text,
                        "_prompt_id": prompt_id,
                    }
                )
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
                message=error_message,
            )

            self.video_updated.emit(
                {
                    "prompt_idx": prompt_idx,
                    "status": self._short_status(status),
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "video_path": video_path,
                    "image_path": image_path,
                    "_prompt_id": prompt_id,
                    "error_code": error_code,
                    "error_message": error_message,
                }
            )
            updated = True

        if updated:
            self._last_status_change_ts = time.time()
        return True

    def _mark_stuck_pending(self, now_ts):
        for scene_id, info in list(self._scene_status.items()):
            status = str(info.get("status") or "")
            if status not in {
                "MEDIA_GENERATION_STATUS_ACTIVE",
                "MEDIA_GENERATION_STATUS_PENDING",
                "ACTIVE",
                "PENDING",
            }:
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

            prompt_id = str(prompt_info.get("prompt_id") or "")
            idx = int(prompt_info.get("index", 0))
            prompt_text = self._get_prompt_text(prompt_id)
            self._scene_status[scene_id]["status"] = "MEDIA_GENERATION_STATUS_FAILED"
            self._update_state_entry(
                prompt_id,
                prompt_text,
                scene_id,
                idx,
                "FAILED",
                error="STATUS_TIMEOUT",
                message="Timeout 7p không thay đổi status",
            )
            self.video_updated.emit(
                {
                    "prompt_idx": f"{prompt_id}_{idx + 1}",
                    "status": "FAILED",
                    "scene_id": scene_id,
                    "prompt": prompt_text,
                    "_prompt_id": prompt_id,
                    "error_code": "STATUS_TIMEOUT",
                    "error_message": "Timeout 7p không thay đổi status",
                }
            )

    def _mark_prompt_failed(self, prompt_id, prompt_text, error_code, message):
        scene_id = str(uuid.uuid4())
        self._update_state_entry(
            prompt_id,
            prompt_text,
            scene_id,
            0,
            "FAILED",
            error=error_code,
            message=message,
        )
        self.video_updated.emit(
            {
                "prompt_idx": f"{prompt_id}_1",
                "status": "FAILED",
                "scene_id": scene_id,
                "prompt": prompt_text,
                "_prompt_id": prompt_id,
                "error_code": str(error_code or ""),
                "error_message": str(message or ""),
            }
        )

    async def _wait_for_completion(self):
        while True:
            if self._should_stop():
                return

            pending = self._count_in_progress()
            if self._all_prompts_submitted and pending <= 0:
                self._log("✅ Sync character hoàn tất")
                return

            if self._all_prompts_submitted and self._complete_wait_timeout > 0:
                elapsed = time.time() - float(self._complete_wait_start_ts or time.time())
                if elapsed >= self._complete_wait_timeout:
                    self._log("⏱️ Quá thời gian chờ hoàn thành, dừng workflow")
                    return

            await asyncio.sleep(2)

    def _short_status(self, status):
        text = str(status or "")
        if "PENDING" in text:
            return "PENDING"
        if "ACTIVE" in text:
            return "ACTIVE"
        if "SUCCESSFUL" in text:
            return "SUCCESSFUL"
        if "FAILED" in text:
            return "FAILED"
        if not text:
            return "UNKNOWN"
        return text

    def _normalize_status_full(self, value):
        s = str(value or "").strip().upper()
        if not s:
            return "MEDIA_GENERATION_STATUS_PENDING"
        if s in {
            "MEDIA_GENERATION_STATUS_PENDING",
            "MEDIA_GENERATION_STATUS_ACTIVE",
            "MEDIA_GENERATION_STATUS_SUCCESSFUL",
            "MEDIA_GENERATION_STATUS_FAILED",
        }:
            return s
        if s == "PENDING":
            return "MEDIA_GENERATION_STATUS_PENDING"
        if s == "ACTIVE":
            return "MEDIA_GENERATION_STATUS_ACTIVE"
        if s in {"SUCCESS", "SUCCESSFUL"}:
            return "MEDIA_GENERATION_STATUS_SUCCESSFUL"
        if s in {"FAIL", "FAILED", "ERROR"}:
            return "MEDIA_GENERATION_STATUS_FAILED"
        return s

    def _extract_media_urls(self, op):
        operation = op.get("operation", {}) if isinstance(op.get("operation"), dict) else {}
        metadata = operation.get("metadata", {}) if isinstance(operation.get("metadata"), dict) else {}
        video = metadata.get("video", {}) if isinstance(metadata.get("video"), dict) else {}
        image = metadata.get("image", {}) if isinstance(metadata.get("image"), dict) else {}

        fife_url = str(video.get("fifeUrl") or "")
        serving_base_uri = str(video.get("servingBaseUri") or "")
        image_url = str(image.get("fifeUrl") or image.get("uri") or "")
        video_url = fife_url or serving_base_uri
        if not image_url:
            image_url = serving_base_uri
        return video_url, image_url

    def _download_video(self, url, prompt_idx):
        if not url:
            return ""
        output_dir = self._video_output_dir()
        file_path = self._build_timestamped_media_path(output_dir, str(prompt_idx), ".mp4")
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            out_path = str(file_path.resolve())
            self.video_folder_updated.emit(str(output_dir.resolve()))
            return out_path
        except Exception:
            return ""

    def _download_image(self, url, prompt_idx):
        if not url:
            return ""
        output_dir = self._image_output_dir()
        file_path = self._build_timestamped_media_path(output_dir, str(prompt_idx), ".jpg")
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(file_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
            return str(file_path.resolve())
        except Exception:
            return ""

    def _build_timestamped_media_path(self, output_dir: Path, prompt_idx: str, suffix: str) -> Path:
        timestamp = datetime.now().strftime("%d%m%Y_%H%M%S")
        base_name = f"{prompt_idx}_{timestamp}"
        file_path = output_dir / f"{base_name}{suffix}"
        counter = 1
        while file_path.exists():
            file_path = output_dir / f"{base_name}_{counter}{suffix}"
            counter += 1
        return file_path

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

    def _extract_media_id(self, response_body):
        try:
            body_json = json.loads(response_body)
        except Exception:
            return ""
        if not isinstance(body_json, dict):
            return ""
        mg = body_json.get("mediaGenerationId")
        if isinstance(mg, dict):
            mid = mg.get("mediaGenerationId")
            if mid:
                return str(mid)
        media = body_json.get("media")
        if isinstance(media, dict):
            mid = media.get("mediaId") or media.get("id")
            if mid:
                return str(mid)
        return str(body_json.get("mediaId") or "")

    def _parse_operations(self, response_body):
        try:
            return (json.loads(response_body) or {}).get("operations", [])
        except Exception:
            return []

    def _extract_error_info(self, response_body):
        try:
            err = (json.loads(response_body) or {}).get("error")
        except Exception:
            err = None
        if not isinstance(err, dict):
            return "", ""
        return str(err.get("code") or ""), str(err.get("message") or "")

    def _save_request_json(self, payload, prompt_id, prompt_text, flow="character_sync"):
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
                    txt = request_file.read_text(encoding="utf-8").strip()
                    if txt:
                        parsed = json.loads(txt)
                        if isinstance(parsed, list):
                            entries = parsed
                        elif isinstance(parsed, dict):
                            entries = [parsed]
                except Exception:
                    entries = []
            entries.append(request_data)
            request_file.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log(f"⚠️ Không thể lưu request.json: {exc}")

    def _get_state_file_path(self):
        project_dir = WORKFLOWS_DIR / str(self.project_name)
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "state.json"

    def _load_state_json(self):
        state_file = self._get_state_file_path()
        if not state_file.exists():
            return {}
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_state_json(self, data):
        try:
            self._get_state_file_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except Exception:
            return False

    def _ensure_prompt_entry(self, state_data, prompt_id, prompt_text):
        if "prompts" not in state_data:
            state_data["prompts"] = {}
        key = str(prompt_id)
        if key not in state_data["prompts"]:
            state_data["prompts"][key] = {
                "id": str(prompt_id),
                "prompt": str(prompt_text or ""),
                "scene_ids": [],
                "statuses": [],
                "video_paths": [],
                "image_paths": [],
                "video_urls": [],
                "image_urls": [],
                "errors": [],
                "messages": [],
            }
        return state_data["prompts"][key]

    def _update_state_entry(self, prompt_id, prompt_text, scene_id, idx, status, video_url="", image_url="", video_path="", image_path="", error="", message=""):
        state_data = self._load_state_json()
        pdata = self._ensure_prompt_entry(state_data, prompt_id, prompt_text)

        if "scene_id_map" not in state_data:
            state_data["scene_id_map"] = {}

        while len(pdata["scene_ids"]) <= idx:
            pdata["scene_ids"].append("")
        pdata["scene_ids"][idx] = str(scene_id or "")
        state_data["scene_id_map"][str(scene_id or "")] = str(prompt_id)

        for key, val in [
            ("statuses", status),
            ("video_paths", video_path),
            ("image_paths", image_path),
            ("video_urls", video_url),
            ("image_urls", image_url),
            ("errors", error),
            ("messages", message),
        ]:
            while len(pdata[key]) <= idx:
                pdata[key].append("")
            pdata[key][idx] = str(val or "")

        self._save_state_json(state_data)

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
            if any(any(marker in str(s or "").upper() for marker in running_markers) for s in statuses):
                count += 1
        return count

    def _count_in_progress(self):
        worker_count = get_running_video_count(default_value=-1)
        if int(worker_count) >= 0:
            return int(worker_count)
        return int(self._count_in_progress_from_state())

    def _cleanup_workflow_data(self):
        try:
            self._save_state_json({})
            project_dir = WORKFLOWS_DIR / str(self.project_name)
            if not project_dir.exists():
                return
            keep_files = {"test.json", "status.json"}
            keep_dirs = {"Download", "thumbnails"}
            for item in project_dir.iterdir():
                if item.name in keep_files or item.name in keep_dirs:
                    continue
                try:
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        import shutil
                        shutil.rmtree(item, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    def _resolve_int_config(self, config, key, default_value):
        try:
            return int(config.get(key, default_value))
        except Exception:
            return int(default_value)

    def _output_root_dir(self):
        raw = str(self.project_data.get("video_output_dir") or self.project_data.get("output_dir") or "").strip()
        if not raw:
            raw = str(WORKFLOWS_DIR / self.project_name / "Download")
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _video_output_dir(self):
        p = self._output_root_dir() / "video"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _image_output_dir(self):
        p = self._output_root_dir() / "image"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _load_auth_config(self):
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

    def _load_text_prompts(self):
        if self.project_data.get("_use_project_prompts"):
            prompts_root = self.project_data.get("prompts", {}) if isinstance(self.project_data.get("prompts"), dict) else {}
            items = prompts_root.get("character_sync", [])
            if not items:
                items = prompts_root.get("text_to_video", [])
        else:
            test_file = WORKFLOWS_DIR / self.project_name / "test.json"
            items = []
            if test_file.exists():
                try:
                    data = json.loads(test_file.read_text(encoding="utf-8"))
                    prompts_root = data.get("prompts", {}) if isinstance(data, dict) else {}
                    items = prompts_root.get("character_sync", []) or prompts_root.get("text_to_video", [])
                except Exception:
                    items = []

        out = []
        for idx, item in enumerate(list(items or []), start=1):
            if not isinstance(item, dict):
                continue
            prompt_text = str(item.get("prompt") or item.get("description") or "").strip()
            if not prompt_text:
                continue
            out.append({"id": str(item.get("id") or idx), "prompt": prompt_text})
        return out

    def _load_character_profiles(self):
        candidates = []

        roots = []
        pdata_chars = self.project_data.get("characters")
        if isinstance(pdata_chars, list):
            roots.extend(pdata_chars)
        pdata_chars2 = self.project_data.get("character_profiles")
        if isinstance(pdata_chars2, list):
            roots.extend(pdata_chars2)

        test_file = WORKFLOWS_DIR / self.project_name / "test.json"
        if test_file.exists():
            try:
                data = json.loads(test_file.read_text(encoding="utf-8"))
                chars = data.get("characters")
                if isinstance(chars, list):
                    roots.extend(chars)
            except Exception:
                pass

        seen = set()
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
            candidates.append({"name": name, "name_key": key, "path": path})

        return candidates

    def _get_prompt_text(self, prompt_id):
        for item in self._load_text_prompts():
            if str(item.get("id")) == str(prompt_id):
                return str(item.get("prompt") or "")
        return ""

    async def _init_token_collector(self, project_link, chrome_userdata_root, profile_name, clear_data_interval, idle_timeout, token_timeout):
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
            keep_chrome_open=False,
            mode="video",
        )


SyncCharacterWorkflow = CharacterSyncWorkflow
TextToVideoWorkflow = CharacterSyncWorkflow
