"""SPECIAL FEATURE: Smart Silence Cut.

Previews and removes long silent gaps -- dead air in lectures, voice notes, recordings,
podcasts. Silence costs space but carries no information, so cutting it shrinks the file
without touching the quality of anything you actually hear. Short natural pauses are
left alone; long ones are shortened to about `max_pause` seconds.
"""
import re
import subprocess

from core.errors import MediaError
from core.ffmpeg_utils import require_ffmpeg

"""SPECIAL FEATURE: Smart Silence Cut.

Detects (preview) and removes long silent gaps -- dead air in lectures, voice notes,
recordings, podcasts. Silence costs space but carries no information, so cutting it
shrinks the file without touching the quality of anything you actually hear.
Short natural pauses are kept (default 0.35 s) so speech doesn't sound chopped.
"""
import re
import subprocess

from core.errors import MediaError
from core.ffmpeg_utils import require_ffmpeg

DEFAULT_THRESHOLD_DB = -45.0   # anything quieter than this counts as silence
DEFAULT_MAX_PAUSE = 0.6        # pauses longer than this are shortened to about this length
_KEEP = 0.1                    # small cushion left at each cut so speech doesn't sound clipped


def _stop_duration(max_pause: float) -> float:
    return max(0.1, float(max_pause) - _KEEP)


def build_filter(threshold_db=DEFAULT_THRESHOLD_DB, max_pause=DEFAULT_MAX_PAUSE) -> str:
    t = f"{float(threshold_db)}dB"
    return (f"silenceremove=start_periods=1:start_duration=0.05:start_threshold={t}:start_silence={_KEEP}"
            f":stop_periods=-1:stop_duration={_stop_duration(max_pause)}:stop_threshold={t}:stop_silence={_KEEP}")


def analyze(path, threshold_db=DEFAULT_THRESHOLD_DB, max_pause=DEFAULT_MAX_PAUSE, total_duration=None) -> dict:
    """Dry run: estimate how much time would be removed. Nothing is written."""
    require_ffmpeg()
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-vn", "-i", str(path),
         "-af", f"silencedetect=noise={float(threshold_db)}dB:d={_stop_duration(max_pause)}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise MediaError("Couldn't analyze this file for silence.")
    log = r.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", log)]
    durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", log)]
    if len(starts) > len(durs) and total_duration:       # trailing silence has no end line
        durs.append(max(0.0, total_duration - starts[-1]))
    removable = sum(max(0.0, d - max_pause) for d in durs)
    return {
        "gaps": len(durs),
        "silence_seconds": round(sum(durs), 1),
        "removable_seconds": round(removable, 1),
        "percent_of_file": round(removable / total_duration * 100, 1) if total_duration else None,
    }
