import os
from pathlib import Path

from core import downloader
from core.compress import compress_audio
from core.errors import MediaError

def run_download(update, cancelled, out_dir, url, kind, height=None, format_id=None, start=None, end=None, audio_fmt="mp3", preset="balanced", message=None):
    update(10, "Starting download…")
    
    # 1. Download the exact requested format
    res = downloader.download(
        url, 
        out_dir, 
        kind=kind, 
        height=height, 
        format_id=format_id, 
        start=start, 
        end=end,
        on_progress=lambda p: update(10 + (p * 0.4)), # Progress mapping: 10% to 50%
        cancel=cancelled,
        on_message=message
    )
    
    downloaded_path = res.get("path")
    if not downloaded_path or not Path(downloaded_path).exists():
        raise MediaError("Download failed: File not found after extraction.")
        
    # 2. Process Audio: Convert strictly to MP3
    if kind == "audio":
        update(50, f"Converting audio to {audio_fmt.upper()}…")
        
        # Pass the raw downloaded file to FFmpeg for MP3 conversion
        comp_res = compress_audio(
            in_path=downloaded_path,
            out_dir=out_dir,
            fmt=audio_fmt, # Defaults to "mp3" from routes.py
            preset=preset,
            on_progress=lambda p: update(50 + (p * 0.5)), # Progress mapping: 50% to 100%
            cancel=cancelled
        )
        
        # Clean up the original raw audio file to save disk space on Render
        try:
            os.remove(downloaded_path)
        except OSError:
            pass
            
        res["path"] = comp_res["path"]
        res["file"] = comp_res["file"]
        update(100, "Audio conversion complete!")
        
    # 3. Process Video: Skip conversion, just finalize the file details
    else:
        res["file"] = Path(res["path"]).name
        update(100, "Ready!")
        
    return res