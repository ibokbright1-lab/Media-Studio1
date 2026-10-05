"""Video / audio downloading via yt-dlp (any site yt-dlp supports, not just YouTube).

Design goals (unchanged from the original):
  * One simple, widely compatible format selector (H.264 + AAC preferred) that never dead-ends.
  * Time ranges use yt-dlp's section download; if that fails for an ordinary reason we fall back
    to: download everything, then cut locally with ffmpeg.
  * Clear, actionable messages for real-world failures.
  * Unsafe URLs (localhost / private network) are refused -- required before this is a website.

What changed for hosting on Render:
  * Invalid `extractor_args` removed. Default yt-dlp client selection is used (it already picks the
    right clients, and uses the cookie-capable ones when cookies are present). Optional override via
    the YT_PLAYER_CLIENTS env var, in the correct dict format.
  * Cookies: found in env path / Render Secret File / repo root, validated, and copied to a private
    temp file per call (yt-dlp writes back to the jar, and calls can run concurrently).
  * Optional residential proxy for YouTube only (YT_PROXY) -- the real fix for datacenter-IP blocks.
  * Optional PO-token provider URL (YT_POT_BASE_URL) for the bgutil plugin.
  * Error classification is stricter (age-gate no longer reported as "bot check", "format not
    available" no longer reported as "video unavailable"); the range fallback no longer retries
    when the failure is a block that a second download can't fix.
  * Output filename is byte-safe (255-byte limit) and emoji-free, so what is on disk matches what
    the route serves (root cause of the 404s on titles with emojis). The result also carries
    "filename" -- serve that, never rebuild it from the title.
  * Live streams are refused (they would download forever on a public server).
  * yt-dlp output goes to the `downloader` logger, so Render logs show the real reason.

Environment variables (all optional):
  YT_PROXY            e.g. http://user:pass@host:port   (used for YouTube links only)
  YT_COOKIES_FILE     path to a Netscape-format cookies.txt (Render Secret Files mount at /etc/secrets/)
  YT_POT_BASE_URL     e.g. http://127.0.0.1:4416        (bgutil PO-token provider HTTP server)
  YT_PLAYER_CLIENTS   e.g. tv,web_safari,mweb           (override yt-dlp's default client choice)
  YT_VERBOSE          1 = log yt-dlp debug output at WARNING level (visible in Render logs)
"""
import contextlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import config
from core.errors import MediaError
from core.ffmpeg_utils import require_ffmpeg
from core.trim import trim_media

log = logging.getLogger("downloader")

ROOT = Path(__file__).resolve().parent.parent
VIDEO_HEIGHTS = [2160, 1440, 1080, 720, 480, 360]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SKIP_SUFFIXES = (".part", ".ytdl", ".temp", ".json", ".jpg", ".jpeg", ".png",
                  ".webp", ".vtt", ".srt", ".description")


def _setting(name, default=None):
    """Env var first, then config.py attribute, then default."""
    v = os.getenv(name)
    if v not in (None, ""):
        return v
    return getattr(config, name, None) or default


# ---------------------------------------------------------------- safety / helpers
def validate_url(url: str) -> str:
    url = (url or "").strip()
    if len(url) > 2048:
        raise MediaError("That link is too long.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise MediaError("That doesn't look like a valid link (it must start with http:// or https://).")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except (socket.gaierror, UnicodeError):
        raise MediaError("Couldn't find that website. Check the link and your internet connection.")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            raise MediaError("Couldn't verify that link.")
        if not ip.is_global or ip.is_multicast:
            raise MediaError("Links to private or local addresses are not allowed.")
    return url


def video_selector(height=None) -> str:
    h = f"[height<={int(height)}]" if height else ""
    return (f"bv*{h}[vcodec^=avc1]+ba[ext=m4a]/"      # best case: plays everywhere, no re-encode
            f"bv*{h}[ext=mp4]+ba[ext=m4a]/"
            f"b{h}[ext=mp4]/"
            f"bv*{h}+ba/b{h}/b")                       # never dead-ends


def _classify(raw: str) -> str:
    """Map a raw yt-dlp error to a short category. Order matters."""
    m = _ANSI.sub("", raw or "").lower()
    if re.search(r"confirm your age|age[- ]restrict|age verification|inappropriate for some users", m):
        return "age"
    if "not a bot" in m or "sign in to confirm" in m:
        return "bot"
    if re.search(r"http error 429|too many requests", m):
        return "rate"
    if re.search(r"http error 403|forbidden|po token|js runtime|javascript runtime|"
                 r"challenge|sabr|no supported javascript", m):
        return "blocked"
    if re.search(r"private video|video is private|this video is private", m):
        return "private"
    if re.search(r"members[- ]only|join this channel|login required|requires login", m):
        return "members"
    if re.search(r"requested format is not available|format is not available", m):
        return "format"
    if re.search(r"live event|is live|live stream|premieres in|will begin in", m):
        return "live"
    if re.search(r"video unavailable|has been removed|no longer available|not available|"
                 r"blocked it|copyright", m):
        return "unavailable"
    if "unsupported url" in m:
        return "unsupported"
    if re.search(r"timed out|timeout|urlopen error|getaddrinfo|connection reset|connection refused|"
                 r"temporary failure|name resolution|remote end closed|proxy", m):
        return "network"
    if "ffmpeg" in m and ("not found" in m or "not installed" in m):
        return "ffmpeg"
    return "other"


def friendly_error(raw: str) -> str:
    raw = _ANSI.sub("", raw or "")
    kind = _classify(raw)

    # Operator hints go to the server log, not to the website visitor.
    if kind == "bot":
        log.error("[operator] YouTube bot check. This is almost always the server's cloud IP. Fix: set "
                  "YT_PROXY (residential), run the bgutil PO-token provider (YT_POT_BASE_URL), and use "
                  "fresh cookies from a private browser window. Raw: %s", raw.strip()[:300])
    elif kind == "blocked":
        log.error("[operator] Site returned 403 / challenge failure. Fix: update yt-dlp (nightly), make "
                  "sure Deno is installed and on PATH, and use a PO-token provider. Raw: %s", raw.strip()[:300])

    messages = {
        "bot": "YouTube is temporarily blocking this server. Please try again later or try another link.",
        "blocked": "The site blocked the download (verification failed). Please try again later.",
        "rate": "Too many requests right now. Please wait a few minutes and try again.",
        "age": "This video is age-restricted and can't be downloaded here.",
        "private": "This video is private.",
        "members": "This video is for members only or requires a login.",
        "format": "That quality isn't available for this link. Please pick another option.",
        "live": "This is a live stream or an upcoming premiere that hasn't finished yet.",
        "unavailable": "This video is unavailable or has been removed (or it is blocked in this region).",
        "unsupported": "This link isn't supported.",
        "network": "Network problem while reaching the site. Please try again.",
        "ffmpeg": "FFmpeg is not installed or not on PATH.",
    }
    if kind in messages:
        return messages[kind]
    return f"Download failed: {raw.strip()[:300]}"


def _safe_title(t: str, max_bytes: int = 150) -> str:
    """Filesystem- and URL-safe title. Drops emoji / symbols, keeps letters (any language),
    digits and basic punctuation, and caps the length in BYTES (filesystems limit names to 255)."""
    t = unicodedata.normalize("NFKC", t or "")
    t = re.sub(r"[^\w\s\-.()\[\]&',!+@#\u0300-\u036f]", "", t)
    t = re.sub(r"\s+", " ", t).strip(" .")
    while len(t.encode("utf-8")) > max_bytes:
        t = t[:-1]
    return t.strip(" .") or "download"


# ---------------------------------------------------------------- yt-dlp setup
def _yt():
    try:
        import yt_dlp
    except ImportError:
        raise MediaError("yt-dlp is not installed. Run:  pip install -U yt-dlp")
    _startup_checks()
    return yt_dlp


def ytdlp_version_warning():
    """YouTube changes constantly; an old yt-dlp is the #1 cause of broken downloads."""
    try:
        from yt_dlp.version import __version__ as v
        d = datetime.strptime(".".join(v.split(".")[:3]), "%Y.%m.%d")
        if (datetime.now() - d).days > 30:
            return f"yt-dlp ({v}) is over a month old. Rebuild with: pip install -U --pre 'yt-dlp[default]'"
    except Exception:
        pass
    return None


_checked = False


def _startup_checks():
    """One-time sanity log so Render logs explain failures before they happen."""
    global _checked
    if _checked:
        return
    _checked = True
    if not any(shutil.which(r) for r in ("deno", "node", "bun", "qjs")):
        log.warning("No JavaScript runtime (deno/node/bun/quickjs) on PATH. Current yt-dlp needs one "
                    "to solve YouTube's challenges. Install Deno in your Render build.")
    w = ytdlp_version_warning()
    if w:
        log.warning(w)
    if not _cookie_source():
        log.info("No valid cookies.txt found (YouTube may demand sign-in from a cloud IP).")
    if not _setting("YT_PROXY"):
        log.info("YT_PROXY not set: YouTube requests go out from this server's own (datacenter) IP.")


def _is_youtube(url: str) -> bool:
    h = (urlparse(url or "").hostname or "").lower()
    return h == "youtu.be" or h.endswith("youtube.com") or h.endswith("youtube-nocookie.com")


is_youtube = _is_youtube     # public name used by route.py / relay


def _cookie_source():
    """Return the first usable Netscape-format cookie file, or None."""
    candidates = []
    env = os.getenv("YT_COOKIES_FILE")
    if env:
        candidates.append(Path(env))
    candidates += [Path("/etc/secrets/cookies.txt"), ROOT / "cookies.txt"]
    for c in candidates:
        try:
            if not (c.is_file() and c.stat().st_size > 0):
                continue
            head = c.read_text(encoding="utf-8", errors="ignore")[:300].lstrip("\ufeff")
            first = head.splitlines()[0] if head else ""
            if re.match(r"#\s*(Netscape )?HTTP Cookie File", first):
                return c
            log.warning("Ignoring %s: first line must be '# Netscape HTTP Cookie File' "
                        "(re-export it as Netscape format).", c)
        except OSError:
            continue
    return None


def diagnostics() -> dict:
    """Non-secret server config summary (names/booleans only) for /api/health."""
    import importlib.util
    return {
        "js_runtime": next((r for r in ("deno", "node", "bun", "qjs") if shutil.which(r)), None),
        "ejs": importlib.util.find_spec("yt_dlp_ejs") is not None,
        "cookies": bool(_cookie_source()),
        "proxy": bool(_setting("YT_PROXY")),
        "pot_provider": bool(_setting("YT_POT_BASE_URL")),
    }


class _YTLogger:
    """Route yt-dlp output into logging so it shows up in Render's log stream."""
    def debug(self, msg):
        (log.warning if _setting("YT_VERBOSE") == "1" else log.debug)("yt-dlp: %s", msg)

    def info(self, msg):
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg):
        log.warning("yt-dlp: %s", msg)

    def error(self, msg):
        log.error("yt-dlp: %s", msg)


@contextlib.contextmanager
def _ydl_opts(url: str = "", **extra):
    """Yield a fresh yt-dlp options dict; clean up the temp cookie copy afterwards."""
    tmp_cookie = None
    o = {
        "logger": _YTLogger(),
        "color": "no_color",
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "noprogress": True,
    }
    if _setting("YT_VERBOSE") == "1":
        o["verbose"] = True

    ea = {}
    clients = _setting("YT_PLAYER_CLIENTS")
    if clients:
        ea["youtube"] = {"player_client": [c.strip() for c in clients.split(",") if c.strip()]}
    pot = _setting("YT_POT_BASE_URL")
    if pot:
        ea["youtubepot-bgutilhttp"] = {"base_url": [pot]}
    if ea:
        o["extractor_args"] = ea

    proxy = _setting("YT_PROXY")
    if proxy and _is_youtube(url):
        o["proxy"] = proxy                 # YouTube only: saves proxy bandwidth on other sites
    else:
        o["source_address"] = "0.0.0.0"    # force IPv4 (more reliable on hosts)

    src = _cookie_source()
    if src:
        fd, tmp_cookie = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
        os.close(fd)
        shutil.copyfile(src, tmp_cookie)
        o["cookiefile"] = tmp_cookie
    elif _setting("COOKIES_FROM_BROWSER"):
        o["cookiesfrombrowser"] = (_setting("COOKIES_FROM_BROWSER"),)   # home machine only

    o.update(extra)
    try:
        yield o
    finally:
        if tmp_cookie:
            Path(tmp_cookie).unlink(missing_ok=True)


# ---------------------------------------------------------------- public API
def fetch_info(url: str) -> dict:
    url = validate_url(url)
    yt = _yt()
    try:
        with _ydl_opts(url, skip_download=True) as opts:
            with yt.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
    except MediaError:
        raise
    except Exception as e:
        log.warning("fetch_info failed for %s: %s", url, _ANSI.sub("", str(e))[:500])
        raise MediaError(friendly_error(str(e)))

    if not info:
        raise MediaError("No information could be retrieved for this link.")
    if info.get("entries"):
        first = next(iter(info["entries"]), None)
        if not first:
            raise MediaError("No information could be retrieved for this link.")
        info = first

    video_formats = []
    audio_formats = []

    for f in info.get("formats") or []:
        format_id = f.get("format_id")
        ext = f.get("ext")
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")

        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        size_mb = round(filesize / (1024 * 1024), 2) if filesize else None

        if vcodec == "none" and acodec != "none":
            audio_formats.append({
                "format_id": format_id,
                "bitrate": f.get("abr") or 0,
                "size_mb": size_mb,
                "ext": ext,
            })
        elif vcodec != "none":
            codec = (vcodec or "").split(".")[0]
            video_formats.append({
                "format_id": format_id,
                "height": f.get("height") or 0,
                "fps": f.get("fps") or 0,
                "size_mb": size_mb,
                "ext": ext,
                "codec": codec,
            })

    audio_formats.sort(key=lambda x: x["bitrate"], reverse=True)
    # Within the same height, prefer H.264 (plays on every phone) over AV1/VP9 when de-duplicating.
    video_formats.sort(key=lambda x: (x["height"], x["codec"].startswith("avc"), x["fps"]), reverse=True)

    unique_videos, seen_videos = [], set()
    for v in video_formats:
        key = (v["height"], v["ext"])
        if key not in seen_videos:
            seen_videos.add(key)
            unique_videos.append(v)

    unique_audios, seen_audios = [], set()
    for a in audio_formats:
        key = (a["bitrate"], a["ext"])
        if key not in seen_audios:
            seen_audios.add(key)
            unique_audios.append(a)

    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "is_live": bool(info.get("is_live")),
        "videos": unique_videos,
        "audios": unique_audios,
        "warning": None,
    }


def download(url: str, out_dir, kind="video", height=720, format_id=None, start=None, end=None,
             on_progress=None, cancel=None, on_message=None) -> dict:
    """Download to out_dir using an exact format_id or falling back to best match.

    Returns {"path", "filename", "title", "notes"}. Serve `filename` (it is the real name on disk).
    """
    url = validate_url(url)
    require_ffmpeg()
    yt = _yt()
    from yt_dlp.utils import download_range_func, match_filter_func

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
        for f in out_dir.glob("download.*"):
            f.unlink(missing_ok=True)
        state["done"] = 0

        with _ydl_opts(url,
                       outtmpl=str(out_dir / "download.%(ext)s"),
                       progress_hooks=[hook],
                       match_filter=match_filter_func("!is_live")) as opts:
            if format_id:
                fid = str(format_id)
                if kind == "video":
                    # chosen video track + best audio (m4a preferred); if the chosen format already
                    # carries audio (TikTok/Facebook) or no separate audio exists, use it as-is.
                    opts["format"] = f"{fid}+ba[ext=m4a]/{fid}+ba/{fid}/b"
                    opts["merge_output_format"] = "mp4/mkv"
                else:
                    opts["format"] = f"{fid}/ba/b"
            elif kind == "audio":
                opts["format"] = "bestaudio/best"
            else:
                opts["format"] = video_selector(height)
                opts["merge_output_format"] = "mp4/mkv"

            max_mb = _setting("MAX_FILESIZE_MB")
            if max_mb:
                opts["max_filesize"] = int(float(max_mb) * 1024 * 1024)

            if with_range:
                opts["download_ranges"] = download_range_func(
                    None, [(start or 0, end if end is not None else float("inf"))])
                opts["force_keyframes_at_cuts"] = True

            with yt.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

    cut_locally = False
    try:
        if wants_range:
            if on_message:
                on_message("Downloading only the part you selected…")
            try:
                info = run(True)
            except MediaError:
                raise
            except Exception as e:
                if cancel and cancel():
                    raise MediaError("Cancelled.")
                # Only fall back for ordinary section-download failures. Blocks, private/removed
                # videos, bad formats etc. would fail again on a full download.
                if _classify(str(e)) != "other":
                    raise
                log.warning("Section download failed (%s); falling back to full download + cut.",
                            _ANSI.sub("", str(e))[:300])
                notes.append("Section download wasn't possible for this link, so the whole file "
                             "was downloaded and then cut.")
                if on_message:
                    on_message("Downloading the full file, then cutting…")
                info = run(False)
                cut_locally = True
                _cut_after(out_dir, start, end)
        else:
            info = run(False)
    except MediaError:
        raise
    except Exception as e:
        if cancel and cancel():
            raise MediaError("Cancelled.")
        log.warning("download failed for %s: %s", url, _ANSI.sub("", str(e))[:500])
        raise MediaError(friendly_error(str(e)))

    if info and info.get("entries"):
        info = next(iter(info["entries"]), None) or info

    src = _find_output(out_dir, info, use_info_path=not cut_locally)
    if not src:
        if info and info.get("is_live"):
            raise MediaError(friendly_error("live stream"))
        raise MediaError("The download finished but no file was produced. Try again, or try another quality.")

    title = (info or {}).get("title")
    final = out_dir / f"{_safe_title(title)}{src.suffix}"
    src.replace(final)

    warn = ytdlp_version_warning()
    return {
        "path": str(final),
        "filename": final.name,
        "title": title,
        "notes": notes + ([warn] if warn and not notes else []),
    }


# ---------------------------------------------------------------- internals
def _find_output(out_dir: Path, info, use_info_path: bool):
    """Locate the finished file: trust yt-dlp's own record first, else the largest real output."""
    if use_info_path and info:
        for rd in info.get("requested_downloads") or []:
            p = rd.get("filepath")
            if p and Path(p).is_file():
                return Path(p)
    files = [f for f in out_dir.glob("download.*")
             if f.is_file() and f.suffix.lower() not in _SKIP_SUFFIXES]
    return max(files, key=lambda f: f.stat().st_size) if files else None


def _cut_after(out_dir: Path, start, end):
    f = _find_output(out_dir, None, use_info_path=False)
    if not f:
        raise MediaError("Nothing was downloaded to cut.")
    res = trim_media(str(f), out_dir, start or 0, end, mode="precise")
    res_path = Path(res["path"])
    if res_path.resolve() != f.resolve():
        f.unlink(missing_ok=True)
    target = out_dir / f"download{res_path.suffix}"
    if res_path.resolve() != target.resolve():
        res_path.replace(target)
