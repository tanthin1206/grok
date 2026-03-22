from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".webm", ".m4v"}
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def _win_hidden_kwargs() -> dict:
    if os.name != "nt":
        return {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        return {"startupinfo": si, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    except Exception:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    prev_error_mode = None
    try:
        if os.name == "nt":
            try:
                import ctypes
                sem_flags = 0x0001 | 0x0002 | 0x8000
                prev_error_mode = ctypes.windll.kernel32.SetErrorMode(sem_flags)
            except Exception:
                prev_error_mode = None
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_win_hidden_kwargs(),
        )
    except OSError as exc:
        raise RuntimeError(f"Không chạy được FFmpeg nội bộ: {exc}") from exc
    finally:
        if os.name == "nt" and prev_error_mode is not None:
            try:
                import ctypes
                ctypes.windll.kernel32.SetErrorMode(int(prev_error_mode))
            except Exception:
                pass


def is_video_file(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS


def filter_existing_videos(video_paths: list[str | Path]) -> list[str]:
    result: list[str] = []
    for item in video_paths or []:
        path = str(item or "").strip()
        if not path:
            continue
        if is_video_file(path):
            result.append(path)
    return result


def _ensure_dir(path: str | Path) -> str:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _safe_output_path(output_dir: str | Path, stem: str, suffix: str = ".mp4") -> str:
    out_dir = Path(_ensure_dir(output_dir))
    base = out_dir / f"{stem}{suffix}"
    if not base.exists():
        return str(base)
    idx = 1
    while True:
        candidate = out_dir / f"{stem}_{idx:03d}{suffix}"
        if not candidate.exists():
            return str(candidate)
        idx += 1


def merge_videos(video_paths: list[str | Path], output_dir: str | Path, output_stem: str = "video_da_noi") -> str:
    videos = filter_existing_videos(video_paths)
    if len(videos) < 2:
        raise ValueError("Cần ít nhất 2 video hợp lệ để nối.")

    out_path = _safe_output_path(output_dir, output_stem, ".mp4")

    list_file = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
            list_file = tf.name
            for path in videos:
                safe = str(Path(path).resolve()).replace("'", "'\\''")
                tf.write(f"file '{safe}'\n")

        cmd_copy = [
            FFMPEG_PATH,
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            "-y",
            out_path,
        ]
        copy_ret = _run_ffmpeg(cmd_copy)
        if copy_ret.returncode == 0 and os.path.isfile(out_path):
            return out_path

        cmd_encode = [
            FFMPEG_PATH,
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-c:v", "libx264",
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-y",
            out_path,
        ]
        enc_ret = _run_ffmpeg(cmd_encode)
        if enc_ret.returncode != 0 or not os.path.isfile(out_path):
            err = (enc_ret.stderr or copy_ret.stderr or "Unknown ffmpeg error")[-600:]
            raise RuntimeError(f"Nối video thất bại: {err}")
        return out_path
    finally:
        if list_file and os.path.isfile(list_file):
            try:
                os.remove(list_file)
            except Exception:
                pass


def extract_last_frames(video_paths: list[str | Path], output_dir: str | Path) -> list[str]:
    videos = filter_existing_videos(video_paths)
    if not videos:
        raise ValueError("Không có video hợp lệ để cắt frame cuối.")

    out_dir = Path(_ensure_dir(output_dir))
    outputs: list[str] = []

    for idx, video in enumerate(videos, 1):
        src = Path(video)
        out_name = f"{src.stem}_last_{idx:03d}.jpg"
        out_path = out_dir / out_name
        cmd = [
            FFMPEG_PATH,
            "-sseof", "-0.10",
            "-i", str(src),
            "-vframes", "1",
            "-q:v", "5",
            "-y",
            str(out_path),
        ]
        ret = _run_ffmpeg(cmd)
        if ret.returncode != 0 or not out_path.is_file():
            err = (ret.stderr or "Unknown ffmpeg error")[-400:]
            raise RuntimeError(f"Cắt frame cuối thất bại ({src.name}): {err}")
        outputs.append(str(out_path))

    return outputs
