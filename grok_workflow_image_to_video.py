from __future__ import annotations

import asyncio
import datetime
import os
import re
import threading
from pathlib import Path
from typing import Callable

from grok_api_image_to_video import (
    ImageToVideoConfig,
    api_create_image_post_in_page,
    api_image_to_video_in_page,
    api_upload_image_in_page,
    api_upscale_video_in_page,
)
from grok_api_text_to_video import auto_discover_statsig_headers, download_mp4
from grok_chrome_manager import open_chrome_session, resolve_profile_dir


CDP_HOST = os.getenv("GROK_CDP_HOST", os.getenv("CDP_HOST", "127.0.0.1"))
CDP_PORT = int(os.getenv("GROK_CDP_PORT", os.getenv("CDP_PORT", "9223")))

WORKSPACE_DIR = Path(__file__).resolve().parent
CHROME_USER_DATA_ROOT = Path(
    os.getenv("GROK_CHROME_USER_DATA_ROOT", str(WORKSPACE_DIR / "chrome_user_data_grok"))
)
PROFILE_NAME = os.getenv("GROK_PROFILE_NAME", os.getenv("PROFILE_NAME", "PROFILE_1"))
USER_DATA_DIR = resolve_profile_dir(PROFILE_NAME)
CACHE_PATH = Path(os.getenv("GROK_CACHE_PATH", str(WORKSPACE_DIR / "grok_cache.json")))

STREAM_TIMEOUT_SECONDS = int(os.getenv("GROK_STREAM_TIMEOUT", "300"))
DOWNLOAD_DIR = Path(os.getenv("GROK_DOWNLOAD_DIR", str(WORKSPACE_DIR / "downloads")))
DOWNLOAD_TIMEOUT_MS = int(os.getenv("GROK_DOWNLOAD_TIMEOUT_MS", "180000"))
JOB_HARD_TIMEOUT_SECONDS = int(os.getenv("GROK_JOB_HARD_TIMEOUT_SECONDS", "420"))

StatusCallback = Callable[[int, str], None]
ProgressCallback = Callable[[int, int], None]
VideoCallback = Callable[[int, str], None]
InfoCallback = Callable[[str], None]


def _safe_call(cb, *args) -> None:
    try:
        if cb:
            cb(*args)
    except Exception:
        pass


def _safe_filename(value: str, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    raw = raw.strip("._ ")
    return raw or fallback


def _build_unique_video_name(prompt_index: int, prompt: str | None, image_path: str | None) -> str:
    try:
        pid = f"{int(prompt_index):03d}"
    except Exception:
        pid = "000"
    prompt_part = _safe_filename(str(prompt or "")[:30], "prompt")
    image_part = _safe_filename(Path(str(image_path or "")).stem[:30], "image")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = f"{pid}_{image_part}_{prompt_part}_{ts}"
    if len(base) > 180:
        base = base[:180].rstrip("._- ")
    return f"{base}.mp4"


async def _run_jobs_async_ui(
    items: list[dict[str, str]],
    cfg: ImageToVideoConfig,
    max_concurrency: int,
    on_status: StatusCallback | None,
    on_progress: ProgressCallback | None,
    on_video: VideoCallback | None,
    on_info: InfoCallback | None,
    stop_event: threading.Event | None = None,
    offscreen_chrome: bool = False,
) -> None:
    def _stop_requested() -> bool:
        try:
            return bool(stop_event is not None and stop_event.is_set())
        except Exception:
            return False

    async def _await_with_stop(coro, *, step_name: str, idx: int | None = None, check_interval: float = 0.2):
        task = asyncio.create_task(coro)
        interval = max(0.05, float(check_interval or 0.2))
        try:
            while True:
                if _stop_requested():
                    try:
                        task.cancel()
                    except Exception:
                        pass
                    await asyncio.gather(task, return_exceptions=True)
                    if isinstance(idx, int):
                        _safe_call(on_status, idx, "Stop")
                    _safe_call(on_info, f"🛑 Dừng ở bước {step_name}" + (f" (job {idx+1})" if isinstance(idx, int) else ""))
                    raise asyncio.CancelledError()
                if task.done():
                    return await task
                await asyncio.sleep(interval)
        finally:
            if _stop_requested() and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass

    if _stop_requested():
        _safe_call(on_info, "🛑 Đã nhận STOP trước khi mở Chrome GROK")
        return

    session = await open_chrome_session(
        host=CDP_HOST,
        port=CDP_PORT,
        user_data_dir=USER_DATA_DIR,
        start_url="https://grok.com/",
        cdp_wait_seconds=30,
        offscreen=bool(offscreen_chrome),
    )

    try:
        if _stop_requested():
            _safe_call(on_info, "🛑 Đã nhận STOP, thoát trước khi khởi tạo page")
            return

        context = session.context
        page = None
        for candidate in list(context.pages):
            try:
                if not candidate.is_closed() and "grok.com" in (candidate.url or ""):
                    page = candidate
                    break
            except Exception:
                continue
        if page is None:
            page = await context.new_page()

        try:
            await _await_with_stop(
                page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=30000),
                step_name="mở trang GROK",
            )
        except Exception:
            pass

        if _stop_requested():
            _safe_call(on_info, "🛑 Đã nhận STOP sau khi mở trang GROK")
            return

        _safe_call(on_info, "Lấy headers")
        headers = await _await_with_stop(
            auto_discover_statsig_headers(page, CACHE_PATH, PROFILE_NAME, force=True, persist=False),
            step_name="lấy headers",
        )

        async def js_progress(payload):
            if _stop_requested() or not isinstance(payload, dict):
                return
            idx = payload.get("index")
            pct = payload.get("progress")
            if isinstance(idx, int) and isinstance(pct, (int, float)):
                _safe_call(on_progress, idx, int(max(0, min(100, pct))))

        async def js_log(payload):
            if _stop_requested() or not isinstance(payload, dict):
                return
            idx = payload.get("index")
            msg = str(payload.get("message") or "")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if not isinstance(idx, int):
                return

            if msg == "conversation_start":
                _safe_call(on_status, idx, "Đang Tạo video")
            elif msg == "conversation_status":
                _safe_call(on_status, idx, "Đang Tạo video")
            elif msg == "video_generated":
                _safe_call(on_progress, idx, 100)
                _safe_call(on_status, idx, "Tạo xong")
            elif msg in {"upscale_attempt", "upscale_retry"}:
                _safe_call(on_status, idx, "Đang Tải video")
            elif msg == "upscale_success":
                _safe_call(on_status, idx, "Tải HD")
            elif msg.endswith("_error"):
                _safe_call(on_status, idx, f"Lỗi {msg}")
            elif data:
                _safe_call(on_info, f"[GROK-I2V {idx+1}] {msg}: {data}")

        try:
            await page.expose_function("py_progress", js_progress)
        except Exception:
            pass
        try:
            await page.expose_function("py_log", js_log)
        except Exception:
            pass

        sem = asyncio.Semaphore(max(1, int(max_concurrency or 1)))
        headers_lock = asyncio.Lock()

        async def run_one_job(idx: int, job: dict[str, str]) -> None:
            nonlocal headers
            if _stop_requested():
                _safe_call(on_status, idx, "Stop")
                return

            image_path = Path(str(job.get("image_path") or "").strip())
            prompt = str(job.get("prompt") or "").strip()
            _safe_call(on_info, f"[GROK-I2V {idx+1}] bắt đầu job | image={image_path.name}")
            _safe_call(
                on_info,
                f"[GROK-I2V {idx+1}] config: aspect={getattr(cfg, 'aspect_ratio', '9:16')} | "
                f"length={getattr(cfg, 'video_length_seconds', 6)} | res={getattr(cfg, 'resolution_name', '480p')}",
            )

            if not image_path.is_file():
                _safe_call(on_status, idx, "Lỗi ảnh")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] ảnh không tồn tại: {image_path}")
                return

            async with sem:
                if _stop_requested():
                    _safe_call(on_status, idx, "Stop")
                    return

                _safe_call(on_status, idx, "Xếp hàng")
                _safe_call(on_status, idx, "Upload ảnh")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] upload ảnh...")

                upload_result = await _await_with_stop(
                    api_upload_image_in_page(page, image_path, headers),
                    step_name="upload ảnh",
                    idx=idx,
                )
                file_metadata_id = str(upload_result.get("fileMetadataId") or "").strip()
                file_uri = str(upload_result.get("fileUri") or "").strip()
                parent_post_id = str(upload_result.get("parentPostId") or "").strip()
                if not file_metadata_id:
                    _safe_call(on_status, idx, "Lỗi upload")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] upload lỗi: {upload_result}")
                    return
                _safe_call(
                    on_info,
                    f"[GROK-I2V {idx+1}] upload xong | fileMetadataId={file_metadata_id[:10]}... | "
                    f"fileUri={'yes' if bool(file_uri) else 'no'} | parentPostId={'yes' if bool(parent_post_id) else 'no'}",
                )

                _safe_call(on_status, idx, "Tạo post")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] tạo image post...")
                media_url = str(file_uri or "").strip()
                if media_url and not (media_url.startswith("http://") or media_url.startswith("https://")):
                    media_url = f"https://assets.grok.com/{media_url.lstrip('/')}"

                create_result = await _await_with_stop(
                    api_create_image_post_in_page(
                        page,
                        media_url=media_url,
                        statsig_headers=headers,
                        job_index=idx,
                    ),
                    step_name="tạo post",
                    idx=idx,
                )
                create_status = int(create_result.get("status") or 0)
                created_post_id = str(create_result.get("postId") or "").strip()
                created_media_url = str(create_result.get("mediaUrl") or "").strip()
                if not created_post_id:
                    _safe_call(on_status, idx, "Lỗi tạo post")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] create post lỗi: {create_result}")
                    return
                parent_post_id = created_post_id
                if created_media_url:
                    file_uri = created_media_url
                _safe_call(
                    on_info,
                    f"[GROK-I2V {idx+1}] create post xong | status={create_status} | postId={created_post_id}",
                )

                _safe_call(on_status, idx, "Đang Tạo video")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] gửi request tạo video...")
                video_result = await _await_with_stop(
                    api_image_to_video_in_page(
                        page,
                        prompt=prompt,
                        file_metadata_id=file_metadata_id,
                        file_uri=file_uri,
                        parent_post_id=parent_post_id,
                        statsig_headers=headers,
                        cfg=cfg,
                        timeout_seconds=STREAM_TIMEOUT_SECONDS,
                        job_index=idx,
                    ),
                    step_name="gọi conversation/new",
                    idx=idx,
                )

                convo_status = int(video_result.get("status") or 0)
                _safe_call(on_info, f"[GROK-I2V {idx+1}] phản hồi tạo video: status={convo_status}")
                if convo_status == 403:
                    async with headers_lock:
                        _safe_call(on_info, f"Refresh headers (job {idx + 1})")
                        headers = await _await_with_stop(
                            auto_discover_statsig_headers(
                                page,
                                CACHE_PATH,
                                PROFILE_NAME,
                                force=True,
                                persist=False,
                            ),
                            step_name="refresh headers",
                            idx=idx,
                        )
                    video_result = await _await_with_stop(
                        api_image_to_video_in_page(
                            page,
                            prompt=prompt,
                            file_metadata_id=file_metadata_id,
                            file_uri=file_uri,
                            parent_post_id=parent_post_id,
                            statsig_headers=headers,
                            cfg=cfg,
                            timeout_seconds=STREAM_TIMEOUT_SECONDS,
                            job_index=idx,
                        ),
                        step_name="gọi lại conversation/new",
                        idx=idx,
                    )
                    convo_status = int(video_result.get("status") or 0)

                if _stop_requested():
                    _safe_call(on_status, idx, "Stop")
                    return

                last_event = video_result.get("lastEvent") if isinstance(video_result.get("lastEvent"), dict) else {}
                progress = int(last_event.get("progress") or 0)
                video_id = str(last_event.get("videoId") or "").strip()
                video_url = str(last_event.get("videoUrl") or "").strip()
                stream_resolution = str(last_event.get("resolutionName") or "").strip().lower()
                is_720p = (
                    str(getattr(cfg, "resolution_name", "") or "").strip().lower() == "720p"
                    or stream_resolution == "720p"
                )
                generated_id = str(video_result.get("generatedId") or video_id or parent_post_id or "").strip()
                generated_direct_url = str(video_result.get("directVideoUrl") or "").strip()
                generated_hd_candidate = str(video_result.get("hdVideoUrlCandidate") or "").strip()
                _safe_call(on_info, f"[GROK-I2V {idx+1}] tiến độ cuối: {progress}%")
                if generated_direct_url:
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] direct URL có sẵn từ thành phần users/generated")

                has_generation_signal = bool(
                    video_url
                    or video_id
                    or generated_direct_url
                    or generated_hd_candidate
                    or progress >= 95
                )
                if not has_generation_signal:
                    _safe_call(on_status, idx, "Lỗi chưa tạo xong")
                    _safe_call(
                        on_info,
                        f"[GROK-I2V {idx+1}] chưa có tín hiệu video hợp lệ từ stream (progress={progress}) -> dừng tải",
                    )
                    return

                if convo_status != 200:
                    _safe_call(on_status, idx, f"Lỗi {convo_status or progress}")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] job lỗi | status={convo_status} progress={progress}")
                    return

                if progress < 100:
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] server chưa báo 100%, thử lấy video bằng videoId/videoUrl")

                source_url = ""
                candidate_urls: list[str] = []
                if is_720p:
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] bỏ qua upscale vì video đã 720p")
                elif video_id:
                    _safe_call(on_status, idx, "Tải HD")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] upscale videoId={video_id[:10]}...")
                    upscale_result = await _await_with_stop(
                        api_upscale_video_in_page(
                            page,
                            video_id=video_id,
                            statsig_headers=headers,
                            job_index=idx,
                            max_retries=3,
                        ),
                        step_name="upscale",
                        idx=idx,
                    )
                    source_url = str(upscale_result.get("hdMediaUrl") or "").strip()
                    upscale_status = upscale_result.get("status")
                    upscale_error = str(upscale_result.get("error") or "").strip()
                    upscale_data = upscale_result.get("data")
                    _safe_call(
                        on_info,
                        f"[GROK-I2V {idx+1}] upscale status={upscale_status} | "
                        f"hdMediaUrl={'yes' if bool(source_url) else 'no'} | error={upscale_error or '-'} | data={upscale_data}",
                    )
                    if source_url:
                        candidate_urls.append(source_url)
                    elif generated_hd_candidate:
                        _safe_call(on_info, f"[GROK-I2V {idx+1}] upscale không trả URL, dùng link HD ghép sẵn")
                        candidate_urls.append(generated_hd_candidate)
                    else:
                        _safe_call(on_info, f"[GROK-I2V {idx+1}] upscale lỗi/không có HD URL, fallback video direct")

                if generated_direct_url:
                    candidate_urls.append(generated_direct_url)

                if video_url:
                    candidate_urls.append(video_url)

                if video_id:
                    candidate_urls.append(
                        f"https://imagine-public.x.ai/imagine-public/share-videos/{video_id}.mp4?cache=1&dl=1"
                    )
                if parent_post_id:
                    candidate_urls.append(
                        f"https://imagine-public.x.ai/imagine-public/share-videos/{parent_post_id}.mp4?cache=1&dl=1"
                    )

                uid = ""
                try:
                    if file_uri:
                        parts = [p for p in str(file_uri).split("/") if p]
                        if len(parts) >= 2 and parts[0] == "users":
                            uid = str(parts[1])
                except Exception:
                    uid = ""

                if uid and generated_id:
                    candidate_urls.append(
                        f"https://assets.grok.com/users/{uid}/generated/{generated_id}/generated_video_hd.mp4"
                    )
                    candidate_urls.append(
                        f"https://assets.grok.com/users/{uid}/generated/{generated_id}/generated_video.mp4?cache=1&dl=1"
                    )

                dedup_urls: list[str] = []
                seen_urls: set[str] = set()
                for url in candidate_urls:
                    u = str(url or "").strip()
                    if not u or u in seen_urls:
                        continue
                    dedup_urls.append(u)
                    seen_urls.add(u)

                if not dedup_urls:
                    _safe_call(on_status, idx, "Lỗi URL")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] không có URL tải nào khả dụng")
                    return

                out_path = DOWNLOAD_DIR / _build_unique_video_name(idx + 1, prompt, str(image_path))
                _safe_call(on_status, idx, "Đang Tải video")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] tải video về: {out_path.name}")
                ok = False
                for attempt, url in enumerate(dedup_urls, start=1):
                    if _stop_requested():
                        _safe_call(on_status, idx, "Stop")
                        _safe_call(on_info, f"[GROK-I2V {idx+1}] dừng khi đang thử URL tải")
                        return
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] thử URL tải #{attempt}: {url}")
                    ok = await _await_with_stop(
                        download_mp4(context, url, out_path, timeout_ms=DOWNLOAD_TIMEOUT_MS),
                        step_name="tải video",
                        idx=idx,
                    )
                    if ok:
                        _safe_call(on_info, f"[GROK-I2V {idx+1}] tải thành công với URL #{attempt}")
                        break
                if not ok:
                    _safe_call(on_status, idx, "Lỗi tải")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] tải thất bại với tất cả URL fallback")
                    return

                _safe_call(on_video, idx, str(out_path))
                _safe_call(on_status, idx, "Hoàn thành")
                _safe_call(on_info, f"[GROK-I2V {idx+1}] hoàn thành")

        async def run_one_job_guarded(idx: int, job: dict[str, str]) -> None:
            timeout_seconds = max(60, int(JOB_HARD_TIMEOUT_SECONDS or 420))
            try:
                await asyncio.wait_for(run_one_job(idx, job), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                if not _stop_requested():
                    _safe_call(on_status, idx, "Lỗi timeout")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] timeout sau {timeout_seconds}s, bỏ qua job để tránh kẹt luồng")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _stop_requested():
                    _safe_call(on_status, idx, "Lỗi")
                    _safe_call(on_info, f"[GROK-I2V {idx+1}] lỗi runtime: {exc}")

        tasks = [asyncio.create_task(run_one_job_guarded(i, item)) for i, item in enumerate(items)]

        async def _wait_stop() -> bool:
            if stop_event is None:
                return False
            while True:
                if _stop_requested():
                    return True
                await asyncio.sleep(0.2)

        if tasks:
            if stop_event is None:
                await asyncio.gather(*tasks)
            else:
                stop_task = asyncio.create_task(_wait_stop())
                pending_tasks = list(tasks)
                try:
                    while pending_tasks:
                        done, _ = await asyncio.wait(
                            pending_tasks + [stop_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            for t in pending_tasks:
                                try:
                                    t.cancel()
                                except Exception:
                                    pass
                            for i in range(len(items)):
                                _safe_call(on_status, i, "Stop")
                            await asyncio.gather(*tasks, return_exceptions=True)
                            return
                        pending_tasks = [t for t in pending_tasks if not t.done()]
                    await asyncio.gather(*tasks)
                finally:
                    try:
                        stop_task.cancel()
                    except Exception:
                        pass
                    await asyncio.gather(stop_task, return_exceptions=True)

    finally:
        await session.close()


def run_image_to_video_jobs(
    items: list[dict[str, str]],
    aspect_ratio: str,
    video_length_seconds: int,
    resolution_name: str,
    max_concurrency: int,
    download_dir: str | None = None,
    offscreen_chrome: bool = False,
    stop_event: threading.Event | None = None,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
    on_video: VideoCallback | None = None,
    on_info: InfoCallback | None = None,
) -> None:
    global DOWNLOAD_DIR

    clean_items: list[dict[str, str]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        image_path = str(raw.get("image_path") or "").strip()
        if not image_path:
            continue
        clean_items.append({"image_path": image_path, "prompt": str(raw.get("prompt") or "").strip()})

    if not clean_items:
        raise ValueError("Danh sách GROK Image to Video rỗng.")

    if isinstance(download_dir, str) and download_dir.strip():
        DOWNLOAD_DIR = Path(download_dir.strip()) / "grok_video"
    else:
        DOWNLOAD_DIR = Path(DOWNLOAD_DIR) / "grok_video"
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    runtime_resolution_name = str(resolution_name or "480p")
    if runtime_resolution_name not in {"480p", "720p"}:
        runtime_resolution_name = "480p"

    runtime_aspect_ratio = str(aspect_ratio or "9:16").strip()
    if runtime_aspect_ratio not in {"9:16", "16:9"}:
        runtime_aspect_ratio = "9:16"

    cfg = ImageToVideoConfig(
        aspect_ratio=runtime_aspect_ratio,
        video_length_seconds=int(video_length_seconds or 6),
        resolution_name=runtime_resolution_name,
    )

    asyncio.run(
        _run_jobs_async_ui(
            items=clean_items,
            cfg=cfg,
            max_concurrency=max_concurrency,
            on_status=on_status,
            on_progress=on_progress,
            on_video=on_video,
            on_info=on_info,
            stop_event=stop_event,
            offscreen_chrome=bool(offscreen_chrome),
        )
    )
