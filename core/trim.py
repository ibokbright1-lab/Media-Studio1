"""Trim audio or video files (uploaded or downloaded).

precise (default for video): re-encodes (x264 CRF 23, bitrate capped to the source's
    bitrate) so the cut lands exactly where you asked without the file growing.
fast: stream copy -- instant and zero quality loss, but video starts at the nearest
    earlier keyframe (can be up to a few seconds early).
Audio is cut by stream copy when possible. When it must be re-encoded (MP3/M4A
extraction or fallback), the bitrate is capped at the source's so the file never grows.
"""
from pathlib import Path

from core.errors import MediaError
from core.ffmpeg_utils import probe, run_ffmpeg, human_size
from core.timeparse import fmt_time


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


def _source_kbps(info: dict) -> int:
    """Source's average total bitrate in kbit/s (0 if unknown)."""
    try:
        return int(info["size"] * 8 / info["duration"] / 1000)
    except Exception:
        return 0


def _bitrate_cap_kbps(info: dict) -> int:
    """Ceiling for video re-encoding."""
    return max(_source_kbps(info), 500)


def _audio_kbps(info: dict) -> int:
    """Audio re-encode bitrate: never above the source's, never above 192k."""
    src = _source_kbps(info)
    if src <= 0:
        return 128
    return max(64, min(src, 192))


def _audio_codec_args(fmt: str, kbps: int) -> list:
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{kbps}k"]
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]  # m4a


EXTRACT_FORMATS = ("mp3", "m4a")


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
        kbps = _audio_kbps(info)
        if extract_audio in EXTRACT_FORMATS:
            out = out_dir / f"{src.stem}-{tag}.{extract_audio}"
            args = base + ["-map", "0:a:0", "-vn"] + _audio_codec_args(extract_audio, kbps) + [out]
        else:
            out = out_dir / f"{src.stem}-{tag}{src.suffix.lower()}"
            args = base + ["-map", "0:a:0", "-vn", "-c:a", "copy", "-map_metadata", "0", out]
        try:
            run_ffmpeg(args, length, on_progress, cancel)
        except MediaError as e:
            if "Cancelled" in str(e) or extract_audio in EXTRACT_FORMATS:
                raise
            out = out_dir / f"{src.stem}-{tag}.mp3"          # copy not possible: re-encode safely
            run_ffmpeg(base + ["-map", "0:a:0", "-vn"] + _audio_codec_args("mp3", kbps) + [out],
                       length, on_progress, cancel)
            notes.append("Original format couldn't be cut without re-encoding, so MP3 was used.")
    elif mode == "fast":
        out = out_dir / f"{src.stem}-{tag}{src.suffix.lower()}"
        run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero", out],
                   length, on_progress, cancel)
    else:
        out = out_dir / f"{src.stem}-{tag}.mp4"
        cap = _bitrate_cap_kbps(info)
        rate_args = ["-maxrate", f"{cap}k", "-bufsize", f"{cap * 2}k"] if cap else []
        run_ffmpeg(base + ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                           "-crf", "23"] + rate_args +
                          ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                           "-movflags", "+faststart", out], length, on_progress, cancel)

    out_info = probe(out)
    if mode == "fast" and info["has_video"] and not extract_audio and out_info["duration"] - length > 0.5:
        notes.append(f"Fast mode cuts on keyframes: the clip is {out_info['duration']:.1f}s instead of "
                     f"{length:.1f}s because it starts at the nearest earlier keyframe. Use Precise for an exact cut.")
    return {"file": out.name, "path": str(out), "requested_seconds": round(length, 2),
            "actual_seconds": round(out_info["duration"], 2), "new_human": human_size(out_info["size"]),
            "notes": notes}
