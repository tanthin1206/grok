"""
Grok API: Image to Video
- Upload image (base64)
- Create video from image
- Poll progress until completion
- Upscale to HD
"""

import base64
import datetime
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GROK_BASE = "https://grok.com"
GROK_ASSETS_BASE = "https://assets.grok.com"

ENDPOINT_UPLOAD_FILE = f"{GROK_BASE}/rest/app-chat/upload-file"
ENDPOINT_POST_CREATE = f"{GROK_BASE}/rest/media/post/create"
ENDPOINT_CONVO_NEW = f"{GROK_BASE}/rest/app-chat/conversations/new"
ENDPOINT_UPSCALE = f"{GROK_BASE}/rest/media/video/upscale"
UPLOAD_FILE_SOURCE = "SELF_UPLOAD_FILE_SOURCE"
REQUEST_LOG_PATH = Path(
  os.getenv(
    "GROK_REQUEST_LOG_PATH",
    str(Path(__file__).resolve().parent / "Workflows" / "default_project" / "grok_request.json"),
  )
)


@dataclass(frozen=True)
class ImageToVideoConfig:
    aspect_ratio: str = "9:16"
    video_length_seconds: int = 6
    resolution_name: str = "480p"
    is_video_edit: bool = False

    def as_dict(self) -> dict[str, Any]:
        resolution = str(self.resolution_name or "480p").strip().lower()
        if resolution not in {"480p", "720p"}:
            resolution = "480p"
        return {
            "aspectRatio": self.aspect_ratio,
            "videoLength": int(self.video_length_seconds),
            "resolutionName": resolution,
            "isVideoEdit": self.is_video_edit,
        }


def image_to_base64(image_path: Path) -> str:
    """Convert image file to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_mime_type(image_path: Path) -> str:
    """Get MIME type from file extension."""
    mime, _ = mimetypes.guess_type(str(image_path))
    return mime or "image/png"


def payload_upload_image(image_path: Path) -> dict[str, Any]:
    """Create payload for uploading image."""
    return {
        "fileName": image_path.name,
        "fileMimeType": get_mime_type(image_path),
        "content": image_to_base64(image_path),
    "fileSource": UPLOAD_FILE_SOURCE,
    }


def payload_image_to_video(
    prompt: str,
    file_metadata_id: str,
    file_uri: str,
    cfg: ImageToVideoConfig,
) -> dict[str, Any]:
    """Create payload for generating video from image.
    
    Args:
        prompt: Text prompt for video generation
        file_metadata_id: ID returned from upload (fileMetadataId)
        file_uri: URI returned from upload (fileUri)
        cfg: Video generation config
    """
    # Build full asset URL
    asset_url = f"{GROK_ASSETS_BASE}/{file_uri}"
    
    # Message format: asset_url + space + prompt
    message = f"{asset_url}  {prompt}"
    
    return {
        "temporary": True,
        "modelName": "grok-3",
        "message": message,
        "fileAttachments": [file_metadata_id],
        "toolOverrides": {"videoGen": True},
        "enableSideBySide": True,
        "responseMetadata": {
            "experiments": [],
            "modelConfigOverride": {
                "modelMap": {
                    "videoGenModelConfig": {
                        "parentPostId": file_metadata_id,
                        **cfg.as_dict(),
                    }
                }
            },
        },
    }


def payload_upscale(video_id: str) -> dict[str, Any]:
    """Create payload for upscaling video."""
    return {"videoId": video_id}


def _extract_user_id_from_file_uri(file_uri: str | None) -> str:
  raw = str(file_uri or "").strip().lstrip("/")
  parts = [p for p in raw.split("/") if p]
  if len(parts) >= 2 and parts[0] == "users":
    return parts[1]
  return ""


def _normalize_assets_url(url_or_uri: str | None, *, add_download_query: bool = False) -> str:
  raw = str(url_or_uri or "").strip()
  if not raw:
    return ""

  if raw.startswith("http://") or raw.startswith("https://"):
    url = raw
  else:
    url = f"{GROK_ASSETS_BASE}/{raw.lstrip('/')}"

  if add_download_query and "?" not in url:
    url = f"{url}?cache=1&dl=1"
  return url


def _extract_user_and_generated_from_video_url(video_url: str | None) -> tuple[str, str]:
  raw = str(video_url or "").strip()
  if not raw:
    return "", ""

  normalized = raw
  if normalized.startswith("http://") or normalized.startswith("https://"):
    try:
      from urllib.parse import urlparse

      normalized = urlparse(normalized).path
    except Exception:
      normalized = raw

  parts = [p for p in str(normalized).split("/") if p]
  # users/<uid>/generated/<generated_id>/generated_video.mp4
  if len(parts) >= 5 and parts[0] == "users" and parts[2] == "generated":
    return parts[1], parts[3]
  return "", ""


def _build_generated_video_urls(user_id: str, generated_id: str) -> dict[str, str]:
  uid = str(user_id or "").strip()
  gid = str(generated_id or "").strip()
  if not uid or not gid:
    return {"direct": "", "hd": ""}
  base = f"https://assets.grok.com/users/{uid}/generated/{gid}"
  return {
    "direct": f"{base}/generated_video.mp4?cache=1&dl=1",
    "hd": f"{base}/generated_video_hd.mp4?cache=1&dl=1",
  }


def _append_request_log(record: dict[str, Any]) -> None:
  """Append one debug record into pretty JSON file grok_request.json."""
  try:
    REQUEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict[str, Any]] = []
    if REQUEST_LOG_PATH.exists():
      try:
        raw = json.loads(REQUEST_LOG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, list):
          existing = [item for item in raw if isinstance(item, dict)]
      except Exception:
        existing = []

    existing.append(record)
    REQUEST_LOG_PATH.write_text(
      json.dumps(existing, ensure_ascii=False, indent=2),
      encoding="utf-8",
    )
  except Exception:
    pass


async def api_upload_image_in_page(
    page,
    image_path: Path,
    statsig_headers: dict,
) -> dict:
    """Upload image via browser page.
    
    Returns dict with:
        - status: HTTP status code
        - fileMetadataId: ID to use for video generation
        - fileUri: URI for asset URL
        - data: Full response data
    """
    
    # Read and encode image
    try:
        content_b64 = image_to_base64(image_path)
        mime_type = get_mime_type(image_path)
        file_name = image_path.name
    except Exception as e:
        return {"status": 0, "error": f"Failed to read image: {e}", "fileMetadataId": None}
    
    payload = {
        "fileName": file_name,
        "fileMimeType": mime_type,
        "content": content_b64,
      "fileSource": UPLOAD_FILE_SOURCE,
        "statsigHeaders": statsig_headers or {},
    }

    upload_request_debug = {
      "endpoint": ENDPOINT_UPLOAD_FILE,
      "payload": {
        "fileName": file_name,
        "fileMimeType": mime_type,
        "fileSource": UPLOAD_FILE_SOURCE,
        "contentLength": len(content_b64 or ""),
      },
      "headers": dict(statsig_headers or {}),
    }
    
    result = await page.evaluate(
        """(async ({ fileName, fileMimeType, content, fileSource, statsigHeaders }) => {
        function pickStringAny(root, keys) {
          const queue = [root];
          const seen = new Set();
          while (queue.length) {
            const cur = queue.shift();
            if (!cur || typeof cur !== 'object' || seen.has(cur)) continue;
            seen.add(cur);
            for (const k of keys) {
              const v = cur[k];
              if (typeof v === 'string' && v.trim()) return v.trim();
            }
            for (const v of Object.values(cur)) {
              if (v && typeof v === 'object') queue.push(v);
            }
          }
          return null;
        }

            try {
                const res = await fetch('https://grok.com/rest/app-chat/upload-file', {
                    method: 'POST',
                    headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
                    credentials: 'include',
                    body: JSON.stringify({ fileName, fileMimeType, content, fileSource }),
                });
                const data = await res.json().catch(() => null);
          const fileMetadataId = pickStringAny(data, ['fileMetadataId', 'file_metadata_id', 'metadataId']);
          const fileUri = pickStringAny(data, ['fileUri', 'fileURL', 'uri', 'assetUri', 'assetUrl', 'url']);
          const parentPostId = pickStringAny(data, ['parentPostId', 'postId', 'id', 'fileMetadataId']);
                return {
                    status: res.status,
            fileMetadataId,
            fileUri,
            parentPostId,
                    fileName: data && data.fileName ? data.fileName : null,
                    data: data,
                };
            } catch (e) {
                return { status: 0, error: String(e), fileMetadataId: null };
            }
        })""",
        payload,
    )

    normalized = result if isinstance(result, dict) else {"error": "unexpected", "raw": result}
    _append_request_log(
      {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": "image_to_video_upload_image",
        "request": upload_request_debug,
        "response": {
          "status": normalized.get("status"),
          "error": normalized.get("error"),
          "fileMetadataId": normalized.get("fileMetadataId"),
          "fileUri": normalized.get("fileUri"),
          "parentPostId": normalized.get("parentPostId"),
          "data": normalized.get("data"),
        },
      }
    )
    return normalized


async def api_image_to_video_in_page(
    page,
    prompt: str,
    file_metadata_id: str,
    file_uri: str,
    parent_post_id: str | None,
    statsig_headers: dict,
    cfg: ImageToVideoConfig,
    timeout_seconds: int,
    job_index: int = 0,
) -> dict:
    """Generate video from uploaded image.
    
    Streams progress and returns when video is complete or timeout.
    """
    
    # Build asset URL and message (tolerate full URL or relative URI or missing URI)
    raw_uri = str(file_uri or "").strip()
    if raw_uri.startswith("http://") or raw_uri.startswith("https://"):
      asset_url = raw_uri
    elif raw_uri:
      asset_url = f"https://assets.grok.com/{raw_uri.lstrip('/')}"
    else:
      asset_url = ""

    prompt_text = str(prompt or "").strip()
    message = f"{asset_url}  {prompt_text}".strip() if asset_url else prompt_text

    parent_id = str(parent_post_id or "").strip() or str(file_metadata_id or "").strip()
    attachment_id = parent_id or str(file_metadata_id or "").strip()
    
    payload = {
        "message": message,
        "fileMetadataId": file_metadata_id,
        "attachmentId": attachment_id,
        "parentPostId": parent_id,
        "cfg": cfg.as_dict(),
        "statsigHeaders": statsig_headers or {},
        "timeoutSeconds": int(timeout_seconds),
        "jobIndex": int(job_index),
    }

    request_debug = {
      "endpoint": ENDPOINT_CONVO_NEW,
      "temporary": True,
      "modelName": "grok-3",
      "message": message,
      "fileAttachments": [attachment_id],
      "toolOverrides": {"videoGen": True},
      "enableSideBySide": True,
      "responseMetadata": {
        "experiments": [],
        "modelConfigOverride": {
          "modelMap": {
            "videoGenModelConfig": {
              "parentPostId": parent_id or file_metadata_id,
              **cfg.as_dict(),
            }
          }
        },
      },
    }
    
    result = await page.evaluate(
        """(async ({ message, fileMetadataId, attachmentId, parentPostId, cfg, statsigHeaders, timeoutSeconds, jobIndex }) => {
          function log(msg, data) {
            try {
              if (typeof globalThis.py_log === 'function') {
                globalThis.py_log({ index: jobIndex, message: msg, data: data || null, ts: Date.now() });
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
              if (!svr || typeof svr !== 'object') continue;

              const hasProgress = (typeof svr.progress === 'number');
              const hasVideoUrl = !!(svr.videoUrl);
              const hasVideoId = !!(svr.videoId || svr.videoPostId);
              const hasParent = !!(svr.parentPostId);
              const hasResolution = !!(svr.resolutionName);
              if (!hasProgress && !hasVideoUrl && !hasVideoId && !hasParent && !hasResolution) continue;

              const prevProgress = (last && typeof last.progress === 'number') ? last.progress : 0;
              const nextProgress = hasProgress ? svr.progress : prevProgress;
              const candidateVideoUrl =
                svr.videoUrl ||
                svr.generatedVideoUrl ||
                svr.generatedVideoUri ||
                svr.mediaUrl ||
                (last ? last.videoUrl : null) ||
                null;
              last = {
                progress: nextProgress,
                videoUrl: candidateVideoUrl,
                videoId: svr.videoId || svr.videoPostId || (last ? last.videoId : null) || null,
                parentPostId: svr.parentPostId || (last ? last.parentPostId : null) || null,
                resolutionName: svr.resolutionName || (last ? last.resolutionName : null) || null,
                imageReference: svr.imageReference || (last ? last.imageReference : null) || null,
              };
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

          log('conversation_start', { fileMetadataId, parentPostId, hasMessage: !!message, messagePreview: String(message || '').slice(0, 120) });

          const requestId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random();
          const convoPayload = {
            temporary: true,
            modelName: 'grok-3',
            message: message,
            fileAttachments: [attachmentId || fileMetadataId],
            toolOverrides: { videoGen: true },
            enableSideBySide: true,
            responseMetadata: {
              experiments: [],
              modelConfigOverride: {
                modelMap: {
                  videoGenModelConfig: Object.assign({ parentPostId: parentPostId || fileMetadataId }, cfg),
                },
              },
            },
          };

          log('conversation_payload_ready', {
            hasFileAttachment: !!(attachmentId || fileMetadataId),
            parentPostId: parentPostId || fileMetadataId || null,
            resolutionName: cfg && cfg.resolutionName,
            videoLength: cfg && cfg.videoLength,
          });

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
            log('conversation_fetch_error', { error: String(e) });
            return { status: 0, error: String(e), lastEvent: null };
          }

          const status = res.status;
          log('conversation_status', { status });
          let lastEvent = null;

          try {
            if (!res.body) {
              const text = await res.text();
              const parsed = parseJsonObjectsFromBuffer(text);
              lastEvent = pickLastProgressEvent(parsed.objects);
              if (lastEvent) reportProgress(lastEvent.progress, lastEvent.videoUrl);
              clearTimeout(t);
              return { status, lastEvent };
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
                const ev = pickLastProgressEvent(parsed.objects);
                if (ev) {
                  lastEvent = ev;
                  reportProgress(lastEvent.progress, lastEvent.videoUrl);
                  if ((lastEvent.progress >= 95 && lastEvent.videoUrl) || (lastEvent.videoUrl && !('progress' in lastEvent))) {
                    log('video_generated', { videoUrl: lastEvent.videoUrl, videoId: lastEvent.videoId });
                    break;
                  }
                }
              }
            }

            clearTimeout(t);
            return { status, lastEvent };
          } catch (e) {
            clearTimeout(t);
            log('conversation_parse_error', { error: String(e) });
            return { status, error: String(e), lastEvent };
          }
        })""",
        payload,
    )

    normalized = result if isinstance(result, dict) else {"error": "unexpected", "raw": result}
    last_event = normalized.get("lastEvent") if isinstance(normalized.get("lastEvent"), dict) else {}
    stream_video_url = _normalize_assets_url(last_event.get("videoUrl"), add_download_query=False)
    if stream_video_url:
      last_event["videoUrl"] = stream_video_url

    stream_user_id, stream_generated_id = _extract_user_and_generated_from_video_url(stream_video_url)
    generated_id = str(
      last_event.get("videoId")
      or stream_generated_id
      or last_event.get("parentPostId")
      or parent_id
      or file_metadata_id
      or ""
    ).strip()
    user_id = str(_extract_user_id_from_file_uri(file_uri) or stream_user_id).strip()
    urls = _build_generated_video_urls(user_id, generated_id)

    normalized["userId"] = user_id
    normalized["generatedId"] = generated_id
    normalized["directVideoUrl"] = urls["direct"]
    normalized["hdVideoUrlCandidate"] = urls["hd"]

    _append_request_log(
      {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": "image_to_video_conversation_new",
        "jobIndex": int(job_index),
        "prompt": str(prompt or ""),
        "request": request_debug,
        "response": {
          "status": normalized.get("status"),
          "error": normalized.get("error"),
          "lastEvent": last_event,
          "userId": normalized.get("userId"),
          "generatedId": normalized.get("generatedId"),
          "directVideoUrl": normalized.get("directVideoUrl"),
          "hdVideoUrlCandidate": normalized.get("hdVideoUrlCandidate"),
        },
      }
    )
    return normalized


async def api_create_image_post_in_page(
    page,
    media_url: str,
    statsig_headers: dict,
    job_index: int = 0,
) -> dict:
    """Create image post before creating conversation/new.

    Expected request:
      POST /rest/media/post/create
      {"mediaType":"MEDIA_POST_TYPE_IMAGE","mediaUrl":"https://assets.grok.com/.../content"}
    """
    clean_media_url = str(media_url or "").strip()
    payload = {
        "mediaType": "MEDIA_POST_TYPE_IMAGE",
        "mediaUrl": clean_media_url,
        "statsigHeaders": statsig_headers or {},
    }

    request_debug = {
      "endpoint": ENDPOINT_POST_CREATE,
      "payload": {
        "mediaType": "MEDIA_POST_TYPE_IMAGE",
        "mediaUrl": clean_media_url,
      },
      "headers": dict(statsig_headers or {}),
    }

    result = await page.evaluate(
        """(async ({ mediaType, mediaUrl, statsigHeaders }) => {
          function pickStringAny(root, keys) {
            const queue = [root];
            const seen = new Set();
            while (queue.length) {
              const cur = queue.shift();
              if (!cur || typeof cur !== 'object' || seen.has(cur)) continue;
              seen.add(cur);
              for (const k of keys) {
                const v = cur[k];
                if (typeof v === 'string' && v.trim()) return v.trim();
              }
              for (const v of Object.values(cur)) {
                if (v && typeof v === 'object') queue.push(v);
              }
            }
            return null;
          }

          try {
            const res = await fetch('https://grok.com/rest/media/post/create', {
              method: 'POST',
              headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
              credentials: 'include',
              body: JSON.stringify({ mediaType, mediaUrl }),
            });
            const data = await res.json().catch(() => null);
            const postId = pickStringAny(data, ['id', 'postId', 'parentPostId']);
            const postMediaUrl = pickStringAny(data, ['mediaUrl', 'url']);
            return {
              status: res.status,
              postId,
              mediaUrl: postMediaUrl,
              data,
            };
          } catch (e) {
            return { status: 0, error: String(e), postId: null, mediaUrl: null };
          }
        })""",
        payload,
    )

    normalized = result if isinstance(result, dict) else {"error": "unexpected", "raw": result}
    _append_request_log(
      {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": "image_to_video_create_post",
        "jobIndex": int(job_index),
        "request": request_debug,
        "response": {
          "status": normalized.get("status"),
          "error": normalized.get("error"),
          "postId": normalized.get("postId"),
          "mediaUrl": normalized.get("mediaUrl"),
          "data": normalized.get("data"),
        },
      }
    )
    return normalized


async def api_upscale_video_in_page(
    page,
    video_id: str,
    statsig_headers: dict,
    job_index: int = 0,
    max_retries: int = 3,
) -> dict:
    """Upscale video to HD with retry logic.
    
    Returns dict with:
        - status: HTTP status code
        - hdMediaUrl: HD video URL if successful
        - attempt: Which attempt succeeded (or last attempt if failed)
    """
    
    payload = {
        "videoId": video_id,
        "statsigHeaders": statsig_headers or {},
        "jobIndex": int(job_index),
        "maxRetries": int(max_retries),
    }
    
    result = await page.evaluate(
        """(async ({ videoId, statsigHeaders, jobIndex, maxRetries }) => {
          function log(msg, data) {
            try {
              if (typeof globalThis.py_log === 'function') {
                globalThis.py_log({ index: jobIndex, message: msg, data: data || null, ts: Date.now() });
              }
            } catch (e) {}
          }

          function pickStringAny(root, keys) {
            const queue = [root];
            const seen = new Set();
            while (queue.length) {
              const cur = queue.shift();
              if (!cur || typeof cur !== 'object' || seen.has(cur)) continue;
              seen.add(cur);
              for (const k of keys) {
                const v = cur[k];
                if (typeof v === 'string' && v.trim()) return v.trim();
              }
              for (const v of Object.values(cur)) {
                if (v && typeof v === 'object') queue.push(v);
              }
            }
            return null;
          }

          for (let attempt = 1; attempt <= maxRetries; attempt++) {
            try {
              log('upscale_attempt', { attempt, videoId });
              
              const res = await fetch('https://grok.com/rest/media/video/upscale', {
                method: 'POST',
                headers: Object.assign({ 'content-type': 'application/json' }, statsigHeaders || {}),
                credentials: 'include',
                body: JSON.stringify({ videoId }),
              });
              const data = await res.json().catch(() => null);
              const hdMediaUrl = pickStringAny(data, ['hdMediaUrl', 'hdVideoUrl', 'videoUrl', 'url']);
              
              if (res.status === 200 && hdMediaUrl) {
                log('upscale_success', { attempt, hdMediaUrl });
                return { status: res.status, hdMediaUrl, data, attempt };
              }
              
              if (attempt < maxRetries) {
                log('upscale_retry', { attempt, status: res.status, hdMediaUrl });
                await new Promise(r => setTimeout(r, 2000 * attempt));
                continue;
              }
              
              return { status: res.status, hdMediaUrl, data, attempt };
            } catch (e) {
              if (attempt < maxRetries) {
                log('upscale_retry_error', { attempt, error: String(e) });
                await new Promise(r => setTimeout(r, 2000 * attempt));
                continue;
              }
              return { status: 0, hdMediaUrl: null, error: String(e), attempt };
            }
          }
          return { status: 0, hdMediaUrl: null, attempt: maxRetries };
        })""",
        payload,
    )

    normalized = result if isinstance(result, dict) else {"error": "unexpected", "raw": result}
    _append_request_log(
      {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "kind": "image_to_video_upscale",
        "jobIndex": int(job_index),
        "request": {
          "endpoint": ENDPOINT_UPSCALE,
          "payload": {"videoId": str(video_id or "").strip()},
          "headers": dict(statsig_headers or {}),
          "maxRetries": int(max_retries),
        },
        "response": {
          "status": normalized.get("status"),
          "error": normalized.get("error"),
          "attempt": normalized.get("attempt"),
          "hdMediaUrl": normalized.get("hdMediaUrl"),
          "data": normalized.get("data"),
        },
      }
    )

    return normalized


async def api_run_image_to_video_job(
    page,
    image_path: Path,
    prompt: str,
    statsig_headers: dict,
    cfg: ImageToVideoConfig,
    timeout_seconds: int,
    job_index: int = 0,
) -> dict:
    """Complete image-to-video pipeline:
    1. Upload image
    2. Generate video
    3. Upscale to HD
    
    Returns full result dict.
    """
    
    # Step 1: Upload image
    upload_result = await api_upload_image_in_page(page, image_path, statsig_headers)
    
    if not upload_result.get("fileMetadataId"):
        return {
            "prompt": prompt,
            "imagePath": str(image_path),
            "uploadStatus": upload_result.get("status", 0),
            "uploadError": upload_result.get("error"),
            "fileMetadataId": None,
            "convoStatus": 0,
            "lastEvent": None,
            "upscaleStatus": 0,
            "hdMediaUrl": None,
        }
    
    file_metadata_id = upload_result["fileMetadataId"]
    file_uri = upload_result.get("fileUri", "")
    parent_post_id = upload_result.get("parentPostId", "")
    upload_media_url = _normalize_assets_url(file_uri, add_download_query=False)

    # Step 2: Create image post (required before conversation/new)
    create_post_result = await api_create_image_post_in_page(
      page,
      media_url=upload_media_url,
      statsig_headers=statsig_headers,
      job_index=job_index,
    )
    created_post_id = str(create_post_result.get("postId") or "").strip()
    created_media_url = str(create_post_result.get("mediaUrl") or "").strip()
    if not created_post_id:
      return {
        "prompt": prompt,
        "imagePath": str(image_path),
        "uploadStatus": upload_result.get("status", 0),
        "createPostStatus": create_post_result.get("status", 0),
        "createPostError": create_post_result.get("error"),
        "fileMetadataId": file_metadata_id,
        "fileUri": file_uri,
        "parentPostId": "",
        "convoStatus": 0,
        "lastEvent": None,
        "upscaleStatus": 0,
        "hdMediaUrl": None,
      }
    if created_post_id:
      parent_post_id = created_post_id
    if created_media_url:
      file_uri = created_media_url
    
    # Step 3: Generate video
    video_result = await api_image_to_video_in_page(
        page,
        prompt=prompt,
        file_metadata_id=file_metadata_id,
        file_uri=file_uri,
        parent_post_id=parent_post_id,
        statsig_headers=statsig_headers,
        cfg=cfg,
        timeout_seconds=timeout_seconds,
        job_index=job_index,
    )
    
    last_event = video_result.get("lastEvent") or {}
    video_id = last_event.get("videoId")
    direct_video_url = str(video_result.get("directVideoUrl") or "").strip()
    hd_video_candidate = str(video_result.get("hdVideoUrlCandidate") or "").strip()
    has_generation_signal = bool(
      (isinstance(last_event, dict) and (last_event.get("videoUrl") or last_event.get("videoId")))
      or direct_video_url
      or hd_video_candidate
      or (isinstance(last_event, dict) and int(last_event.get("progress") or 0) >= 95)
    )
    
    # Step 4: Upscale if video generated successfully
    upscale_result = None
    if (
      video_result.get("status") == 200
      and has_generation_signal
      and (
        last_event.get("progress", 0) >= 95
        or bool(last_event.get("videoUrl"))
        or bool(last_event.get("videoId"))
      )
      and bool(video_id)
    ):
        upscale_result = await api_upscale_video_in_page(
            page,
            video_id=video_id,
            statsig_headers=statsig_headers,
            job_index=job_index,
            max_retries=3,
        )
    
    return {
        "prompt": prompt,
        "imagePath": str(image_path),
        "uploadStatus": upload_result.get("status", 0),
        "createPostStatus": create_post_result.get("status", 0),
        "fileMetadataId": file_metadata_id,
        "fileUri": file_uri,
        "parentPostId": parent_post_id,
        "convoStatus": video_result.get("status", 0),
        "lastEvent": last_event,
        "videoId": video_id,
      "userId": video_result.get("userId", ""),
      "generatedId": video_result.get("generatedId", ""),
      "directVideoUrl": direct_video_url,
      "hdVideoUrlCandidate": hd_video_candidate,
        "upscaleStatus": upscale_result.get("status", 0) if upscale_result else 0,
        "hdMediaUrl": upscale_result.get("hdMediaUrl") if upscale_result else None,
        "upscaleAttempt": upscale_result.get("attempt", 0) if upscale_result else 0,
      "downloadUrl": (
        str(upscale_result.get("hdMediaUrl") or "").strip() if upscale_result else ""
      ) or direct_video_url or str(last_event.get("videoUrl") or "").strip(),
    }


async def api_run_image_to_video_jobs(
    page,
    jobs: list[dict],  # List of {"image_path": Path, "prompt": str}
    statsig_headers: dict,
    cfg: ImageToVideoConfig,
    timeout_seconds: int,
) -> list[dict]:
    """Run multiple image-to-video jobs concurrently.
    
    Each job dict should have:
        - image_path: Path to image file
        - prompt: Text prompt for video
    """
    
    import asyncio
    
    async def run_one(idx: int, job: dict) -> dict:
        image_path = Path(job["image_path"])
        prompt = job.get("prompt", "")
        return await api_run_image_to_video_job(
            page,
            image_path=image_path,
            prompt=prompt,
            statsig_headers=statsig_headers,
            cfg=cfg,
            timeout_seconds=timeout_seconds,
            job_index=idx,
        )
    
    tasks = [run_one(i, job) for i, job in enumerate(jobs)]
    results = await asyncio.gather(*tasks)
    return list(results)
