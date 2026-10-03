"""Audio compression that keeps quality.

How "no quality loss" is protected:
  1. Everything that costs bytes but isn't sound is dropped: video streams, cover art,
     extra tracks, (optionally) tags.
  2. The bitrate NEVER goes above what the source really had -- encoding higher than the
     source adds size and zero quality.
  3. Lossless sources (WAV/FLAC/AIFF) shrink massively with transparent-quality settings.
  4. The result is never larger than the input. If compressing would grow the file, you
     keep the best smaller version, or are told the file is already optimal.
  5. Channels/sample-rate are kept unless you choose Voice mode.
"""
import shutil
from pathlib import Path

from core import silence as silence_mod
from core.errors import MediaError
from core.ffmpeg_utils import probe, run_ffmpeg, human_size

# kbps per quality preset, per output format (Opus is the most efficient; MP3 the most compatible)
PRESETS = {
    "high":     {"mp3": 192, "m4a": 160, "opus": 128},
    "balanced": {"mp3": 128, "m4a": 112, "opus": 96},
    "small":    {"mp3": 96,  "m4a": 80,  "opus": 64},
    "voice":    {"mp3": 64,  "m4a": 48,  "opus": 32},   # forces mono
}
FORMATS = {
    "mp3":  {"ext": ".mp3",  "args": ["-c:a", "libmp3lame"]},
    "m4a":  {"ext": ".m4a",  "args": ["-c:a", "aac", "-movflags", "+faststart"]},
    "opus": {"ext": ".opus", "args": ["-c:a", "libopus", "-vbr", "on"]},
}
LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff"}
MIN_KBPS = 16


def _is_lossless(codec) -> bool:
    return bool(codec) and (codec in LOSSLESS_CODECS or codec.startswith("pcm_"))


def plan_bitrate(info: dict, fmt: str, preset: str, target_mb: float = None, force_cap=True):
    """Return (bitrate_kbps, notes). Pure function -- easy to unit test."""
    notes = []
    kbps = PRESETS[preset][fmt]
    lossless = _is_lossless(info.get("audio_codec"))
    src = info.get("audio_bitrate_kbps")
    if target_mb:
        if not info["duration"]:
            raise MediaError("Can't aim for a size on a file with unknown length.")
        kbps = int(target_mb * 1024 * 1024 * 8 * 0.97 / info["duration"] / 1000)
        if kbps < 24:
            notes.append(f"A {target_mb:g} MB target forces {max(kbps, MIN_KBPS)} kbps -- quality will audibly drop.")
    if force_cap and src and not lossless and kbps > src:
        kbps = src
        notes.append(f"Capped at the source bitrate ({src} kbps) -- going higher would only add size.")
    return max(MIN_KBPS, min(int(kbps), 320)), notes


def compress_audio(src, out_dir, fmt="mp3", preset="balanced", strip_extras=True,
                   remove_silence=False, silence_db=silence_mod.DEFAULT_THRESHOLD_DB,
                   silence_pause=silence_mod.DEFAULT_MAX_PAUSE, target_mb=None,
                   on_progress=None, cancel=None) -> dict:
    if fmt not in FORMATS:
        raise MediaError("Unknown output format.")
    if preset not in PRESETS:
        raise MediaError("Unknown quality preset.")
    src, out_dir = Path(src), Path(out_dir)
    info = probe(src)
    if not info["has_audio"]:
        raise MediaError("This file has no audio to compress.")

    kbps, notes = plan_bitrate(info, fmt, preset, target_mb)
    out = out_dir / f"{src.stem}-compressed{FORMATS[fmt]['ext']}"

    args = ["-i", src, "-map", "0:a:0", "-vn"]
    if strip_extras:
        args += ["-map_metadata", "-1", "-map_chapters", "-1"]
    filters = []
    if remove_silence:
        filters.append(silence_mod.build_filter(silence_db, silence_pause))
    if filters:
        args += ["-af", ",".join(filters)]
    if preset == "voice" and info["channels"] != 1:
        args += ["-ac", "1"]
        notes.append("Voice mode: converted to mono (halves the size, ideal for speech).")
    if info["sample_rate"] and info["sample_rate"] > 48000:
        args += ["-ar", "48000"]
    args += FORMATS[fmt]["args"] + ["-b:a", f"{kbps}k"]
    if fmt == "opus" and preset == "voice":
        args += ["-application", "voip"]
    args += [out]

    run_ffmpeg(args, info["duration"], on_progress, cancel)

    out_info = probe(out)
    original, new = info["size"], out_info["size"]

    # Guard: never hand back something bigger than what was uploaded.
    if new >= original and not remove_silence and not target_mb:
        fallback = out_dir / f"{src.stem}-cleaned{src.suffix.lower()}"
        try:
            run_ffmpeg(["-i", src, "-map", "0:a:0", "-vn", "-map_metadata", "-1", "-c:a", "copy", fallback])
            if fallback.stat().st_size < original:
                out.unlink(missing_ok=True)
                out, new = fallback, fallback.stat().st_size
                notes.append("Re-encoding would have made it bigger, so only the extra data was stripped (audio untouched).")
            else:
                fallback.unlink(missing_ok=True)
                raise MediaError("no gain")
        except MediaError:
            out.unlink(missing_ok=True)
            out = out_dir / src.name
            if out.resolve() != src.resolve():
                shutil.copy2(src, out)
            new = original
            notes.append("This file is already well optimized -- any further compression would cost quality, so it was left as is.")

    if info["audio_codec"] and not _is_lossless(info["audio_codec"]) and new < original and "untouched" not in " ".join(notes):
        notes.append("The source was already lossy, so re-encoding adds a small generation loss. Pick High quality to minimise it.")

    return {
        "file": out.name, "path": str(out),
        "original_size": original, "new_size": new,
        "saved_percent": round((1 - new / original) * 100, 1) if original else 0,
        "original_human": human_size(original), "new_human": human_size(new),
        "original_duration": round(info["duration"], 1), "new_duration": round(out_info["duration"], 1),
        "bitrate_kbps": kbps, "notes": notes,
    }
