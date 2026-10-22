import logging
import os
from pathlib import Path

from core import downloader
from core.compress import compress_audio
from core.errors import MediaError

log = logging.getLogger("pipeline")


def run_download(update, cancelled, out_dir, url, kind, height=None, format_id=None, start=None, end=None, audio_fmt="mp3", preset="balanced", message=None):
    update(10, "Starting download…")

    # Social media sites often serve "background music" as the standalone audio track.
    # To get voices + music, force the fully mixed video first, then rip the audio from it.
    if kind == "audio":
        format_id = "best"

    # 1. Download the requested format (or the forced 'best' mix for audio)
    res = downloader.download(
        url,
        out_dir,
        kind=kind,
        height=height,
        format_id=format_id,
        start=start,
        end=end,
        on_progress=lambda p: update(10 + (p * 0.4)),
        cancel=cancelled,
        on_message=message
    )

    # Fail cleanly instead of crashing the worker (and freezing the progress bar) if the
    # downloader returns nothing. downloader.download() should always return a dict or raise.
    if not isinstance(res, dict):
        log.error("downloader.download returned %r (from %s) -- expected a dict. "
                  "Check that the deployed core/downloader.py is the current version.",
                  res, getattr(downloader, "__file__", "?"))
        raise MediaError("Download failed on the server. Please try again.")

    downloaded_path = res.get("path")
    if not downloaded_path or not Path(downloaded_path).exists():
        raise MediaError("Download failed: File not found after extraction.")

    # 2. Process Audio: Rip the full MP3 from the complete video mix
    if kind == "audio":
        update(50, f"Extracting full audio to {audio_fmt.upper()}…")

        comp_res = compress_audio(
            downloaded_path,
            out_dir,
            fmt=audio_fmt,
            preset=preset,
            on_progress=lambda p: update(50 + (p * 0.5)),
            cancel=cancelled
        )

        # Clean up the original raw video file to save disk space
        try:
            os.remove(downloaded_path)
        except OSError:
            pass

        res["path"] = comp_res["path"]
        res["file"] = comp_res["file"]
        update(100, "Audio conversion complete!")

    # 3. Video: nothing to convert, just finalize the file details
    else:
        res["file"] = Path(res["path"]).name
        update(100, "Ready!")

    return res
