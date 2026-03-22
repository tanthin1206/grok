from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from workflow_run_control import set_control_providers


@dataclass
class WorkflowQueueItem:
    mode_key: str
    rows: list[int]
    label: str = ""
    retry_round: int = 0


class WorkflowRunWorker:
    def __init__(
        self,
        start_job_callback: Callable[[WorkflowQueueItem], bool],
        stop_active_callback: Callable[[], None],
        log_callback: Callable[[str], None] | None = None,
        get_running_count_callback: Callable[[], int] | None = None,
        get_max_in_flight_callback: Callable[[], int] | None = None,
        request_retry_rows_callback: Callable[[str, list[int], int], list[int]] | None = None,
    ) -> None:
        self._start_job_callback = start_job_callback
        self._stop_active_callback = stop_active_callback
        self._log_callback = log_callback
        self._get_running_count_callback = get_running_count_callback
        self._get_max_in_flight_callback = get_max_in_flight_callback
        self._request_retry_rows_callback = request_retry_rows_callback

        self._queue: list[WorkflowQueueItem] = []
        self._active_items: list[WorkflowQueueItem] = []
        self._stopping = False

        set_control_providers(self._get_running_video_count, self._get_max_in_flight)

    def _get_running_video_count(self) -> int:
        if not callable(self._get_running_count_callback):
            return 0
        try:
            return max(0, int(self._get_running_count_callback()))
        except Exception:
            return 0

    def _get_max_in_flight(self) -> int:
        if not callable(self._get_max_in_flight_callback):
            return 1
        try:
            return max(1, int(self._get_max_in_flight_callback()))
        except Exception:
            return 1

    def _log(self, message: str) -> None:
        if callable(self._log_callback):
            try:
                self._log_callback(str(message or ""))
            except Exception:
                pass

    def enqueue(self, item: WorkflowQueueItem) -> int:
        if not isinstance(item, WorkflowQueueItem):
            return self.pending_count()
        if not item.rows:
            return self.pending_count()
        self._queue.append(item)
        return self.pending_count()

    def pending_count(self) -> int:
        return int(len(self._queue))

    def is_busy(self) -> bool:
        return bool(self._active_items or self._queue)

    def is_stopping(self) -> bool:
        return bool(self._stopping)

    def stop_all(self) -> None:
        self._stopping = True
        self._queue.clear()
        self._active_items.clear()
        try:
            self._stop_active_callback()
        except Exception:
            pass

    def on_run_state_changed(self, running: bool) -> None:
        if running:
            return

        finished_item = self._active_items.pop(0) if self._active_items else None

        if self._stopping:
            self._stopping = False
            self._queue.clear()
            self._log("🧹 Đã dừng toàn bộ và xóa hàng chờ")
            return

        if isinstance(finished_item, WorkflowQueueItem):
            self._log(f"✅ Worker hoàn tất: {finished_item.label or finished_item.mode_key} ({len(finished_item.rows)} dòng)")
            retry_rows: list[int] = []
            if callable(self._request_retry_rows_callback):
                try:
                    retry_rows = [int(r) for r in (self._request_retry_rows_callback(
                        str(finished_item.mode_key or ""),
                        list(finished_item.rows or []),
                        int(finished_item.retry_round or 0),
                    ) or [])]
                except Exception:
                    retry_rows = []
            if retry_rows:
                retry_item = WorkflowQueueItem(
                    mode_key=str(finished_item.mode_key or ""),
                    rows=retry_rows,
                    label=str(finished_item.label or finished_item.mode_key or "workflow"),
                    retry_round=int(finished_item.retry_round or 0) + 1,
                )
                self._queue.append(retry_item)
                self._log(
                    f"🔁 Worker xếp retry lượt {retry_item.retry_round}: "
                    f"{retry_item.label} ({len(retry_rows)} dòng)"
                )

        self.ensure_started()

    def ensure_started(self) -> None:
        if self._stopping:
            return
        self._start_next()

    def clear(self) -> None:
        self._queue.clear()
        self._active_items.clear()
        self._stopping = False

    def _start_next(self) -> None:
        if self._stopping:
            return
        if self._active_items:
            return
        if self._get_running_video_count() > 0:
            return
        if not self._queue:
            return

        next_item = self._queue.pop(0)
        started = False
        try:
            started = bool(self._start_job_callback(next_item))
        except Exception:
            started = False
        if started:
            self._active_items.append(next_item)
            self._log(f"▶️ Bắt đầu: {next_item.label or next_item.mode_key} ({len(next_item.rows)} dòng)")
            return
        self._log(f"⚠️ Bỏ qua job lỗi: {next_item.label or next_item.mode_key}")
