from __future__ import annotations

from threading import Lock
from typing import Callable


_lock = Lock()
_get_running_count_cb: Callable[[], int] | None = None
_get_max_in_flight_cb: Callable[[], int] | None = None


def set_control_providers(
    get_running_count_cb: Callable[[], int] | None,
    get_max_in_flight_cb: Callable[[], int] | None,
) -> None:
    global _get_running_count_cb, _get_max_in_flight_cb
    with _lock:
        _get_running_count_cb = get_running_count_cb if callable(get_running_count_cb) else None
        _get_max_in_flight_cb = get_max_in_flight_cb if callable(get_max_in_flight_cb) else None


def get_running_video_count(default_value: int = 0) -> int:
    with _lock:
        cb = _get_running_count_cb
    if not callable(cb):
        return int(default_value)
    try:
        return max(0, int(cb()))
    except Exception:
        return int(default_value)


def get_max_in_flight(default_value: int = 1) -> int:
    with _lock:
        cb = _get_max_in_flight_cb
    if not callable(cb):
        return max(1, int(default_value))
    try:
        return max(1, int(cb()))
    except Exception:
        return max(1, int(default_value))
