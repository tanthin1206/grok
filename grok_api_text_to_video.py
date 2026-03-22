import asyncio
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GROK_BASE = "https://grok.com"

ENDPOINT_CREATE_POST = f"{GROK_BASE}/rest/media/post/create"
ENDPOINT_CONVO_NEW = f"{GROK_BASE}/rest/app-chat/conversations/new"
ENDPOINT_UPSCALE = f"{GROK_BASE}/rest/media/video/upscale"


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 80:
        return value
    return f"{value[:60]}...({len(value)} chars)"


@dataclass(frozen=True)
class VideoGenConfig:
    aspect_ratio: str = "9:16"
    video_length_seconds: int = 6
    resolution_name: str = "480p"

    def as_dict(self) -> dict[str, Any]:
        resolution = str(self.resolution_name or "480p").strip().lower()
        if resolution not in {"480p", "720p"}:
            resolution = "480p"
        return {
            "aspectRatio": self.aspect_ratio,
            "videoLength": int(self.video_length_seconds),
            "resolutionName": resolution,
        }


def payload_create_post(prompt: str) -> dict[str, Any]:
    return {"mediaType": "MEDIA_POST_TYPE_VIDEO", "prompt": prompt}


def payload_conversation_new(prompt: str, parent_post_id: str, cfg: VideoGenConfig) -> dict[str, Any]:
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": prompt,
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {"videoGenModelConfig": {**cfg.as_dict(), "parentPostId": parent_post_id}},
            },
        },
    }


def payload_upscale(video_id: str) -> dict[str, Any]:
    return {"videoId": video_id}


def _load_cache(cache_path: Path) -> dict:
    try:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def get_cached_headers(cache_path: Path, profile_name: str) -> dict:
    cache = _load_cache(cache_path)
    profiles = cache.get("profiles") if isinstance(cache, dict) else None
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        return {}
    headers = entry.get("custom_headers")
    if not isinstance(headers, dict):
        return {}
    headers = dict(headers)
    headers.pop("x-xai-request-id", None)
    return headers


def set_cached_headers(cache_path: Path, profile_name: str, headers: dict) -> None:
    headers = dict(headers or {})
    headers.pop("x-xai-request-id", None)

    cache = _load_cache(cache_path)
    if not isinstance(cache, dict):
        cache = {}
    profiles = cache.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
        cache["profiles"] = profiles

    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        entry = {}
    entry["custom_headers"] = headers
    entry["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    profiles[profile_name] = entry
    _save_cache(cache_path, cache)


def profile_cache_age_seconds(cache_path: Path, profile_name: str) -> float | None:
    cache = _load_cache(cache_path)
    profiles = cache.get("profiles") if isinstance(cache, dict) else None
    if not isinstance(profiles, dict):
        return None
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        return None
    updated_at = entry.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        return None
    try:
        dt = datetime.datetime.fromisoformat(updated_at.strip())
        return (datetime.datetime.now() - dt).total_seconds()
    except Exception:
        return None


async def auto_discover_statsig_headers(
  page,
  cache_path: Path,
  profile_name: str,
  force: bool = False,
  persist: bool = False,
) -> dict:
    cached = get_cached_headers(cache_path, profile_name)
    if cached and not force:
        return cached

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def on_request(req):
        try:
            headers = req.headers or {}
            if not isinstance(headers, dict):
                return
            statsig = headers.get("x-statsig-id")
            if statsig and not future.done():
                future.set_result(statsig)
        except Exception:
            return

    page.on("request", on_request)
    try:
        try:
            await page.goto(f"{GROK_BASE}/imagine", wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        statsig = None
        try:
            statsig = await asyncio.wait_for(future, timeout=12)
        except asyncio.TimeoutError:
            statsig = None

        if isinstance(statsig, str) and statsig.strip():
            custom = {"x-statsig-id": statsig.strip()}
            if persist:
                set_cached_headers(cache_path, profile_name, custom)
            print(f"✅ Auto-discovered x-statsig-id: {_mask(custom.get('x-statsig-id'))}")
            if persist:
                print(f"💾 Saved cache: {cache_path}")
            return custom

        statsig = await page.evaluate(
            """() => {
              try { return localStorage.getItem('x-statsig-id'); } catch (e) { return null; }
            }"""
        )
        if isinstance(statsig, str) and statsig.strip():
            custom = {"x-statsig-id": statsig.strip()}
            if persist:
                set_cached_headers(cache_path, profile_name, custom)
            print("✅ Derived x-statsig-id from localStorage")
            if persist:
                print(f"💾 Saved cache: {cache_path}")
            return custom

        print("⚠️ Không auto-discover được x-statsig-id")
        return {}
    finally:
        try:
            page.off("request", on_request)
        except Exception:
            pass


async def api_run_single_job_in_page(
    page,
    prompt: str,
    statsig_headers: dict,
    cfg: VideoGenConfig,
    timeout_seconds: int,
    job_index: int,
) -> dict:
    """API call sequence run inside the authenticated browser page.

    - Create video post -> returns parentPostId
    - Start conversation -> stream progress
    - Upscale -> returns hdMediaUrl

    Expects workflow to expose globalThis.py_progress for progress updates.
    """

    payload = {
        "prompt": prompt,
        "statsigHeaders": statsig_headers or {},
        "cfg": cfg.as_dict(),
        "timeoutSeconds": int(timeout_seconds),
        "jobIndex": int(job_index),
    }

    result = await page.evaluate(
        """(async ({ prompt, statsigHeaders, cfg, timeoutSeconds, jobIndex }) => {
          function parseJsonObjectsFromBuffer(buffer) {
            const out = [];
            let depth = 0;
            let inString = false;
            let escape = false;
            let start = -1;
            for (let i = 0; i < buffer.length; i++) {
              const ch = buffer[i];
              if (start === -1) {
                if (ch === '{') { start = i; depth = 1; inString = false; escape = false; }
                continue;
              }
              if (inString) {
                if (escape) escape = false;
                else if (ch && ch.charCodeAt(0) === 92) escape = true;
                else if (ch === '"') inString = false;
                continue;
              }
              if (ch === '"') { inString = true; continue; }
              if (ch === '{') depth++;
              else if (ch === '}') {
                depth--;
                if (depth === 0) {
                  const slice = buffer.slice(start, i + 1);
                  try { out.push(JSON.parse(slice)); } catch (e) {}
                  start = -1;
                }
              }
            }
            let tail = '';
            if (start !== -1) tail = buffer.slice(start);
            return { objects: out, tail };
          }

          function pickLastProgressEvent(objects) {
            let last = null;
            for (const obj of objects) {
              const svr = obj && obj.result && obj.result.response && obj.result.response.streamingVideoGenerationResponse;
              if (svr && typeof svr.progress === 'number') {
                last = { progress: svr.progress, videoUrl: svr.videoUrl || null, parentPostId: svr.parentPostId || null };
              }
            }
            return last;
          }

          function reportProgress(pct, videoUrl) {
            try {
              if (typeof globalThis.py_progress === 'function') {
                globalThis.py_progress({ index: jobIndex, progress: pct, videoUrl: videoUrl || null });
              }
            } catch (e) {}
          }

          async function createPost() {
            const res = await fetch('https://grok.com/rest/media/post/create', {
              method: 'POST',
              headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
              credentials: 'include',
              body: JSON.stringify({ mediaType: 'MEDIA_POST_TYPE_VIDEO', prompt }),
            });
            const data = await res.json().catch(() => null);
            const id = data && data.post && data.post.id;
            return { status: res.status, parentPostId: id || null, data };
          }

          async function startConversation(parentPostId) {
            const requestId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
            const convoPayload = {
              temporary: true,
              modelName: 'grok-3',
              message: prompt,
              toolOverrides: { videoGen: true },
              enableSideBySide: true,
              responseMetadata: {
                experiments: [],
                modelConfigOverride: {
                  modelMap: { videoGenModelConfig: Object.assign({ parentPostId }, cfg) },
                },
              },
            };

            const controller = new AbortController();
            const t = setTimeout(() => controller.abort(), Math.max(1, timeoutSeconds) * 1000);

            let res;
            try {
              res = await fetch('https://grok.com/rest/app-chat/conversations/new', {
                method: 'POST',
                headers: Object.assign({ 'content-type': 'application/json', 'x-xai-request-id': requestId }, statsigHeaders || {}),
                credentials: 'include',
                body: JSON.stringify(convoPayload),
                signal: controller.signal,
              });
            } catch (e) {
              clearTimeout(t);
              return { status: 0, error: String(e), lastEvent: null, objectsHead: [] };
            }

            const status = res.status;
            const objectsHead = [];
            let lastEvent = null;

            try {
              if (!res.body) {
                const text = await res.text();
                const parsed = parseJsonObjectsFromBuffer(text);
                if (parsed.objects.length) objectsHead.push(...parsed.objects.slice(0, 2));
                lastEvent = pickLastProgressEvent(parsed.objects);
                if (lastEvent) reportProgress(lastEvent.progress, lastEvent.videoUrl);
                clearTimeout(t);
                return { status, lastEvent, objectsHead };
              }

              const reader = res.body.getReader();
              const decoder = new TextDecoder('utf-8');
              let buffer = '';

              while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                if (!value) continue;

                buffer += decoder.decode(value, { stream: true });
                const parsed = parseJsonObjectsFromBuffer(buffer);
                buffer = parsed.tail;
                if (parsed.objects.length) {
                  if (objectsHead.length < 2) objectsHead.push(...parsed.objects.slice(0, 2 - objectsHead.length));
                  const ev = pickLastProgressEvent(parsed.objects);
                  if (ev) {
                    lastEvent = ev;
                    reportProgress(lastEvent.progress, lastEvent.videoUrl);
                    if (lastEvent.progress >= 100 && lastEvent.videoUrl) break;
                  }
                }
              }

              clearTimeout(t);
              return { status, lastEvent, objectsHead };
            } catch (e) {
              clearTimeout(t);
              return { status, error: String(e), lastEvent, objectsHead };
            }
          }

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
                  return { status: res.status, hdMediaUrl, data, attempt };
                }
                
                if (attempt < maxRetries) {
                  await new Promise(r => setTimeout(r, 2000 * attempt));
                  continue;
                }
                return { status: res.status, hdMediaUrl, data, attempt };
              } catch (e) {
                if (attempt < maxRetries) {
                  await new Promise(r => setTimeout(r, 2000 * attempt));
                  continue;
                }
                return { status: 0, hdMediaUrl: null, data: null, error: String(e), attempt };
              }
            }
            return { status: 0, hdMediaUrl: null, data: null, attempt: maxRetries };
          }

          const created = await createPost();
          if (!created.parentPostId) {
            return { prompt, createStatus: created.status, parentPostId: null, convoStatus: 0, lastEvent: null, upscaleStatus: 0, hdMediaUrl: null, objectsHead: [created.data || null] };
          }

          const convo = await startConversation(created.parentPostId);
          let upscale = null;
          let finalMediaUrl = (convo && convo.lastEvent && convo.lastEvent.videoUrl) ? convo.lastEvent.videoUrl : null;
          const is720p = String((cfg && cfg.resolutionName) || '').toLowerCase() === '720p';
          let usedUpscale = false;
          if (convo && convo.status === 200 && convo.lastEvent && typeof convo.lastEvent.progress === 'number' && convo.lastEvent.progress >= 100) {
            if (!is720p) {
              upscale = await upscaleVideo(created.parentPostId, 3);
              if (upscale && upscale.hdMediaUrl) {
                finalMediaUrl = upscale.hdMediaUrl;
                usedUpscale = true;
              }
            }
          }

          return {
            prompt,
            createStatus: created.status,
            parentPostId: created.parentPostId,
            convoStatus: convo.status,
            lastEvent: convo.lastEvent || null,
            objectsHead: convo.objectsHead || [],
            upscaleStatus: upscale ? upscale.status : 0,
            usedUpscale,
            mediaUrl: finalMediaUrl || null,
            hdMediaUrl: usedUpscale ? (upscale ? (upscale.hdMediaUrl || null) : null) : null,
            upscaleData: upscale ? (upscale.data || null) : null,
          };
        })""",
        payload,
    )

    return result if isinstance(result, dict) else {"error": "unexpected", "raw": result}


async def api_run_jobs_in_page(
    page,
    prompts: list[str],
    statsig_headers: dict,
    cfg: VideoGenConfig,
    timeout_seconds: int,
    index_offset: int = 0,
) -> list[dict]:
    """Run multiple video generations concurrently inside ONE page.

    This avoids opening extra tabs/pages and still runs jobs in parallel using browser fetch.
    """

    payload = {
        "prompts": prompts,
        "statsigHeaders": statsig_headers or {},
        "cfg": cfg.as_dict(),
        "timeoutSeconds": int(timeout_seconds),
        "indexOffset": int(index_offset or 0),
    }

    js = """(async ({ prompts, statsigHeaders, cfg, timeoutSeconds, indexOffset }) => {
          function log(jobIndex, message, data) {
            try {
              if (typeof globalThis.py_log === 'function') {
                globalThis.py_log({
                  index: (indexOffset || 0) + jobIndex,
                  message: String(message || ''),
                  data: (data === undefined ? null : data),
                  ts: Date.now(),
                });
              }
            } catch (e) {}
          }

          function parseJsonObjectsFromBuffer(buffer) {
            const out = [];
            let depth = 0;
            let inString = false;
            let escape = false;
            let start = -1;
            for (let i = 0; i < buffer.length; i++) {
              const ch = buffer[i];
              if (start === -1) {
                if (ch === '{') { start = i; depth = 1; inString = false; escape = false; }
                continue;
              }
              if (inString) {
                if (escape) escape = false;
                else if (ch && ch.charCodeAt(0) === 92) escape = true;
                else if (ch === '"') inString = false;
                continue;
              }
              if (ch === '"') { inString = true; continue; }
              if (ch === '{') depth++;
              else if (ch === '}') {
                depth--;
                if (depth === 0) {
                  const slice = buffer.slice(start, i + 1);
                  try { out.push(JSON.parse(slice)); } catch (e) {}
                  start = -1;
                }
              }
            }
            let tail = '';
            if (start !== -1) tail = buffer.slice(start);
            return { objects: out, tail };
          }

          function pickLastProgressEvent(objects) {
            let last = null;
            for (const obj of objects) {
              const svr = obj && obj.result && obj.result.response && obj.result.response.streamingVideoGenerationResponse;
              if (svr && typeof svr.progress === 'number') {
                last = { progress: svr.progress, videoUrl: svr.videoUrl || null, parentPostId: svr.parentPostId || null };
              }
            }
            return last;
          }

          function reportProgress(jobIndex, pct, videoUrl) {
            try {
              if (typeof globalThis.py_progress === 'function') {
                globalThis.py_progress({ index: (indexOffset || 0) + jobIndex, progress: pct, videoUrl: videoUrl || null });
              }
            } catch (e) {}
          }

          async function createPost(prompt) {
            const res = await fetch('https://grok.com/rest/media/post/create', {
              method: 'POST',
              headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
              credentials: 'include',
              body: JSON.stringify({ mediaType: 'MEDIA_POST_TYPE_VIDEO', prompt }),
            });
            const data = await res.json().catch(() => null);
            const id = data && data.post && data.post.id;
            return { status: res.status, parentPostId: id || null, data };
          }

          async function upscaleVideo(videoId, jobIndex, maxRetries = 3) {
            // Retry up to maxRetries times
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
                
                // Success if status=200 and hdMediaUrl exists
                if (res.status === 200 && hdMediaUrl) {
                  return { status: res.status, hdMediaUrl, data, attempt };
                }
                
                // Failed but can retry
                if (attempt < maxRetries) {
                  log(jobIndex, 'upscale_retry', { attempt, status: res.status, hdMediaUrl: hdMediaUrl || null });
                  await new Promise(r => setTimeout(r, 2000 * attempt)); // Wait 2s, 4s before retry
                  continue;
                }
                
                // Last attempt failed
                return { status: res.status, hdMediaUrl, data, attempt };
              } catch (e) {
                if (attempt < maxRetries) {
                  log(jobIndex, 'upscale_retry_error', { attempt, error: String(e) });
                  await new Promise(r => setTimeout(r, 2000 * attempt));
                  continue;
                }
                return { status: 0, hdMediaUrl: null, data: null, error: String(e), attempt };
              }
            }
            return { status: 0, hdMediaUrl: null, data: null, attempt: maxRetries };
          }

          async function startConversation(prompt, parentPostId, jobIndex) {
            log(jobIndex, 'conversation_start', { parentPostId });
            const requestId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
            const convoPayload = {
              temporary: true,
              modelName: 'grok-3',
              message: prompt,
              toolOverrides: { videoGen: true },
              enableSideBySide: true,
              responseMetadata: {
                experiments: [],
                modelConfigOverride: {
                  modelMap: { videoGenModelConfig: Object.assign({ parentPostId }, cfg) },
                },
              },
            };

            const controller = new AbortController();
            const t = setTimeout(() => controller.abort(), Math.max(1, timeoutSeconds) * 1000);

            let res;
            try {
              res = await fetch('https://grok.com/rest/app-chat/conversations/new', {
                method: 'POST',
                headers: Object.assign({ 'content-type': 'application/json', 'x-xai-request-id': requestId }, statsigHeaders || {}),
                credentials: 'include',
                body: JSON.stringify(convoPayload),
                signal: controller.signal,
              });
            } catch (e) {
              clearTimeout(t);
              log(jobIndex, 'conversation_fetch_error', { error: String(e) });
              return { status: 0, error: String(e), lastEvent: null, objectsHead: [] };
            }

            const status = res.status;
            log(jobIndex, 'conversation_status', { status });
            const objectsHead = [];
            let lastEvent = null;

            try {
              if (!res.body) {
                const text = await res.text();
                const parsed = parseJsonObjectsFromBuffer(text);
                if (parsed.objects.length) objectsHead.push(...parsed.objects.slice(0, 2));
                lastEvent = pickLastProgressEvent(parsed.objects);
                if (lastEvent) reportProgress(jobIndex, lastEvent.progress, lastEvent.videoUrl);
                clearTimeout(t);
                return { status, lastEvent, objectsHead };
              }

              const reader = res.body.getReader();
              const decoder = new TextDecoder('utf-8');
              let buffer = '';

              while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                if (!value) continue;

                buffer += decoder.decode(value, { stream: true });
                const parsed = parseJsonObjectsFromBuffer(buffer);
                buffer = parsed.tail;
                if (parsed.objects.length) {
                  if (objectsHead.length < 2) objectsHead.push(...parsed.objects.slice(0, 2 - objectsHead.length));
                  const ev = pickLastProgressEvent(parsed.objects);
                  if (ev) {
                    lastEvent = ev;
                    reportProgress(jobIndex, lastEvent.progress, lastEvent.videoUrl);
                    if (lastEvent.progress >= 100 && lastEvent.videoUrl) break;
                  }
                }
              }

              clearTimeout(t);
              return { status, lastEvent, objectsHead };
            } catch (e) {
              clearTimeout(t);
              log(jobIndex, 'conversation_parse_error', { error: String(e) });
              return { status, error: String(e), lastEvent, objectsHead };
            }
          }

          const createResults = await Promise.all((prompts || []).map((p, i) => {
            log(i, 'create_post_start');
            return createPost(p);
          }));
          const results = await Promise.all(createResults.map(async (cr, i) => {
            const prompt = prompts[i];
            const parentPostId = cr.parentPostId;
            log(i, 'create_post_done', { status: cr.status, parentPostId: parentPostId || null });
            if (!parentPostId) {
              return { prompt, createStatus: cr.status, parentPostId: null, convoStatus: 0, lastEvent: null, upscaleStatus: 0, hdMediaUrl: null, objectsHead: [cr.data || null] };
            }

            const convo = await startConversation(prompt, parentPostId, i);
            if (convo && convo.lastEvent && typeof convo.lastEvent.progress === 'number' && convo.lastEvent.progress >= 100) {
              log(i, 'video_generated', { parentPostId, videoUrl: convo.lastEvent.videoUrl || null });
            }
            let upscale = null;
            let finalMediaUrl = (convo && convo.lastEvent && convo.lastEvent.videoUrl) ? convo.lastEvent.videoUrl : null;
            const is720p = String((cfg && cfg.resolutionName) || '').toLowerCase() === '720p';
            let usedUpscale = false;

            if (convo && convo.status === 200 && convo.lastEvent && typeof convo.lastEvent.progress === 'number' && convo.lastEvent.progress >= 100) {
              if (is720p) {
                log(i, 'upscale_skip', { reason: 'already_720p', mediaUrl: finalMediaUrl || null });
              } else {
                log(i, 'upscale_start', { videoId: parentPostId });
                upscale = await upscaleVideo(parentPostId, i, 3);
                log(i, 'upscale_done', { status: upscale.status, hdMediaUrl: upscale.hdMediaUrl || null, attempt: upscale.attempt || 1 });
                if (upscale && upscale.hdMediaUrl) {
                  finalMediaUrl = upscale.hdMediaUrl;
                  usedUpscale = true;
                }
              }
            }

            return {
              prompt,
              createStatus: cr.status,
              parentPostId,
              convoStatus: convo.status,
              lastEvent: convo.lastEvent || null,
              objectsHead: convo.objectsHead || [],
              upscaleStatus: upscale ? upscale.status : 0,
              usedUpscale,
              mediaUrl: finalMediaUrl || null,
              hdMediaUrl: usedUpscale ? (upscale ? (upscale.hdMediaUrl || null) : null) : null,
              upscaleData: upscale ? (upscale.data || null) : null,
            };
          }));

          return results;
        })"""

    # Playwright can throw: "Execution context was destroyed" if the page navigates/reloads.
    # This happens occasionally if Grok refreshes, Cloudflare kicks in, or the user clicks around.
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            result = await page.evaluate(js, payload)
            return result if isinstance(result, list) else []
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            is_ctx_destroyed = (
                "Execution context was destroyed" in msg
                or "Most likely the page has been closed" in msg
                or "Navigation" in msg
            )
            if not is_ctx_destroyed or attempt >= 3:
                raise

            # wait for navigation to settle, then try to get back to the imagine page
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            try:
                await page.goto("https://grok.com/imagine", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            try:
                import asyncio

                await asyncio.sleep(0.6 * attempt)
            except Exception:
                pass

    # unreachable, but keep mypy happy
    if last_exc is not None:
        raise last_exc
    return []


async def download_mp4(context, url: str, out_path: Path, timeout_ms: int) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def looks_like_mp4(buf: bytes) -> bool:
        return isinstance(buf, (bytes, bytearray)) and len(buf) > 12 and buf[4:8] == b"ftyp"

    target = (url or "").strip()
    if not target:
        print("⚠️ Download failed: empty URL")
        return False

    headers = {
        "accept": "video/mp4,application/octet-stream,*/*",
        "referer": f"{GROK_BASE}/imagine",
    }

    max_attempts = 3
    last_body: bytes | None = None
    last_status: int | None = None
    last_ctype: str | None = None
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
      print(f"⬇️ Downloading ({attempt}/{max_attempts}): {target}")
      try:
        resp = await context.request.get(target, timeout=timeout_ms, headers=headers)
        body = await resp.body()
        ctype = (resp.headers.get("content-type") or "").lower()
        last_body = body
        last_status = getattr(resp, "status", None)
        last_ctype = ctype

        if resp.status == 200 and ("video" in ctype or "octet-stream" in ctype) and looks_like_mp4(body):
          out_path.write_bytes(body)
          print(f"⬇️ Saved: {out_path}")
          return True

        sample = body[:200].decode("utf-8", errors="replace") if body else ""
        print(
          f"⚠️ Download failed: status={resp.status} ctype={ctype} url={target} sample={sample!r}"
        )
      except Exception as exc:
        last_exc = exc
        print(f"⚠️ Download exception: {exc}")

      if attempt < max_attempts:
        # small backoff for flaky CDN / transient network errors
        try:
          import asyncio

          await asyncio.sleep(0.8 * attempt)
        except Exception:
          pass

    # Final failure: no file logging (per UI requirement)
    print(f"⚠️ Download failed after {max_attempts} attempts (status={last_status} ctype={last_ctype} exc={last_exc})")
    return False
