"""Timestamp parsing: '90', '1:30', '01:02:03', '1:30.5', '1m30s' -> seconds (float)."""
import re
from core.errors import MediaError


def parse_time(raw) -> float:
    if raw is None or str(raw).strip() == "":
        raise MediaError("Time can't be empty.")
    s = str(raw).strip().lower()
    if re.search(r"[hms]", s):
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", s)
        if not m or not any(m.groups()):
            raise MediaError(f"Couldn't understand '{raw}'. Use seconds, mm:ss or hh:mm:ss.")
        h, mi, sec = (float(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + sec
    parts = s.split(":")
    if len(parts) > 3:
        raise MediaError(f"Couldn't understand '{raw}'. Use seconds, mm:ss or hh:mm:ss.")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise MediaError(f"Couldn't understand '{raw}'. Use seconds, mm:ss or hh:mm:ss.")
    if any(n < 0 for n in nums):
        raise MediaError("Time can't be negative.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def fmt_time(seconds: float) -> str:
    seconds = max(0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"
