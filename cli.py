#!/usr/bin/env python3
"""Command-line version of every feature (same engine as the website).

  python cli.py compress song.wav --fmt opus --preset balanced
  python cli.py compress lecture.mp3 --silence --target-mb 10
  python cli.py trim video.mp4 1:30 2:45 --mode precise
  python cli.py trim video.mp4 1:30 2:45 --extract mp3
  python cli.py download "https://..." --kind video --height 720 --start 0:30 --end 1:10
  python cli.py silence-report lecture.mp3
"""
import argparse
import sys
from pathlib import Path

import config
from core import silence
from core.compress import FORMATS, PRESETS, compress_audio
from core.errors import MediaError
from core.ffmpeg_utils import probe
from core.pipeline import run_download
from core.timeparse import parse_time
from core.trim import EXTRACT_FORMATS, trim_media


def bar(p):
    sys.stdout.write(f"\r  {p:5.1f}%")
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Media Studio")
    ap.add_argument("--out", default=str(config.OUTPUT_DIR / "cli"), help="output folder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compress"); c.add_argument("file")
    c.add_argument("--fmt", choices=FORMATS, default="mp3"); c.add_argument("--preset", choices=PRESETS, default="balanced")
    c.add_argument("--keep-tags", action="store_true"); c.add_argument("--silence", action="store_true")
    c.add_argument("--silence-db", type=float, default=silence.DEFAULT_THRESHOLD_DB)
    c.add_argument("--pause", type=float, default=silence.DEFAULT_MAX_PAUSE)
    c.add_argument("--target-mb", type=float)

    t = sub.add_parser("trim"); t.add_argument("file"); t.add_argument("start"); t.add_argument("end", nargs="?")
    t.add_argument("--mode", choices=["precise", "fast"], default="precise"); t.add_argument("--extract", choices=EXTRACT_FORMATS)

    d = sub.add_parser("download"); d.add_argument("url"); d.add_argument("--kind", choices=["video", "audio"], default="video")
    d.add_argument("--height", type=int, default=720); d.add_argument("--start"); d.add_argument("--end")
    d.add_argument("--fmt", choices=FORMATS, default="mp3"); d.add_argument("--preset", choices=PRESETS, default="balanced")

    s = sub.add_parser("silence-report"); s.add_argument("file")
    s.add_argument("--silence-db", type=float, default=silence.DEFAULT_THRESHOLD_DB)
    s.add_argument("--pause", type=float, default=silence.DEFAULT_MAX_PAUSE)

    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    try:
        if a.cmd == "compress":
            r = compress_audio(a.file, out, fmt=a.fmt, preset=a.preset, strip_extras=not a.keep_tags,
                               remove_silence=a.silence, silence_db=a.silence_db, silence_pause=a.pause,
                               target_mb=a.target_mb, on_progress=bar)
            print(f"\n  {r['original_human']} -> {r['new_human']}  (saved {r['saved_percent']}%)  {r['path']}")
            [print("  note:", n) for n in r["notes"]]
        elif a.cmd == "trim":
            r = trim_media(a.file, out, parse_time(a.start), parse_time(a.end) if a.end else None,
                           mode=a.mode, extract_audio=a.extract, on_progress=bar)
            print(f"\n  {r['actual_seconds']}s  {r['new_human']}  {r['path']}")
            [print("  note:", n) for n in r["notes"]]
        elif a.cmd == "download":
            r = run_download(bar, lambda: False, out, a.url, a.kind, a.height,
                             parse_time(a.start) if a.start else None, parse_time(a.end) if a.end else None,
                             audio_fmt=a.fmt, preset=a.preset, message=lambda m: print("\n  " + m))
            print(f"\n  saved: {out / r['file']}")
            [print("  note:", n) for n in r["notes"]]
        elif a.cmd == "silence-report":
            info = probe(a.file)
            print(silence.analyze(a.file, a.silence_db, a.pause, info["duration"]))
    except MediaError as e:
        print(f"\nError: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
