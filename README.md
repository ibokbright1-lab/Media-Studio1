# Media Studio

Compress audio, trim audio/video, and download from links -- one engine, two ways to use it:
a **website** (runs locally now, ready to host later) and a **command line**.

## Setup
```bash
pip install -r requirements.txt     # Flask + yt-dlp
# FFmpeg must be installed and on PATH (https://ffmpeg.org)
python app.py                        # open http://127.0.0.1:5000
```

## What it does
**Compress audio** (upload from your device)
- Drops what costs space but isn't sound: video, cover art, hidden extra data.
- Bitrate never goes above the source's real bitrate (no wasted bytes).
- Never returns a file bigger than you uploaded.
- Formats: MP3 (plays everywhere), M4A, Opus (smallest). Presets: High / Balanced / Small / Voice (mono).
- "Aim for a maximum size" mode (e.g. 16 MB for WhatsApp/email).
- **Special feature -- Smart Silence Cut:** preview, then remove long silent gaps. Pauses longer than
  your limit (default 0.6 s) are shortened; speech itself is untouched.

**Trim** (upload any audio/video, or trim while downloading)
- Precise: exact cut (near-lossless re-encode). Fast: instant stream copy (may start on the nearest keyframe).
- Audio files are cut without re-encoding. Video can be exported as audio only (MP3/M4A).
- Use the preview player's "Use playhead" buttons to pick the cut points.

**Download** (YouTube, Facebook and anything else yt-dlp supports)
- Video up to your chosen resolution as MP4, or audio only.
- Optional time range: downloads just that part; if the site refuses, it downloads everything and cuts locally.

## Command line
```bash
python cli.py compress song.wav --fmt opus --preset balanced
python cli.py compress lecture.mp3 --silence --target-mb 10
python cli.py silence-report lecture.mp3
python cli.py trim video.mp4 1:30 2:45 --mode precise
python cli.py trim video.mp4 1:30 2:45 --extract mp3
python cli.py download "https://..." --kind video --height 720 --start 0:30 --end 1:10
```

## If downloads fail (the usual causes)
1. **Update yt-dlp** -- sites change weekly: `pip install -U yt-dlp`
2. **HTTP 403 / verification errors on YouTube:** install **Deno** (https://deno.com) so yt-dlp can solve YouTube's checks.
3. **"Confirm you're not a bot":** export `cookies.txt` from a logged-in private browser window (extension
   "Get cookies.txt LOCALLY"), save it in this folder, or set `MS_COOKIES_BROWSER=chrome`.
   Never share cookies.txt -- it is equivalent to being logged in.

## Settings (environment variables)
`MS_HOST` `MS_PORT` `MS_MAX_UPLOAD_MB` `MS_MAX_WORKERS` `MS_FILE_TTL_HOURS` `MS_DATA_DIR` `MS_COOKIES_FILE` `MS_COOKIES_BROWSER`

## Tests
```bash
python -m unittest discover -s tests -t .
```

## Road to a real website
Already done: logic separated from web layer (`core/` vs `web/`), background jobs with progress and cancel,
upload validation, safe file names, job-ID-only file access (no path traversal), private/local URLs refused,
automatic cleanup, config via environment.
Still to do when you host it publicly: user accounts + per-user limits, HTTPS and a real WSGI server
(gunicorn behind nginx), replace `core/jobs.py` with a queue (Celery/RQ + Redis) and local disk with object storage,
rate limiting, and a legal review of what users may download.

## Layout
```
app.py  cli.py  config.py
core/   compress.py  silence.py  trim.py  downloader.py  pipeline.py  jobs.py  ffmpeg_utils.py  timeparse.py
web/    routes.py  templates/index.html  static/app.css  static/app.js
tests/
```
