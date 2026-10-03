"""config.py -- all settings in one place. Override with environment variables
(this is what makes the move to a hosted website easy later)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MS_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
for _d in (UPLOAD_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("MS_HOST", "127.0.0.1")      # use 0.0.0.0 only when hosting
PORT = int(os.environ.get("MS_PORT", "5000"))
MAX_UPLOAD_MB = int(os.environ.get("MS_MAX_UPLOAD_MB", "2048"))
MAX_WORKERS = int(os.environ.get("MS_MAX_WORKERS", "2"))
FILE_TTL_HOURS = int(os.environ.get("MS_FILE_TTL_HOURS", "24"))  # auto-delete old files
COOKIES_FILE = os.environ.get("MS_COOKIES_FILE", str(BASE_DIR / "cookies.txt"))
COOKIES_FROM_BROWSER = os.environ.get("MS_COOKIES_BROWSER", "")  # e.g. "chrome", "firefox"

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".amr"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".3gp", ".flv", ".ts"}
