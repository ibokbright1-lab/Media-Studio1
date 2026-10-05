"""HTTP layer: validates input, starts jobs, serves results. All real work lives in core/."""
import os
import re
import shutil
import urllib.parse
from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

import config
from core import downloader, relay, silence
from core.compress import FORMATS, PRESETS, compress_audio
from core.errors import MediaError
from core.ffmpeg_utils import human_size, probe
from core.jobs import cleanup_old_files
from core.pipeline import run_download
from core.timeparse import parse_time
from core.trim import EXTRACT_FORMATS, trim_media

bp = Blueprint("web", __name__)
ALLOWED = config.AUDIO_EXTS | config.VIDEO_EXTS

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FORMAT_ID_RE = re.compile(r"^[\w.\-=]{1,64}$")   # plain yt-dlp format ids only, never selector syntax


def jobs():
    return current_app.config["JOBS"]


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.errorhandler(MediaError)
def _media_error(e):
    return _err(str(e))


@bp.errorhandler(RequestEntityTooLarge)
def _too_big(e):
    return _err(f"File too large (limit {config.MAX_UPLOAD_MB} MB).", 413)


@bp.app_errorhandler(Exception)
def _unexpected(e):
    """Always answer with JSON so the front-end never chokes on an HTML error page."""
    if isinstance(e, HTTPException):
        return _err(e.description or e.name, e.code or 500)
    current_app.logger.exception("Unhandled error")
    return _err("Something went wrong on the server. Please try again.", 500)


HOME_MODE = os.getenv("HOME_MODE") == "1"     # set ONLY on the home computer


@bp.before_request
def _home_guard():
    """On the home machine only the relay (holding the shared key) may use the API; no web UI."""
    if not HOME_MODE:
        return None
    if request.path == "/":
        return _err("Not found.", 404)
    if request.path.startswith("/api/") and not relay.check_key(request.headers.get("X-Relay-Key")):
        return _err("Forbidden.", 403)
    return None


@bp.after_request
def _cors_files(resp):
    """Lets the browser fetch files from the home server after Render redirects it there."""
    origin = os.getenv("CORS_ORIGIN")
    if origin and request.path.startswith("/files/"):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Content-Length"
    return resp


def _relay_json(method, path, payload=None, timeout=20):
    """Forward to the home server. Returns (code, body), or None if it is unreachable (fall back)."""
    try:
        return relay.call(method, path, payload, timeout=timeout)
    except relay.RelayDown as e:
        current_app.logger.warning("Home relay unreachable (%s); falling back to local.", e)
        return None


def _remote_job(method, job_id, suffix=""):
    if not _JOB_ID_RE.match(job_id):
        return _err("Job not found (it may have expired).", 404)
    try:
        code, body = relay.call(method, f"/api/jobs/{relay.strip_prefix(job_id)}{suffix}",
                                {} if method == "POST" else None, timeout=15)
    except relay.RelayDown:
        return _err("The download server is unreachable right now. Please try again.", 503)
    return jsonify(relay.rewrite_urls(body)), code


def _save_upload(job_id: str) -> Path:
    f = request.files.get("file")
    if not f or not f.filename:
        raise MediaError("Please choose a file first.")
    name = secure_filename(f.filename) or "upload"
    if Path(name).suffix.lower() not in ALLOWED:
        raise MediaError("That file type isn't supported. Use a common audio or video file (mp3, wav, m4a, mp4, mkv…).")
    d = config.UPLOAD_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    f.save(path)
    return path


def _num(name, default=None):
    v = request.form.get(name, request.args.get(name))
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        raise MediaError(f"Invalid number for '{name}'.")


def _flag(name):
    return request.form.get(name) in ("1", "true", "on", "yes")


def _start_job(kind, work):
    """work(job, in_path|None, out_dir) -> result dict (must contain 'file')."""
    cleanup_old_files()
    job_id = jobs().new_id()
    out_dir = config.OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    in_path = _save_upload(job_id) if kind in ("compress", "trim") else None

    def fn(job):
        res = work(job, in_path, out_dir)
        # URL-encode the filename so spaces, '#', '&' etc. survive the browser round trip.
        # res["file"] must be the REAL name on disk (core/pipeline.py: use downloader's "filename").
        safe_name = urllib.parse.quote(res["file"])
        res["url"] = f"/files/{job_id}/{safe_name}"
        res["size_human"] = human_size((out_dir / res["file"]).stat().st_size)
        res.pop("path", None)
        return res

    cleanup_dirs = [in_path.parent] if in_path else []
    jobs().submit(kind, fn, job_id=job_id, cleanup_dirs=cleanup_dirs)
    return jsonify({"job_id": job_id})


# ------------------------------------------------------------------ pages / meta
@bp.get("/")
def index():
    return render_template("index.html", presets=list(PRESETS), formats=list(FORMATS),
                           max_mb=config.MAX_UPLOAD_MB, ttl=config.FILE_TTL_HOURS, heights=downloader.VIDEO_HEIGHTS)


@bp.get("/api/health")
def health():
    try:
        import yt_dlp
        ytv = yt_dlp.version.__version__
    except Exception:
        ytv = None
    return jsonify({
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "yt_dlp": ytv,
        "warning": downloader.ytdlp_version_warning(),
        "home_relay": relay.enabled(),    # True = Render currently sees your home server online
        **downloader.diagnostics(),      # js_runtime, ejs, cookies, proxy, pot_provider (booleans/names only)
    })


# ------------------------------------------------------------------ features
@bp.post("/api/compress")
def api_compress():
    fmt, preset = request.form.get("fmt", "mp3"), request.form.get("preset", "balanced")
    opts = dict(fmt=fmt, preset=preset, strip_extras=_flag("strip"), remove_silence=_flag("silence"),
                silence_db=_num("silence_db", silence.DEFAULT_THRESHOLD_DB),
                silence_pause=_num("silence_pause", silence.DEFAULT_MAX_PAUSE),
                target_mb=_num("target_mb"))
    if fmt not in FORMATS or preset not in PRESETS:
        raise MediaError("Unknown format or quality preset.")

    def work(job, in_path, out_dir):
        job.update(2, "Compressing…")
        return compress_audio(in_path, out_dir, on_progress=job.update, cancel=lambda: job.cancelled, **opts)

    return _start_job("compress", work)


@bp.post("/api/analyze")
def api_analyze():
    tmp_id = jobs().new_id()
    try:
        path = _save_upload(tmp_id)
        info = probe(path)
        rep = silence.analyze(path, _num("silence_db", silence.DEFAULT_THRESHOLD_DB),
                              _num("silence_pause", silence.DEFAULT_MAX_PAUSE), info["duration"])
        rep.update(duration=round(info["duration"], 1))
        return jsonify(rep)
    finally:
        shutil.rmtree(config.UPLOAD_DIR / tmp_id, ignore_errors=True)


@bp.post("/api/trim")
def api_trim():
    start = parse_time(request.form.get("start", "0"))
    end_raw = request.form.get("end", "")
    end = parse_time(end_raw) if end_raw.strip() else None
    mode = request.form.get("mode", "precise")
    extract = request.form.get("extract") or None
    if mode not in ("precise", "fast") or (extract and extract not in EXTRACT_FORMATS):
        raise MediaError("Invalid trim options.")

    def work(job, in_path, out_dir):
        job.update(2, "Trimming…")
        return trim_media(in_path, out_dir, start, end, mode=mode, extract_audio=extract,
                          on_progress=job.update, cancel=lambda: job.cancelled)

    return _start_job("trim", work)


@bp.post("/api/info")
def api_info():
    d = request.get_json(silent=True) or request.form
    url = d.get("url", "")
    if not HOME_MODE and relay.enabled() and downloader.is_youtube(url):
        downloader.validate_url(url)
        r = _relay_json("POST", "/api/info", {"url": url}, timeout=60)
        if r:
            return jsonify(r[1]), r[0]
    return jsonify(downloader.fetch_info(url))


@bp.post("/api/download")
def api_download():
    d = request.get_json(silent=True) or request.form
    url = downloader.validate_url(d.get("url", ""))
    kind = d.get("kind", "video")
    if kind not in ("video", "audio"):
        raise MediaError("Invalid download type.")

    height = None
    if d.get("height"):
        try:
            height = int(d.get("height"))
        except (TypeError, ValueError):
            raise MediaError("Invalid video height.")
        if not 100 <= height <= 4320:
            raise MediaError("Invalid video height.")

    format_id = d.get("format_id") or None
    if format_id is not None:
        format_id = str(format_id)
        if not _FORMAT_ID_RE.match(format_id):
            raise MediaError("Invalid format.")

    def _opt_time(v):
        return None if v is None or not str(v).strip() else parse_time(v)

    start = _opt_time(d.get("start"))
    end = _opt_time(d.get("end"))
    if start is not None and end is not None and end <= start:
        raise MediaError("End time must be after the start time.")

    fmt, preset = d.get("fmt", "mp3"), d.get("preset", "balanced")
    if fmt not in FORMATS or preset not in PRESETS:
        raise MediaError("Unknown format or quality preset.")

    if not HOME_MODE and relay.enabled() and downloader.is_youtube(url):
        payload = {k: d.get(k) for k in ("url", "kind", "height", "format_id", "start", "end", "fmt", "preset")
                   if d.get(k) is not None}
        r = _relay_json("POST", "/api/download", payload)
        if r:
            code, body = r
            if code == 200 and body.get("job_id"):
                return jsonify({"job_id": relay.PREFIX + body["job_id"]})
            return jsonify(body), code

    def work(job, _in, out_dir):
        return run_download(job.update, lambda: job.cancelled, out_dir, url, kind,
                            height=height, format_id=format_id, start=start, end=end,
                            audio_fmt=fmt, preset=preset, message=lambda m: job.update(message=m))

    return _start_job("download", work)


# ------------------------------------------------------------------ jobs / files
@bp.post("/api/relay/register")
def relay_register():
    """Heartbeat from the home agent: tells Render the current tunnel URL."""
    if not relay.check_key(request.headers.get("X-Relay-Key")):
        return _err("Forbidden.", 403)
    d = request.get_json(silent=True) or {}
    if not relay.register(d.get("url")):
        return _err("Invalid relay URL.", 400)
    return jsonify({"ok": True})


@bp.get("/api/jobs/<job_id>")
def job_status(job_id):
    if relay.is_remote_id(job_id):
        return _remote_job("GET", job_id)
    job = jobs().get(job_id)
    return jsonify(job.public()) if job else _err("Job not found (it may have expired).", 404)


@bp.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id):
    if relay.is_remote_id(job_id):
        return _remote_job("POST", job_id, "/cancel")
    job = jobs().cancel(job_id)
    return jsonify({"ok": bool(job)})


@bp.get("/files/<job_id>/<path:name>")
def get_file(job_id, name):
    """Serve a finished file by its exact name. (This route was missing from the uploaded file.)"""
    if relay.is_remote_id(job_id):
        base = relay.home_url()
        real = relay.strip_prefix(job_id)
        if not base or not _JOB_ID_RE.match(real):
            return _err("File not found on server.", 404)
        target = f"{base}/files/{real}/{urllib.parse.quote(name)}"
        if request.args.get("play") == "1":
            target += "?play=1"
        return redirect(target, 302)
    cleanup_old_files()
    if not _JOB_ID_RE.match(job_id):
        return _err("File not found on server.", 404)
    root = config.OUTPUT_DIR.resolve()
    job_dir = (root / job_id).resolve()
    if job_dir.parent != root or not job_dir.is_dir():
        return _err("File not found on server (it may have expired).", 404)
    if not (job_dir / name).is_file():
        # Log exactly what was asked vs what exists, so a name mismatch is visible in Render logs.
        current_app.logger.warning("404 file: asked=%r, on disk=%r", name,
                                   sorted(f.name for f in job_dir.iterdir()))
        return _err("File not found on server.", 404)
    # send_from_directory blocks path traversal and supports Range requests (needed for video playback).
    return send_from_directory(job_dir, name, as_attachment=request.args.get("play") != "1")
