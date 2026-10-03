"""HTTP layer: validates input, starts jobs, serves results. All real work lives in core/."""
import re
import shutil
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import config
from core import downloader, silence
from core.compress import FORMATS, PRESETS, compress_audio
from core.errors import MediaError
from core.ffmpeg_utils import human_size, probe
from core.jobs import cleanup_old_files
from core.pipeline import run_download
from core.timeparse import parse_time
from core.trim import EXTRACT_FORMATS, trim_media

bp = Blueprint("web", __name__)
ID_RE = re.compile(r"^[0-9a-f]{32}$")
ALLOWED = config.AUDIO_EXTS | config.VIDEO_EXTS


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
    return default if v in (None, "") else float(v)


def _flag(name):
    return request.form.get(name) in ("1", "true", "on", "yes")


def _start_job(kind, work):
    """work(job, in_path|None, out_dir) -> result dict (must contain 'file')."""
    job_id = jobs().new_id()
    out_dir = config.OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    in_path = _save_upload(job_id) if kind in ("compress", "trim") else None

    def fn(job):
        res = work(job, in_path, out_dir)
        res["url"] = f"/files/{job_id}/{res['file']}"
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
    import shutil as sh
    try:
        import yt_dlp
        ytv = yt_dlp.version.__version__
    except Exception:
        ytv = None
    return jsonify({"ffmpeg": bool(sh.which("ffmpeg")), "yt_dlp": ytv, "warning": downloader.ytdlp_version_warning()})


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
    """Preview for Smart Silence Cut -- how much would be removed. Writes nothing permanent."""
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
    # Check for JSON first, fall back to FormData if JSON isn't used
    d = request.get_json(silent=True) or request.form
    return jsonify(downloader.fetch_info(d.get("url", "")))


@bp.post("/api/download")
def api_download():
    # Support both data formats
    d = request.get_json(silent=True) or request.form
    url = downloader.validate_url(d.get("url", ""))
    kind = d.get("kind", "video")
    if kind not in ("video", "audio"):
        raise MediaError("Invalid download type.")
    
    # Extract height AND the specific format_id chosen by the user
    height = int(d.get("height")) if d.get("height") else None
    format_id = d.get("format_id")
    
    # Safely get start and end times without throwing a KeyError
    start = parse_time(d.get("start", "")) if str(d.get("start", "")).strip() else None
    end = parse_time(d.get("end", "")) if str(d.get("end", "")).strip() else None
    if start is not None and end is not None and end <= start:
        raise MediaError("End time must be after the start time.")
        
    fmt, preset = d.get("fmt", "mp3"), d.get("preset", "balanced")
    if fmt not in FORMATS or preset not in PRESETS:
        raise MediaError("Unknown format or quality preset.")

    def work(job, _in, out_dir):
        # We now pass format_id down to run_download
        return run_download(job.update, lambda: job.cancelled, out_dir, url, kind, 
                            height=height, format_id=format_id, start=start, end=end,
                            audio_fmt=fmt, preset=preset, message=lambda m: job.update(message=m))

    return _start_job("download", work)


# ------------------------------------------------------------------ jobs / files
@bp.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = jobs().get(job_id) if ID_RE.match(job_id) else None
    return jsonify(job.public()) if job else _err("Job not found (it may have expired).", 404)


@bp.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id):
    job = jobs().cancel(job_id) if ID_RE.match(job_id) else None
    return jsonify({"ok": bool(job)})


@bp.get("/files/<job_id>/<path:name>")
def get_file(job_id, name):
    if not ID_RE.match(job_id):
        return _err("Not found.", 404)
    cleanup_old_files()
    return send_from_directory(config.OUTPUT_DIR / job_id, name, as_attachment=request.args.get("play") != "1")
