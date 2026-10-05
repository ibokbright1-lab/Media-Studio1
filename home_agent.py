#!/usr/bin/env python3
"""Run this on your HOME computer (residential IP). One command does everything:

    python home_agent.py

It (1) starts the app in HOME_MODE, (2) opens a free Cloudflare quick tunnel to it, and
(3) tells your Render app the tunnel's address every 45 seconds. If this window is closed or
the computer is off, Render simply falls back to trying YouTube itself.

Settings come from environment variables or a file named home.env next to this script:
    RENDER_URL=https://your-app.onrender.com
    RELAY_KEY=<same long secret you set on Render>
    APP_TARGET=app:app                 (optional; "module:variable" of your Flask app)
    COOKIES_FROM_BROWSER=firefox       (optional; or put cookies.txt in the project folder)
    CORS_ORIGIN=https://your-app.onrender.com   (optional but recommended)

Needs on this computer: python deps (pip install -r requirements.txt waitress),
yt-dlp[default], Deno, ffmpeg, and cloudflared.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 5055
HEARTBEAT_SECONDS = 45
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def load_env_file():
    f = HERE / "home.env"
    if not f.is_file():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def need(name):
    v = os.getenv(name, "").strip()
    if not v:
        sys.exit(f"Missing {name}. Put it in home.env or set it as an environment variable.")
    return v


def local_ok(key):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/health", headers={"X-Relay-Key": key})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def register(render_url, key, tunnel_url):
    req = urllib.request.Request(
        render_url.rstrip("/") + "/api/relay/register", method="POST",
        data=json.dumps({"url": tunnel_url}).encode(),
        headers={"Content-Type": "application/json", "X-Relay-Key": key,
                 "User-Agent": "media-home-agent/1.0"})
    with urllib.request.urlopen(req, timeout=90) as r:     # long timeout: free Render may be waking up
        return r.status


def read_tunnel_output(proc, state):
    for line in proc.stdout:
        m = TUNNEL_RE.search(line)
        if m and not state.get("url"):
            state["url"] = m.group(0)
            print(f"[agent] tunnel is up: {state['url']}", flush=True)


def heartbeat(render_url, key, state, stop):
    last_ok = None
    while not stop.is_set():
        url = state.get("url")
        if url and local_ok(key):
            try:
                register(render_url, key, url)
                if last_ok is not True:
                    print("[agent] registered with Render OK", flush=True)
                last_ok = True
            except Exception as e:
                if last_ok is not False:
                    print(f"[agent] could not reach Render yet: {e}", flush=True)
                last_ok = False
        stop.wait(HEARTBEAT_SECONDS)


def main():
    load_env_file()
    render_url = need("RENDER_URL")
    key = need("RELAY_KEY")
    target = os.getenv("APP_TARGET", "app:app")

    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        sys.exit("cloudflared not found. Install it (Windows: winget install Cloudflare.cloudflared).")

    env = {**os.environ, "HOME_MODE": "1", "RELAY_KEY": key}
    server_cmd = [sys.executable, "-m", "waitress", f"--listen=127.0.0.1:{PORT}", "--threads=8"]
    if ":" in target and target.endswith("()"):            # factory, e.g. "app:create_app()"
        server_cmd += ["--call", target[:-2]]
    else:
        server_cmd.append(target)

    state, stop = {}, threading.Event()
    threading.Thread(target=heartbeat, args=(render_url, key, state, stop), daemon=True).start()

    try:
        while True:
            state.clear()
            print("[agent] starting app + tunnel…", flush=True)
            server = subprocess.Popen(server_cmd, cwd=HERE, env=env)
            tunnel = subprocess.Popen(
                [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            threading.Thread(target=read_tunnel_output, args=(tunnel, state), daemon=True).start()
            while server.poll() is None and tunnel.poll() is None:
                time.sleep(2)
            print("[agent] a process stopped; restarting in 5s…", flush=True)
            for p in (server, tunnel):
                if p.poll() is None:
                    p.terminate()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[agent] stopping.")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
