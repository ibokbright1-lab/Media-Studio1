"""Relay: send YouTube work from Render to your home computer (residential IP).

How it works
  * The home computer runs the same app (HOME_MODE=1) behind a Cloudflare quick tunnel and
    "registers" its tunnel URL with Render every minute (see home_agent.py).
  * Render forwards YouTube /api/info and /api/download calls to that URL, tells the browser the
    job id is "home-<id>", and redirects the final file download straight to the home machine.
  * If the home machine is off/unreachable, Render falls back to trying YouTube locally.

Environment
  Render + home (same value):  RELAY_KEY        long random shared secret
  Render (optional):           HOME_URL         fixed tunnel URL (named tunnel); otherwise the
                                                 registered quick-tunnel URL is used
  Render (optional):           RELAY_ALLOWED_SUFFIXES   default ".trycloudflare.com"
"""
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

PREFIX = "home-"
STALE_AFTER = 180          # seconds without a heartbeat before the home server is considered offline

_state = {"url": "", "seen": 0.0}


class RelayDown(Exception):
    """Home server unreachable (caller should fall back to local)."""


# ---------------------------------------------------------------- auth / registry
def check_key(given) -> bool:
    key = os.getenv("RELAY_KEY", "")
    return bool(key) and hmac.compare_digest((given or "").encode(), key.encode())


def register(url) -> bool:
    """Called by the home agent's heartbeat. Only https tunnel hosts on the allow-list are accepted."""
    p = urlparse(url or "")
    suffixes = tuple(s.strip() for s in
                     (os.getenv("RELAY_ALLOWED_SUFFIXES") or ".trycloudflare.com").split(",") if s.strip())
    if p.scheme != "https" or not p.hostname or p.path not in ("", "/"):
        return False
    if not p.hostname.endswith(suffixes):
        return False
    _state.update(url=f"https://{p.hostname}", seen=time.time())
    return True


def home_url() -> str:
    static = (os.getenv("HOME_URL") or "").rstrip("/")
    if static:
        return static
    if _state["url"] and time.time() - _state["seen"] < STALE_AFTER:
        return _state["url"]
    return ""


def enabled() -> bool:
    return bool(os.getenv("RELAY_KEY")) and bool(home_url())


def is_remote_id(job_id: str) -> bool:
    return (job_id or "").startswith(PREFIX)


def strip_prefix(job_id: str) -> str:
    return job_id[len(PREFIX):]


# ---------------------------------------------------------------- calling the home server
def call(method: str, path: str, payload=None, timeout: float = 20):
    """Return (status_code, json_body). Raises RelayDown if the home server can't be reached."""
    base = home_url()
    if not base:
        raise RelayDown("no home server registered")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-Relay-Key": os.getenv("RELAY_KEY", ""),
                 "User-Agent": "media-relay/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # Cloudflare answers 502/503/504/530 when the tunnel is up but nothing is behind it.
        if e.code in (502, 503, 504, 530):
            raise RelayDown(f"tunnel returned {e.code}")
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {"error": f"Download server error ({e.code})."}
        return e.code, body
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError) as e:
        raise RelayDown(str(e))


def rewrite_urls(obj):
    """Prefix every '/files/<id>/...' URL in a job payload so Render knows the file lives at home."""
    if isinstance(obj, dict):
        return {k: rewrite_urls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rewrite_urls(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("/files/"):
        return "/files/" + PREFIX + obj[len("/files/"):]
    return obj
