import asyncio
import datetime
import os
import re
import sys
import time
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from grok_api_text_to_video import (
  VideoGenConfig,
  api_run_jobs_in_page,
  auto_discover_statsig_headers,
  download_mp4,
)
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

PROMPT = os.getenv("GROK_PROMPT", "con chó đang chạy bộ trên bãi biển --mode=custom")
PROMPTS_RAW = os.getenv("GROK_PROMPTS", "").strip()
VIDEO_COUNT = int(os.getenv("GROK_COUNT", "5"))

VIDEO_ASPECT_RATIO = os.getenv("GROK_VIDEO_ASPECT_RATIO", "9:16")
VIDEO_LENGTH_SECONDS = int(os.getenv("GROK_VIDEO_LENGTH", "6"))
VIDEO_RESOLUTION = os.getenv("GROK_VIDEO_RESOLUTION", "480p")

STREAM_TIMEOUT_SECONDS = int(os.getenv("GROK_STREAM_TIMEOUT", "300"))

AUTO_REFRESH_HEADERS = os.getenv("GROK_AUTO_REFRESH_HEADERS", "1").strip() not in {"0", "false", "False"}

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


def _build_assets_hd_url(media_url: str | None, parent_post_id: str | None) -> str | None:
  raw = (media_url or "").strip()
  if not raw:
    return None

  # try extracting /users/<uid>/generated/<vid>/ from any source URL
  m = re.search(r"/users/([^/]+)/generated/([^/]+)/", raw)
  if m:
    user_id = m.group(1).strip()
    video_id = m.group(2).strip()
    if user_id and video_id:
      return f"https://assets.grok.com/users/{user_id}/generated/{video_id}/generated_video_hd.mp4"

  # fallback: if only parent id can be used as generated id, keep same path style when possible
  parsed = urlparse(raw)
  path_parts = [p for p in parsed.path.split("/") if p]
  if len(path_parts) >= 2 and path_parts[0] == "users":
    user_id = path_parts[1]
    video_id = (parent_post_id or "").strip() or (path_parts[3] if len(path_parts) > 3 else "")
    if user_id and video_id:
      return f"https://assets.grok.com/users/{user_id}/generated/{video_id}/generated_video_hd.mp4"
  return None


def _log_step(title: str) -> None:
  print(f"\n===== {title} =====")


def _safe_filename(value: str, fallback: str) -> str:
  raw = str(value or "").strip()
  if not raw:
    return fallback
  # Windows-safe filename: keep letters/numbers/._- and replace others with '_'
  raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
  raw = raw.strip("._ ")
  return raw or fallback


def _build_unique_video_name(prompt_index: int, prompt: str | None) -> str:
  """Build a unique, Windows-safe mp4 filename.

  Required format: <promptID>_<prompt>_<timestamp>.mp4
  - promptID is 001/002/003...
  - prompt is truncated for filesystem safety
  """
  try:
    pid = f"{int(prompt_index):03d}"
  except Exception:
    pid = "000"

  pr_raw = str(prompt or "").strip().replace("\r", " ").replace("\n", " ")
  pr_short = (pr_raw[:40] if pr_raw else "")
  pr = _safe_filename(pr_short, "prompt")

  # Include microseconds to avoid collisions when downloading concurrently.
  ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

  ext = ".mp4"
  base = f"{pid}_{pr}_{ts}"
  # Keep filenames reasonably short for Windows path limits.
  max_base_len = 200 - len(ext)
  if len(base) > max_base_len:
    base = base[:max_base_len].rstrip("._- ")
  return base + ext


def _interactive_get_prompts() -> list[str]:
  """Hỏi người dùng số video và prompt cho từng video."""
  _log_step("NHẬP THÔNG TIN VIDEO")
  
  # Hỏi số lượng video
  while True:
    try:
      count_str = input("Số video muốn tạo (mặc định 1): ").strip()
      if not count_str:
        count = 1
        break
      count = int(count_str)
      if count < 1:
        print("Số video phải >= 1")
        continue
      break
    except ValueError:
      print("Vui lòng nhập số nguyên.")
  
  print(f"\nSẽ tạo {count} video.")
  print("Nhập prompt cho từng video (ENTER để dùng lại prompt trước đó):\n")
  
  prompts: list[str] = []
  default_prompt = PROMPT
  
  for i in range(count):
    hint = f" [mặc định: {default_prompt[:50]}...]" if len(default_prompt) > 50 else f" [mặc định: {default_prompt}]"
    text = input(f"Prompt {i+1}/{count}{hint}: ").strip()
    if text:
      prompts.append(text)
      default_prompt = text  # Prompt tiếp theo mặc định dùng prompt vừa nhập
    else:
      prompts.append(default_prompt)
  
  print("\n📝 Danh sách prompt:")
  for i, p in enumerate(prompts, 1):
    print(f"  {i}. {p[:80]}{'...' if len(p) > 80 else ''}")
  
  return prompts


def _build_prompts_from_env() -> list[str]:
  """Build prompts từ biến môi trường (không interactive)."""
  prompts: list[str] = []
  if PROMPTS_RAW:
    prompts = [p.strip() for p in PROMPTS_RAW.split("|") if p.strip()]

  if not prompts:
    prompts = [PROMPT]

  if VIDEO_COUNT > 1:
    while len(prompts) < VIDEO_COUNT:
      prompts.append(prompts[-1])
    prompts = prompts[:VIDEO_COUNT]

  return prompts


async def run_workflow() -> None:
  # BƯỚC 1: Hỏi prompt TRƯỚC khi mở Chrome
  prompts = _interactive_get_prompts()
  
  cfg = VideoGenConfig(
    aspect_ratio=VIDEO_ASPECT_RATIO,
    video_length_seconds=VIDEO_LENGTH_SECONDS,
    resolution_name=VIDEO_RESOLUTION,
  )

  # BƯỚC 2: Mở Chrome để login
  _log_step("MỞ CHROME ĐỂ ĐĂNG NHẬP")
  print(f"Profile: {PROFILE_NAME}")
  print(f"User data: {USER_DATA_DIR}")
  
  session = await open_chrome_session(
    host=CDP_HOST,
    port=CDP_PORT,
    user_data_dir=USER_DATA_DIR,
    start_url="https://grok.com/",
    cdp_wait_seconds=30,
  )

  try:
    context = session.context

    page = None
    try:
      for candidate in list(context.pages):
        if not candidate.is_closed() and "grok.com" in (candidate.url or ""):
          page = candidate
          break
    except Exception:
      page = None
    if page is None:
      page = await context.new_page()

    await page.set_viewport_size({"width": 1280, "height": 720})
    try:
      await page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
      pass

    # BƯỚC 3: Đợi user login xong
    _log_step("ĐĂNG NHẬP GROK")
    print("🔐 Hãy đăng nhập / vượt Cloudflare trên cửa sổ Chrome.")
    print("   (Nếu đã login sẵn thì bỏ qua)")
    await asyncio.to_thread(input, "\n✅ Đăng nhập xong rồi thì nhấn ENTER để bắt đầu tạo video... ")

    headers: dict = {}
    if AUTO_REFRESH_HEADERS:
      _log_step("AUTO DISCOVER HEADERS")
      headers = await auto_discover_statsig_headers(page, CACHE_PATH, PROFILE_NAME, force=True, persist=False)

    print(f"Số job: {len(prompts)}")

    progress_state: dict[int, int] = {i: 0 for i in range(len(prompts))}
    last_render = 0.0

    def render(force: bool = False) -> None:
      nonlocal last_render
      now = time.monotonic()
      if not force and now - last_render < 0.15:
        return
      last_render = now
      parts: list[str] = []
      for i in range(len(prompts)):
        pct = int(progress_state.get(i, 0) or 0)
        pct = max(0, min(100, pct))
        width = 14
        filled = int(round((pct / 100) * width))
        bar = "#" * filled + "-" * (width - filled)
        parts.append(f"{i+1}:[{bar}]{pct:3d}%")
      sys.stdout.write("\r" + " ".join(parts) + " " * 5)
      sys.stdout.flush()

    async def on_progress(payload):
      try:
        if isinstance(payload, dict):
          idx = payload.get("index")
          pct = payload.get("progress")
          if isinstance(idx, int) and isinstance(pct, (int, float)) and 0 <= idx < len(prompts):
            progress_state[idx] = int(pct)
            render()
      except Exception:
        pass

    async def on_log(payload):
      try:
        if not isinstance(payload, dict):
          return
        idx = payload.get("index")
        msg = payload.get("message")
        data = payload.get("data")
        if not isinstance(idx, int) or not (0 <= idx < len(prompts)):
          return
        if not isinstance(msg, str):
          msg = str(msg)

        # Print log on its own line, then re-render progress bars
        sys.stdout.write("\r" + " " * 120 + "\r")
        prefix = f"[{idx+1}/{len(prompts)}]"
        if msg == "create_post_start":
          print(f"{prefix} create post...")
        elif msg == "create_post_done":
          parent = (data or {}).get("parentPostId") if isinstance(data, dict) else None
          status = (data or {}).get("status") if isinstance(data, dict) else None
          print(f"{prefix} create done (status={status}) parentPostId={parent}")
        elif msg == "conversation_start":
          parent = (data or {}).get("parentPostId") if isinstance(data, dict) else None
          print(f"{prefix} generating video (parentPostId={parent})")
        elif msg == "conversation_status":
          status = (data or {}).get("status") if isinstance(data, dict) else None
          print(f"{prefix} convo status={status}")
        elif msg == "video_generated":
          print(f"{prefix} ✅ tạo video xong")
        elif msg == "upscale_start":
          print(f"{prefix} upscale...")
        elif msg == "upscale_done":
          status = (data or {}).get("status") if isinstance(data, dict) else None
          print(f"{prefix} ✅ upscale xong (status={status})")
        elif msg.endswith("_error"):
          print(f"{prefix} ❌ {msg}: {data}")
        else:
          print(f"{prefix} {msg} {data if data is not None else ''}")

        render(force=True)
      except Exception:
        pass

    _log_step("RUN JOBS (concurrent - single page)")

    # Expose callbacks (ignore if already exposed)
    try:
      await page.expose_function("py_progress", on_progress)
    except Exception:
      pass
    try:
      await page.expose_function("py_log", on_log)
    except Exception:
      pass

    # Run all jobs concurrently in ONE page (no extra tabs)
    results = await api_run_jobs_in_page(
      page,
      prompts=prompts,
      statsig_headers=headers,
      cfg=cfg,
      timeout_seconds=STREAM_TIMEOUT_SECONDS,
      index_offset=0,
    )

    # If anti-bot kicked in, refresh headers and retry once
    if any(isinstance(r, dict) and int(r.get("convoStatus") or 0) == 403 for r in (results or [])):
      _log_step("REFRESH HEADERS (403) + RETRY")
      headers = await auto_discover_statsig_headers(page, CACHE_PATH, PROFILE_NAME, force=True, persist=False)
      results = await api_run_jobs_in_page(
        page,
        prompts=prompts,
        statsig_headers=headers,
        cfg=cfg,
        timeout_seconds=STREAM_TIMEOUT_SECONDS,
        index_offset=0,
      )

    async def _upscale_again(video_id: str, statsig_headers: dict, job_index: int = 0) -> str | None:
      vid = str(video_id or "").strip()
      if not vid:
        return None
      payload = {
        "videoId": vid,
        "statsigHeaders": statsig_headers or {},
        "jobIndex": int(job_index),
      }
      try:
        res = await page.evaluate(
          """(async ({ videoId, statsigHeaders, jobIndex }) => {
            async function upscaleVideo(videoId, maxRetries = 3) {
              for (let attempt = 1; attempt <= maxRetries; attempt++) {
                try {
                  const res = await fetch('https://grok.com/rest/media/video/upscale', {
                    method: 'POST',
                    headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
                    credentials: 'include',
                    body: JSON.stringify({ videoId }),
                  });
                  const data = await res.json().catch(() => null);
                  const hdMediaUrl = (data && data.hdMediaUrl) ? data.hdMediaUrl : null;
                  if (res.status === 200 && hdMediaUrl) {
                    return { status: res.status, hdMediaUrl, attempt };
                  }
                  if (attempt < maxRetries) {
                    await new Promise(r => setTimeout(r, 1500 * attempt));
                  }
                } catch (e) {
                  if (attempt < maxRetries) {
                    await new Promise(r => setTimeout(r, 1500 * attempt));
                  }
                }
              }
              return { status: 0, hdMediaUrl: null, attempt: maxRetries };
            }
            const up = await upscaleVideo(videoId, 3);
            return (up && up.hdMediaUrl) ? String(up.hdMediaUrl) : null;
          })""",
          payload,
        )
        return str(res).strip() if isinstance(res, str) and str(res).strip() else None
      except Exception:
        return None

    # Download HD videos (can run concurrently)
    async def download_one(i: int, job: dict) -> None:
      parent_id = job.get("parentPostId") if isinstance(job, dict) else None
      hd = job.get("hdMediaUrl") if isinstance(job, dict) else None
      if isinstance(hd, str) and hd.strip():
        sys.stdout.write("\r" + " " * 120 + "\r")
        print(f"[{i+1}/{len(prompts)}] download HD...")
        prompt_text = job.get("prompt") if isinstance(job, dict) else None
        if not isinstance(prompt_text, str) or not prompt_text.strip():
          prompt_text = prompts[i] if 0 <= i < len(prompts) else ""
        filename = _build_unique_video_name(i + 1, prompt_text)
        out_path = DOWNLOAD_DIR / filename
        ok = await download_mp4(context, hd.strip(), out_path, timeout_ms=DOWNLOAD_TIMEOUT_MS)
        if not ok and isinstance(parent_id, str) and parent_id.strip():
          # If download failed, request a fresh upscale URL and retry download.
          new_hd = await _upscale_again(parent_id.strip(), headers, job_index=i)
          if isinstance(new_hd, str) and new_hd.strip():
            ok = await download_mp4(context, new_hd.strip(), out_path, timeout_ms=DOWNLOAD_TIMEOUT_MS)
        sys.stdout.write("\r" + " " * 120 + "\r")
        if ok:
          print(f"[{i+1}/{len(prompts)}] ✅ download xong: {out_path}")
        else:
          print(f"[{i+1}/{len(prompts)}] ⚠️ download lỗi: {out_path}")
        render(force=True)

    dl_tasks = [
      asyncio.create_task(download_one(i, job))
      for i, job in enumerate(results or [])
      if isinstance(job, dict)
    ]
    if dl_tasks:
      await asyncio.gather(*dl_tasks)
    render(force=True)
    print()

    for idx, job in enumerate(results, start=1):
      print(f"\n--- JOB {idx}/{len(results)} ---")
      if not isinstance(job, dict):
        print(f"error: {job}")
        continue
      print(f"prompt={job.get('prompt')!r}")
      print(f"createStatus={job.get('createStatus')} parentPostId={job.get('parentPostId')}")
      print(f"convoStatus={job.get('convoStatus')} upscaleStatus={job.get('upscaleStatus')}")
      parent_id = job.get("parentPostId")
      if isinstance(parent_id, str) and parent_id.strip():
        print(f"🔗 Post URL: https://grok.com/imagine/post/{parent_id.strip()}")
      hd = job.get("hdMediaUrl")
      if isinstance(hd, str) and hd.strip():
        print(f"✅ HD download URL: {hd.strip()}")

  finally:
    await session.close()


def main() -> None:
  try:
    asyncio.run(run_workflow())
  except KeyboardInterrupt:
    print("\n⛔ Đã hủy (Ctrl+C).")


async def _run_jobs_async_ui(
  prompts: list[str],
  cfg: VideoGenConfig,
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

    if _stop_requested():
      _safe_call(on_info, "🛑 Đã nhận STOP sau bước lấy headers")
      return

    async def _upscale_again(video_id: str, statsig_headers: dict, job_index: int) -> str | None:
      if _stop_requested():
        return None
      vid = str(video_id or "").strip()
      if not vid:
        return None
      payload = {
        "videoId": vid,
        "statsigHeaders": statsig_headers or {},
        "jobIndex": int(job_index),
      }
      try:
        res = await _await_with_stop(
          page.evaluate(
            """(async ({ videoId, statsigHeaders, jobIndex }) => {
            async function upscaleVideo(videoId, maxRetries = 3) {
              for (let attempt = 1; attempt <= maxRetries; attempt++) {
                try {
                  const res = await fetch('https://grok.com/rest/media/video/upscale', {
                    method: 'POST',
                    headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
                    credentials: 'include',
                    body: JSON.stringify({ videoId }),
                  });
                  const data = await res.json().catch(() => null);
                  const hdMediaUrl = (data && data.hdMediaUrl) ? data.hdMediaUrl : null;
                  if (res.status === 200 && hdMediaUrl) {
                    return { status: res.status, hdMediaUrl, attempt };
                  }
                  if (attempt < maxRetries) {
                    await new Promise(r => setTimeout(r, 1500 * attempt));
                  }
                } catch (e) {
                  if (attempt < maxRetries) {
                    await new Promise(r => setTimeout(r, 1500 * attempt));
                  }
                }
              }
              return { status: 0, hdMediaUrl: null, attempt: maxRetries };
            }
            const up = await upscaleVideo(videoId, 3);
            return (up && up.hdMediaUrl) ? String(up.hdMediaUrl) : null;
          })""",
            payload,
          ),
          step_name="upscale lại",
          idx=job_index,
        )
        return str(res).strip() if isinstance(res, str) and str(res).strip() else None
      except Exception:
        return None

    async def js_progress(payload):
      if _stop_requested():
        return
      if not isinstance(payload, dict):
        return
      idx = payload.get("index")
      pct = payload.get("progress")
      if isinstance(idx, int) and isinstance(pct, (int, float)):
        _safe_call(on_progress, idx, int(max(0, min(100, pct))))

    async def js_log(payload):
      if _stop_requested():
        return
      if not isinstance(payload, dict):
        return
      idx = payload.get("index")
      msg = str(payload.get("message") or "")
      data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
      if not isinstance(idx, int):
        return

      if msg == "create_post_start":
        _safe_call(on_status, idx, "Tạo post")
      elif msg == "create_post_done":
        status = data.get("status")
        _safe_call(on_status, idx, "Post OK" if status == 200 else "Post lỗi")
      elif msg == "conversation_start":
        _safe_call(on_status, idx, "Đang Tạo video")
      elif msg == "conversation_status":
        _safe_call(on_status, idx, "Đang Tạo video")
      elif msg == "video_generated":
        _safe_call(on_status, idx, "Tạo xong")
        _safe_call(on_progress, idx, 100)
      elif msg == "upscale_start":
        _safe_call(on_status, idx, "Đang Tải video")
      elif msg == "upscale_done":
        status = data.get("status")
        _safe_call(on_status, idx, "Đang Tải video" if status == 200 else "Lỗi")
      elif msg.endswith("_error"):
        _safe_call(on_status, idx, "Lỗi")

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

    async def run_one_job(idx: int, prompt: str) -> None:
      nonlocal headers
      if _stop_requested():
        _safe_call(on_status, idx, "Stop")
        return
      async with sem:
        if _stop_requested():
          _safe_call(on_status, idx, "Stop")
          return
        _safe_call(on_info, f"[GROK-T2V {idx+1}] bắt đầu job")
        _safe_call(on_status, idx, "Xếp hàng")
        local_headers = headers

        _safe_call(on_info, f"[GROK-T2V {idx+1}] gửi request tạo video")
        results = await _await_with_stop(
          api_run_jobs_in_page(
            page,
            prompts=[prompt],
            statsig_headers=local_headers,
            cfg=cfg,
            timeout_seconds=STREAM_TIMEOUT_SECONDS,
            index_offset=idx,
          ),
          step_name="gọi conversation/new",
          idx=idx,
        )

        if _stop_requested():
          _safe_call(on_status, idx, "Stop")
          return

        first_job = results[0] if isinstance(results, list) and results else {}
        if isinstance(first_job, dict) and int(first_job.get("convoStatus") or 0) == 403:
          async with headers_lock:
            _safe_call(on_info, f"Refresh headers (job {idx + 1})")
            headers = await _await_with_stop(
              auto_discover_statsig_headers(page, CACHE_PATH, PROFILE_NAME, force=True, persist=False),
              step_name="refresh headers",
              idx=idx,
            )
            local_headers = headers
          results = await _await_with_stop(
            api_run_jobs_in_page(
              page,
              prompts=[prompt],
              statsig_headers=local_headers,
              cfg=cfg,
              timeout_seconds=STREAM_TIMEOUT_SECONDS,
              index_offset=idx,
            ),
            step_name="gọi lại conversation/new",
            idx=idx,
          )

        if _stop_requested():
          _safe_call(on_status, idx, "Stop")
          return

        job = results[0] if isinstance(results, list) and results else None
        if not isinstance(job, dict):
          _safe_call(on_status, idx, "Lỗi dữ liệu")
          _safe_call(on_info, f"[GROK-T2V {idx+1}] dữ liệu job không hợp lệ")
          return
        parent_id = job.get("parentPostId")
        used_upscale = bool(job.get("usedUpscale"))
        upscale_url = job.get("hdMediaUrl")
        media_url = job.get("mediaUrl")
        is_720p = str(getattr(cfg, "resolution_name", "") or "").strip().lower() == "720p"
        _safe_call(on_info, f"[GROK-T2V {idx+1}] nhận kết quả | convoStatus={int(job.get('convoStatus') or 0)}")

        source_url = None
        if is_720p:
          assets_hd = _build_assets_hd_url(str(media_url or ""), str(parent_id or ""))
          if assets_hd:
            source_url = assets_hd
            _safe_call(on_status, idx, "Tải 720HD")

        if source_url is None and isinstance(parent_id, str) and parent_id.strip():
          source_url = (
            f"https://imagine-public.x.ai/imagine-public/share-videos/"
            f"{parent_id.strip()}.mp4?cache=1&dl=1"
          )
          _safe_call(on_status, idx, "Tải video")

        if used_upscale and isinstance(upscale_url, str) and upscale_url.strip():
          source_url = upscale_url.strip()
          _safe_call(on_status, idx, "Tải HD")

        if isinstance(source_url, str) and source_url.strip():
          if _stop_requested():
            _safe_call(on_status, idx, "Stop")
            return
          filename = _build_unique_video_name(idx + 1, prompt)
          out_path = DOWNLOAD_DIR / filename
          _safe_call(on_info, f"[GROK-T2V {idx+1}] tải video: {out_path.name}")
          ok = await _await_with_stop(
            download_mp4(context, source_url.strip(), out_path, timeout_ms=DOWNLOAD_TIMEOUT_MS),
            step_name="tải video",
            idx=idx,
          )
          if _stop_requested():
            _safe_call(on_status, idx, "Stop")
            return
          if ok:
            _safe_call(on_video, idx, str(out_path))
            _safe_call(on_status, idx, "Hoàn thành")
            _safe_call(on_info, f"[GROK-T2V {idx+1}] hoàn thành")
          else:
            # Retry: upscale again -> download again
            retried = False
            if isinstance(parent_id, str) and parent_id.strip():
              _safe_call(on_status, idx, "Upscale lại")
              fresh_hd = await _upscale_again(parent_id.strip(), local_headers, job_index=idx)
              if isinstance(fresh_hd, str) and fresh_hd.strip():
                retried = True
                _safe_call(on_status, idx, "Tải lại HD")
                ok2 = await _await_with_stop(
                  download_mp4(context, fresh_hd.strip(), out_path, timeout_ms=DOWNLOAD_TIMEOUT_MS),
                  step_name="tải lại HD",
                  idx=idx,
                )
                if ok2:
                  _safe_call(on_video, idx, str(out_path))
                  _safe_call(on_status, idx, "Hoàn thành")
                  _safe_call(on_info, f"[GROK-T2V {idx+1}] hoàn thành sau retry")
                  return
            _safe_call(on_status, idx, "Lỗi tải" if retried else "Lỗi tải")
            _safe_call(on_info, f"[GROK-T2V {idx+1}] tải thất bại")
        else:
          convo_status = int(job.get("convoStatus") or 0)
          if convo_status == 200:
            _safe_call(on_status, idx, "Hoàn thành")
            _safe_call(on_info, f"[GROK-T2V {idx+1}] hoàn thành (không cần tải)")
          else:
            _safe_call(on_status, idx, f"Lỗi {convo_status}")
            _safe_call(on_info, f"[GROK-T2V {idx+1}] lỗi convoStatus={convo_status}")

    async def run_one_job_guarded(idx: int, prompt: str) -> None:
      timeout_seconds = max(60, int(JOB_HARD_TIMEOUT_SECONDS or 420))
      try:
        await asyncio.wait_for(run_one_job(idx, prompt), timeout=timeout_seconds)
      except asyncio.TimeoutError:
        if not _stop_requested():
          _safe_call(on_status, idx, "Lỗi timeout")
          _safe_call(on_info, f"[GROK-T2V {idx+1}] timeout sau {timeout_seconds}s, bỏ qua job để tránh kẹt luồng")
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        if not _stop_requested():
          _safe_call(on_status, idx, "Lỗi")
          _safe_call(on_info, f"[GROK-T2V {idx+1}] lỗi runtime: {exc}")

    tasks = [asyncio.create_task(run_one_job_guarded(i, p)) for i, p in enumerate(prompts or [])]

    async def _wait_stop():
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
              for i in range(len(prompts or [])):
                _safe_call(on_status, i, "Stop")
              await asyncio.gather(*tasks, return_exceptions=True)
              return
            pending_tasks = [t for t in pending_tasks if not t.done()]
          await asyncio.gather(*tasks, return_exceptions=True)
        finally:
          try:
            stop_task.cancel()
          except Exception:
            pass
          await asyncio.gather(stop_task, return_exceptions=True)

  finally:
    await session.close()


def run_text_to_video_jobs(
  prompts: list[str],
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
  cleaned_prompts = [p.strip() for p in (prompts or []) if isinstance(p, str) and p.strip()]
  if not cleaned_prompts:
    raise ValueError("Danh sách prompt rỗng.")

  if isinstance(download_dir, str) and download_dir.strip():
    try:
      DOWNLOAD_DIR = Path(download_dir.strip()) / "grok_video"
      DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
      # fall back to existing DOWNLOAD_DIR
      pass
  else:
    try:
      DOWNLOAD_DIR = Path(DOWNLOAD_DIR) / "grok_video"
      DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
      pass

  runtime_resolution_name = str(resolution_name or "480p")
  if runtime_resolution_name not in {"480p", "720p"}:
    runtime_resolution_name = "480p"

  runtime_aspect_ratio = str(aspect_ratio or "9:16").strip()
  if runtime_aspect_ratio not in {"9:16", "16:9"}:
    runtime_aspect_ratio = "9:16"

  cfg = VideoGenConfig(
    aspect_ratio=runtime_aspect_ratio,
    video_length_seconds=int(video_length_seconds),
    resolution_name=runtime_resolution_name,
  )
  asyncio.run(
    _run_jobs_async_ui(
      prompts=cleaned_prompts,
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


if __name__ == "__main__":
  main()
