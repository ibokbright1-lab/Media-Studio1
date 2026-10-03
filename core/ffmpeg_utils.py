"""Thin, safe wrappers around ffmpeg / ffprobe (no shell, progress parsing, probing)."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

from core.errors import MediaError


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("FFmpeg is not installed or not on PATH. Install it from https://ffmpeg.org")


def probe(path) -> dict:
    """Return a simple summary of a media file."""
    require_ffmpeg()
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise MediaError("That file could not be read as audio/video. It may be corrupted or unsupported.")
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams
                  if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not audio and not video:
        raise MediaError("No audio or video stream found in this file.")
    duration = float(fmt.get("duration") or (audio or video or {}).get("duration") or 0)
    size = int(fmt.get("size") or Path(path).stat().st_size)
    bitrate = None
    if audio and audio.get("bit_rate"):
        bitrate = int(audio["bit_rate"]) // 1000
    elif duration and not video:
        bitrate = int(size * 8 / duration / 1000)
    return {
        "duration": duration, "size": size, "has_video": video is not None, "has_audio": audio is not None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_bitrate_kbps": bitrate,
        "channels": int(audio.get("channels", 2)) if audio else None,
        "sample_rate": int(audio.get("sample_rate", 44100)) if audio else None,
        "format": fmt.get("format_name"),
        "has_cover_art": any(s.get("disposition", {}).get("attached_pic") for s in streams),
    }


def run_ffmpeg(args: list, duration: float = None, on_progress=None, cancel=None) -> None:
    """Run ffmpeg, reporting progress 0-100. Raises MediaError with a readable message on failure."""
    require_ffmpeg()
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-nostats"] + [str(a) for a in args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stderr_chunks = []
    t = threading.Thread(target=lambda: stderr_chunks.append(p.stderr.read()), daemon=True)
    t.start()
    for line in p.stdout:
        if cancel and cancel():
            p.kill()
            raise MediaError("Cancelled.")
        line = line.strip()
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                secs = int(line.split("=")[1]) / 1_000_000
            except ValueError:
                continue
            if on_progress and duration:
                on_progress(min(99.0, secs / duration * 100))
    p.wait()
    t.join(timeout=2)
    p.stdout.close()
    p.stderr.close()
    if p.returncode != 0:
        err = (stderr_chunks[0] if stderr_chunks else "").strip()[-400:]
        raise MediaError(f"FFmpeg failed: {err or 'unknown error'}")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
