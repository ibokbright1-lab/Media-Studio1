import subprocess
from pathlib import Path


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *map(str, args)], check=True)


def make_media(d: Path):
    """speech.wav (3s tone, 4s silence, 3s tone, 4s silence, 3s tone), hi.mp3 (320k), clip.mp4 (12s video+audio)."""
    d.mkdir(parents=True, exist_ok=True)
    fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
    ff("-f", "lavfi", "-i", "sine=f=440:d=3", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=4",
       "-f", "lavfi", "-i", "sine=f=660:d=3", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=4",
       "-f", "lavfi", "-i", "sine=f=880:d=3",
       "-filter_complex", f"[0:a]{fmt}[a];[2:a]{fmt}[b];[4:a]{fmt}[c];[a][1:a][b][3:a][c]concat=n=5:v=0:a=1",
       d / "speech.wav")
    ff("-i", d / "speech.wav", "-b:a", "320k", d / "hi.mp3")
    ff("-f", "lavfi", "-i", "testsrc=d=12:s=320x180:r=25", "-f", "lavfi", "-i", "sine=f=300:d=12",
       "-c:v", "libx264", "-g", "100", "-pix_fmt", "yuv420p", "-c:a", "aac", d / "clip.mp4")
    return d
