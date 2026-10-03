"""Exercises the download pipeline with a FAKE yt-dlp (the real one needs internet)."""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from core import downloader
from core.errors import MediaError
from core.pipeline import run_download
from tests.helpers import ff, make_media


def install_fake(media_dir: Path, fail_ranges=False, error=None):
    created = []

    class FakeYDL:
        def __init__(self, opts): self.o = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=True):
            if error: raise Exception(error)
            if "download_ranges" in self.o and fail_ranges: raise Exception("section download not supported")
            out = Path(self.o["outtmpl"].replace("%(ext)s", "mp4"))
            if self.o.get("format") == "bestaudio/best":
                out = Path(self.o["outtmpl"].replace("%(ext)s", "m4a"))
                ff("-i", media_dir / "hi.mp3", "-c:a", "aac", out)
            elif "download_ranges" in self.o:
                ff("-ss", "2", "-i", media_dir / "clip.mp4", "-t", "4", "-c", "copy", out)
            else:
                ff("-i", media_dir / "clip.mp4", "-c", "copy", out)
            for h in self.o["progress_hooks"]:
                h({"status": "finished", "info_dict": {}})
            created.append(self.o)
            return {"title": 'My: "Video" / test?', "duration": 12}

    yt = types.ModuleType("yt_dlp"); yt.YoutubeDL = FakeYDL
    utils = types.ModuleType("yt_dlp.utils"); utils.download_range_func = lambda *a: ("ranges", a)
    ver = types.ModuleType("yt_dlp.version"); ver.__version__ = "2099.01.01"
    yt.utils, yt.version = utils, ver
    return mock.patch.dict(sys.modules, {"yt_dlp": yt, "yt_dlp.utils": utils, "yt_dlp.version": ver}), created


class Download(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = make_media(Path(tempfile.mkdtemp()))
        cls.p = mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])
        cls.p.start()

    @classmethod
    def tearDownClass(cls): cls.p.stop()

    def run_dl(self, **kw):
        out = Path(tempfile.mkdtemp())
        ctx, created = install_fake(self.m, kw.pop("fail_ranges", False), kw.pop("error", None))
        with ctx:
            return out, run_download(None, lambda: False, out, "https://example.com/v", **kw), created

    def test_video(self):
        out, r, _ = self.run_dl(kind="video", height=720, start=None, end=None)
        self.assertEqual(r["file"], "My Video  test.mp4")      # unsafe characters removed
        self.assertTrue((out / r["file"]).exists())

    def test_video_section(self):
        out, r, created = self.run_dl(kind="video", height=720, start=2, end=6)
        self.assertIn("download_ranges", created[0])
        self.assertTrue(created[0]["force_keyframes_at_cuts"])

    def test_section_falls_back_to_local_cut(self):
        out, r, _ = self.run_dl(kind="video", height=720, start=2.0, end=6.0, fail_ranges=True)
        from core.ffmpeg_utils import probe
        self.assertAlmostEqual(probe(out / r["file"])["duration"], 4, delta=0.2)
        self.assertTrue(any("whole file" in n for n in r["notes"]))

    def test_audio_is_compressed_to_mp3(self):
        out, r, _ = self.run_dl(kind="audio", height=None, start=None, end=None, audio_fmt="mp3", preset="balanced")
        self.assertTrue(r["file"].endswith(".mp3"))
        self.assertEqual(sorted(p.suffix for p in out.iterdir()), [".mp3"])   # no leftovers

    def test_friendly_errors(self):
        with self.assertRaises(MediaError) as c:
            self.run_dl(kind="video", height=720, start=None, end=None, error="HTTP Error 403: Forbidden")
        self.assertIn("pip install -U yt-dlp", str(c.exception))

    def test_blocked_urls(self):
        for u in ("http://localhost/x", "file:///etc/passwd", "http://10.0.0.1/a"):
            with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", (u.split("//")[1].split("/")[0].replace("localhost", "127.0.0.1"), 0))]):
                with self.assertRaises(MediaError): downloader.validate_url(u)


if __name__ == "__main__":
    unittest.main()
