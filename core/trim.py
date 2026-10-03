"""Trim audio or video files (uploaded or downloaded).

precise (default for video): re-encodes at near-lossless quality (x264 CRF 18) so the
    cut lands exactly where you asked.
fast: stream copy -- instant and zero quality loss, but video starts at the nearest
    earlier keyframe (can be up to a few seconds early).
Audio is always cut by stream copy, which is exact to a single audio frame (~25 ms).
"""
from pathlib import Path

from core.errors import MediaError
from core.ffmpeg_utils import probe, run_ffmpeg, human_size
from core.timeparse import fmt_time

EXTRACT_FORMATS = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "m4a": ["-c:a", "aac", "-b:a", "192k"]}


def validate_range(start: float, end, duration: float):
    notes = []
    if duration and start >= duration:
        raise MediaError(f"Start ({fmt_time(start)}) is past the end of the file ({fmt_time(duration)}).")
    if end is None or (duration and end > duration):
        if end is not None:
            notes.append(f"End was beyond the file, so it was set to {fmt_time(duration)}.")
        end = duration
    if end <= start:
        raise MediaError("End time must be after the start time.")
    return start, end, notes


def trim_media(src, out_dir, start: float, end=None, mode="precise", extract_audio=None,
               on_progress=None, cancel=None) -> dict:
    src, out_dir = Path(src), Path(out_dir)
    info = probe(src)
    start, end, notes = validate_range(start, end, info["duration"])
    length = end - start
    tag = f"{fmt_time(start).replace(':', '-')}_to_{fmt_time(end).replace(':', '-')}"

    # -ss BEFORE -i seeks fast; -t (a duration) avoids -to ambiguity
    base = ["-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}"]

    if extract_audio or not info["has_video"]:
        if extract_audio in EXTRACT_FORMATS:
            out = out_dir / f"{src.stem}-{tag}.{extract_audio}"
            args = base + ["-map", "0:a:0", "-vn"] + EXTRACT_FORMATS[extract_audio] + [out]
        else:
            out = out_dir / f"{src.stem}-{tag}{src.suffix.lower()}"
            args = base + ["-map", "0:a:0", "-vn", "-c:a", "copy", "-map_metadata", "0", out]
        try:
            run_ffmpeg(args, length, on_progress, cancel)
        except MediaError as e:
            if "Cancelled" in str(e) or extract_audio in EXTRACT_FORMATS:
                raise
            out = out_dir / f"{src.stem}-{tag}.mp3"          # copy not possible: re-encode safely
            run_ffmpeg(base + ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "2", out], length, on_progress, cancel)
            notes.append("Original format couldn't be cut without re-encoding, so MP3 (high quality) was used.")
    elif mode == "fast":
        out = out_dir / f"{src.stem}-{tag}{src.suffix.lower()}"
        run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero", out],
                   length, on_progress, cancel)
    else:
        out = out_dir / f"{src.stem}-{tag}.mp4"
        run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                           "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                           "-movflags", "+faststart", out], length, on_progress, cancel)

    out_info = probe(out)
    if mode == "fast" and info["has_video"] and not extract_audio and out_info["duration"] - length > 0.5:
        notes.append(f"Fast mode cuts on keyframes: the clip is {out_info['duration']:.1f}s instead of "
                     f"{length:.1f}s because it starts at the nearest earlier keyframe. Use Precise for an exact cut.")
    return {"file": out.name, "path": str(out), "requested_seconds": round(length, 2),
            "actual_seconds": round(out_info["duration"], 2), "new_human": human_size(out_info["size"]),
            "notes": notes}
