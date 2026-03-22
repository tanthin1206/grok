from __future__ import annotations

import threading
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from grok_chrome_manager import kill_profile_chrome, resolve_profile_dir
from grok_workflow_image_to_video import run_image_to_video_jobs
from grok_workflow_text_to_video import run_text_to_video_jobs


def _kill_profile_chrome_async() -> None:
    def _kill() -> None:
        try:
            kill_profile_chrome(resolve_profile_dir())
        except Exception:
            pass

    try:
        t = threading.Thread(target=_kill, daemon=True)
        t.start()
    except Exception:
        pass


class GrokTextToVideoWorker(QThread):
    log_message = pyqtSignal(str)
    status_updated = pyqtSignal(dict)
    video_updated = pyqtSignal(dict)
    automation_complete = pyqtSignal()

    def __init__(
        self,
        prompts: list[str],
        prompt_ids: list[str],
        aspect_ratio: str,
        video_length_seconds: int,
        resolution_name: str,
        output_dir: str,
        max_concurrency: int,
        offscreen_chrome: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._prompts = [str(p or "").strip() for p in (prompts or [])]
        self._prompt_ids = [str(pid or "").strip() for pid in (prompt_ids or [])]
        self._aspect_ratio = str(aspect_ratio or "9:16")
        self._video_length_seconds = int(video_length_seconds or 6)
        if self._video_length_seconds not in {6, 10}:
            self._video_length_seconds = 6
        self._resolution_name = str(resolution_name or "480p")
        if self._resolution_name not in {"480p", "720p"}:
            self._resolution_name = "480p"
        self._output_dir = str(output_dir or "").strip()
        self._max_concurrency = max(1, int(max_concurrency or 1))
        self._offscreen_chrome = bool(offscreen_chrome)
        self._stop_event = threading.Event()
        self._last_progress_bucket: dict[int, int] = {}

    def stop(self) -> None:
        self._stop_event.set()
        _kill_profile_chrome_async()
        try:
            self.requestInterruption()
        except Exception:
            pass

    def _emit_log(self, message: str) -> None:
        try:
            self.log_message.emit(str(message or ""))
        except Exception:
            pass

    def _safe_prompt_id(self, idx: int) -> str:
        try:
            if 0 <= int(idx) < len(self._prompt_ids):
                pid = str(self._prompt_ids[int(idx)] or "").strip()
                if pid:
                    return pid
        except Exception:
            pass
        return str(int(idx) + 1)

    def _on_status(self, idx: int, text: str) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        payload = {
            "prompt_id": prompt_id,
            "index": int(idx),
            "status_text": str(text or "").strip(),
        }
        try:
            self.status_updated.emit(payload)
        except Exception:
            pass
        self._emit_log(f"[GROK-T2V #{prompt_id}] {payload['status_text']}")

    def _on_progress(self, idx: int, progress: int) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        pct = int(max(0, min(100, int(progress or 0))))
        payload = {
            "prompt_id": prompt_id,
            "index": int(idx),
            "progress": pct,
        }
        try:
            self.status_updated.emit(payload)
        except Exception:
            pass
        bucket = int(pct // 10)
        if self._last_progress_bucket.get(int(idx)) != bucket or pct in {0, 100}:
            self._last_progress_bucket[int(idx)] = bucket
            self._emit_log(f"[GROK-T2V #{prompt_id}] tiến độ {pct}%")

    def _on_video(self, idx: int, file_path: str) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        payload = {
            "_prompt_id": prompt_id,
            "prompt_idx": f"{prompt_id}_1",
            "status": "SUCCESSFUL",
            "video_path": str(file_path or "").strip(),
        }
        try:
            self.video_updated.emit(payload)
        except Exception:
            pass
        self._emit_log(f"[GROK-T2V #{prompt_id}] đã tải xong video")

    def run(self) -> None:
        try:
            if self._stop_event.is_set():
                self._emit_log("🛑 GROK workflow đã dừng.")
                return
            prompts = [p for p in self._prompts if p]
            if not prompts:
                self._emit_log("❌ GROK: Không có prompt hợp lệ để chạy.")
                return

            self._emit_log(f"🚀 Khởi động GROK Text to Video | prompts={len(prompts)}")
            run_text_to_video_jobs(
                prompts=prompts,
                aspect_ratio=self._aspect_ratio,
                video_length_seconds=int(self._video_length_seconds),
                resolution_name=str(self._resolution_name),
                max_concurrency=self._max_concurrency,
                download_dir=self._output_dir,
                offscreen_chrome=bool(self._offscreen_chrome),
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_progress=self._on_progress,
                on_video=self._on_video,
                on_info=self._emit_log,
            )
            if self._stop_event.is_set():
                self._emit_log("🛑 GROK workflow đã dừng.")
            else:
                self._emit_log("✅ GROK workflow hoàn tất.")
        except Exception as exc:
            self._emit_log(f"❌ Lỗi GROK workflow: {exc}")
        finally:
            try:
                self.automation_complete.emit()
            except Exception:
                pass


class GrokImageToVideoWorker(QThread):
    log_message = pyqtSignal(str)
    status_updated = pyqtSignal(dict)
    video_updated = pyqtSignal(dict)
    automation_complete = pyqtSignal()

    def __init__(
        self,
        items: list[dict],
        prompt_ids: list[str],
        aspect_ratio: str,
        video_length_seconds: int,
        resolution_name: str,
        output_dir: str,
        max_concurrency: int,
        offscreen_chrome: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._items = [x for x in (items or []) if isinstance(x, dict)]
        self._prompt_ids = [str(pid or "").strip() for pid in (prompt_ids or [])]
        self._aspect_ratio = str(aspect_ratio or "9:16")
        if self._aspect_ratio not in {"9:16", "16:9"}:
            self._aspect_ratio = "9:16"
        self._video_length_seconds = int(video_length_seconds or 6)
        if self._video_length_seconds not in {6, 10}:
            self._video_length_seconds = 6
        self._resolution_name = str(resolution_name or "480p")
        if self._resolution_name not in {"480p", "720p"}:
            self._resolution_name = "480p"
        self._output_dir = str(output_dir or "").strip()
        self._max_concurrency = max(1, int(max_concurrency or 1))
        self._offscreen_chrome = bool(offscreen_chrome)
        self._stop_event = threading.Event()
        self._last_progress_bucket: dict[int, int] = {}

    def stop(self) -> None:
        self._stop_event.set()
        _kill_profile_chrome_async()
        try:
            self.requestInterruption()
        except Exception:
            pass

    def _emit_log(self, message: str) -> None:
        try:
            self.log_message.emit(str(message or ""))
        except Exception:
            pass

    def _safe_prompt_id(self, idx: int) -> str:
        try:
            if 0 <= int(idx) < len(self._prompt_ids):
                pid = str(self._prompt_ids[int(idx)] or "").strip()
                if pid:
                    return pid
        except Exception:
            pass
        return str(int(idx) + 1)

    def _on_status(self, idx: int, text: str) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        payload = {
            "prompt_id": prompt_id,
            "index": int(idx),
            "status_text": str(text or "").strip(),
        }
        try:
            self.status_updated.emit(payload)
        except Exception:
            pass
        self._emit_log(f"[GROK-I2V #{prompt_id}] {payload['status_text']}")

    def _on_progress(self, idx: int, progress: int) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        pct = int(max(0, min(100, int(progress or 0))))
        payload = {
            "prompt_id": prompt_id,
            "index": int(idx),
            "progress": pct,
        }
        try:
            self.status_updated.emit(payload)
        except Exception:
            pass
        bucket = int(pct // 10)
        if self._last_progress_bucket.get(int(idx)) != bucket or pct in {0, 100}:
            self._last_progress_bucket[int(idx)] = bucket
            self._emit_log(f"[GROK-I2V #{prompt_id}] tiến độ {pct}%")

    def _on_video(self, idx: int, file_path: str) -> None:
        prompt_id = self._safe_prompt_id(int(idx))
        payload = {
            "_prompt_id": prompt_id,
            "prompt_idx": f"{prompt_id}_1",
            "status": "SUCCESSFUL",
            "video_path": str(file_path or "").strip(),
        }
        try:
            self.video_updated.emit(payload)
        except Exception:
            pass
        self._emit_log(f"[GROK-I2V #{prompt_id}] đã tải xong video")

    def run(self) -> None:
        try:
            if self._stop_event.is_set():
                self._emit_log("🛑 GROK Image to Video đã dừng.")
                return

            clean_items: list[dict[str, str]] = []
            for raw in self._items:
                image_path = str(raw.get("image_path") or raw.get("image_link") or "").strip()
                prompt = str(raw.get("prompt") or "").strip()
                if not image_path:
                    continue
                clean_items.append({"image_path": image_path, "prompt": prompt})

            if not clean_items:
                self._emit_log("❌ GROK Image to Video: Không có dữ liệu ảnh hợp lệ để chạy.")
                return

            self._emit_log(f"🚀 Khởi động GROK Image to Video | jobs={len(clean_items)}")
            run_image_to_video_jobs(
                items=clean_items,
                aspect_ratio=self._aspect_ratio,
                video_length_seconds=int(self._video_length_seconds),
                resolution_name=str(self._resolution_name),
                max_concurrency=self._max_concurrency,
                download_dir=self._output_dir,
                offscreen_chrome=bool(self._offscreen_chrome),
                stop_event=self._stop_event,
                on_status=self._on_status,
                on_progress=self._on_progress,
                on_video=self._on_video,
                on_info=self._emit_log,
            )
            if self._stop_event.is_set():
                self._emit_log("🛑 GROK Image to Video đã dừng.")
            else:
                self._emit_log("✅ GROK Image to Video hoàn tất.")
        except Exception as exc:
            self._emit_log(f"❌ Lỗi GROK Image to Video: {exc}")
        finally:
            try:
                self.automation_complete.emit()
            except Exception:
                pass
