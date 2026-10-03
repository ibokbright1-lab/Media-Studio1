"""Video / audio downloading via yt-dlp (any site yt-dlp supports, not just YouTube).

Why this is more reliable than the old version:
  * One simple, widely compatible format selector (H.264 + AAC preferred) instead of AV1/Opus
    with hard filters that could dead-end.
  * Time ranges use yt-dlp's own section download (fast, small), and if that fails for ANY
    reason we automatically fall back to: download everything, then cut locally with ffmpeg.
  * Clear, actionable messages for the real-world failures (bot check, 403, outdated yt-dlp).
  * Unsafe URLs (localhost / private network) are refused -- required before this is a website.
"""
import glob
import ipaddress
import re
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import config
from core.errors import MediaError
from core.ffmpeg_utils import require_ffmpeg
from core.trim import trim_media

VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]


# ---------------------------------------------------------------- safety / helpers
def validate_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise MediaError("That doesn't look like a valid link (it must start with http:// or https://).")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise MediaError("Couldn't find that website. Check the link and your internet connection.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise MediaError("Links to private or local addresses are not allowed.")
    return url


def video_selector(height=None) -> str:
    h = f"[height<={int(height)}]" if height else ""
    return (f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/"      # best case: plays everywhere, no re-encode
            f"bv*{h}[ext=mp4]+ba[ext=m4a]/"
            f"b{h}[ext=mp4]/"
            f"bv*{h}+ba/b{h}/b")                       # never dead-ends


def friendly_error(raw: str) -> str:
    m = raw.lower()
    if "sign in" in m or "not a bot" in m or "confirm you" in m:
        return ("The site is asking to confirm you're not a bot. Export cookies.txt from a logged-in browser "
                "and save it next to this app (see README), or set MS_COOKIES_BROWSER=chrome.")
    if "403" in m or "forbidden" in m or "po token" in m or "challenge" in m or "js runtime" in m:
        return ("The site blocked the download (HTTP 403 / verification). Fix: run  pip install -U yt-dlp  "
                "and install Deno (https://deno.com) so yt-dlp can solve YouTube's checks. See README.")
    if "private" in m:
        return "This video is private."
    if "unavailable" in m or "removed" in m or "not available" in m:
        return "This video is unavailable or has been removed (or it is blocked in your country)."
    if "age" in m and "restrict" in m:
        return "This video is age-restricted. Add cookies from a logged-in account (see README)."
    if "unsupported url" in m:
        return "This link isn't supported."
    if "timed out" in m or "timeout" in m or "urlopen error" in m or "getaddrinfo" in m:
        return "Network problem while reaching the site. Check your connection and try again."
    if "ffmpeg" in m and ("not found" in m or "not installed" in m):
        return "FFmpeg is not installed or not on PATH."
    if "live" in m and "event" in m:
        return "This is a live stream that hasn't finished yet."
    return f"Download failed: {raw.strip()[:300]}"


def _yt():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        raise MediaError("yt-dlp is not installed. Run:  pip install -U yt-dlp")


def ytdlp_version_warning():
    """YouTube changes constantly; an old yt-dlp is the #1 cause of broken downloads."""
    try:
        from yt_dlp.version import __version__ as v
        d = datetime.strptime(".".join(v.split(".")[:3]), "%Y.%m.%d")
        if (datetime.now() - d).days > 60:
            return f"Your yt-dlp ({v}) is over 2 months old. Run: pip install -U yt-dlp"
    except Exception:
        pass
    return None


def _base_opts() -> dict:
    "cookiefile": "cookies.txt",
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "retries": 10, "fragment_retries": 10,
         "socket_timeout": 30, "concurrent_fragment_downloads": 4, "windowsfilenames": True,
         "noprogress": True}
    if config.COOKIES_FROM_BROWSER:
        o["cookiesfrombrowser"] = (config.COOKIES_FROM_BROWSER,)
    elif Path(config.COOKIES_FILE).is_file():
        o["cookiefile"] = config.COOKIES_FILE
    return o


def _safe_title(t: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", t or "download").strip(" .")
    return (t or "download")[:120]


# ---------------------------------------------------------------- public API
def fetch_info(url: str) -> dict:
    url = validate_url(url)
    yt = _yt()
    try:
        with yt.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise MediaError(friendly_error(str(e)))
        
    if not info:
        raise MediaError("No information could be retrieved for this link.")
    if info.get("entries"):
        info = next(iter(info["entries"]))

    video_formats = []
    audio_formats = []

    for f in info.get("formats", []):
        format_id = f.get("format_id")
        ext = f.get("ext")
        
        # Calculate file size in MB safely
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        size_mb = round(filesize / (1024 * 1024), 2) if filesize else None

        # Audio-only streams
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            audio_formats.append({
                "format_id": format_id,
                "bitrate": f.get("abr") or 0,
                "size_mb": size_mb,
                "ext": ext
            })
            
        # Video streams (we filter out purely audio streams)
        elif f.get("vcodec") != "none":
            video_formats.append({
                "format_id": format_id,
                "height": f.get("height") or 0,
                "fps": f.get("fps") or 0,
                "size_mb": size_mb,
                "ext": ext
            })

    # Sort arrays so highest quality appears at the top of the frontend table
    audio_formats = sorted(audio_formats, key=lambda x: x["bitrate"], reverse=True)
    video_formats = sorted(video_formats, key=lambda x: (x["height"], x["fps"]), reverse=True)
    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "is_live": bool(info.get("is_live")),
        "videos": video_formats,
        "audios": audio_formats,
        "warning": ytdlp_version_warning()
    }

def download(url: str, out_dir, kind="video", height=720, format_id=None, start=None, end=None,
             on_progress=None, cancel=None, on_message=None) -> dict:
    """Download to out_dir using an exact format_id or falling back to best match."""
    url = validate_url(url)
    require_ffmpeg()
    yt = _yt()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wants_range = start is not None or end is not None
    notes = []

    state = {"done": 0}

    def hook(d):
        if cancel and cancel():
            raise MediaError("Cancelled.")
        info = d.get("info_dict") or {}
        streams = max(1, len(info.get("requested_formats") or [1]))
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes", 0) / total) if total else 0
            if on_progress:
                on_progress(min(95.0, (state["done"] + frac) / streams * 100))
        elif d["status"] == "finished":
            state["done"] += 1

    def run(with_range: bool) -> dict:
        for f in glob.glob(str(out_dir / "download.*")):
            Path(f).unlink(missing_ok=True)
        state["done"] = 0
        opts = {**_base_opts(), "outtmpl": str(out_dir / "download.%(ext)s"), "progress_hooks": [hook]}
        
        # Format Selection Logic
        if format_id:
            if kind == "video":
                # Ensures we grab the exact video track + the best audio track
                opts["format"] = f"{format_id}+bestaudio/best"
                opts["merge_output_format"] = "mp4/mkv"
            else:
                opts["format"] = format_id
        elif kind == "audio":
            opts["format"] = "bestaudio/best"
        else:
            opts["format"] = video_selector(height)
            opts["merge_output_format"] = "mp4/mkv"

        if with_range:
            from yt_dlp.utils import download_range_func
            opts["download_ranges"] = download_range_func(None, [(start or 0, end if end is not None else float("inf"))])
            opts["force_keyframes_at_cuts"] = True
            
        with yt.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        if wants_range:
            if on_message:
                on_message("Downloading only the part you selected…")
            try:
                info = run(True)
            except MediaError:
                raise
            except Exception:
                notes.append("Section download wasn't possible for this link, so the whole file was downloaded and then cut.")
                if on_message:
                    on_message("Downloading the full file, then cutting…")
                info = run(False)
                wants_range = False
                _cut_after(out_dir, start, end, kind, notes)
        else:
            info = run(False)
    except MediaError:
        raise
    except Exception as e:
        raise MediaError(friendly_error(str(e)))

    if info and info.get("entries"):
        info = next(iter(info["entries"]))
    files = [f for f in glob.glob(str(out_dir / "download.*")) if not f.endswith((".part", ".ytdl", ".temp"))]
    if not files:
        raise MediaError("The download finished but no file was produced. Try again, or update yt-dlp.")
        
    src = Path(max(files, key=lambda f: Path(f).stat().st_size))
    final = out_dir / f"{_safe_title((info or {}).get('title'))}{src.suffix}"
    src.rename(final)
    warn = ytdlp_version_warning()
    return {"path": str(final), "title": (info or {}).get("title"), "notes": notes + ([warn] if warn and not notes else [])}

def _cut_after(out_dir: Path, start, end, kind, notes):
    f = next((x for x in glob.glob(str(out_dir / "download.*")) if not x.endswith((".part", ".ytdl"))), None)
    if not f:
        raise MediaError("Nothing was downloaded to cut.")
    res = trim_media(f, out_dir, start or 0, end, mode="precise")
    Path(f).unlink(missing_ok=True)
    Path(res["path"]).rename(out_dir / f"download{Path(res['path']).suffix}")
