import tempfile
import unittest
from pathlib import Path

from core import silence
from core.compress import compress_audio, plan_bitrate
from core.errors import MediaError
from core.ffmpeg_utils import probe
from core.timeparse import parse_time
from core.trim import trim_media
from tests.helpers import make_media


class Core(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.m = make_media(cls.tmp / "in")
        cls.out = cls.tmp / "out"; cls.out.mkdir()

    def test_parse_time(self):
        self.assertEqual(parse_time("1:30"), 90); self.assertEqual(parse_time("1m30s"), 90)
        self.assertEqual(parse_time("1:02:03"), 3723); self.assertEqual(parse_time("2.5"), 2.5)
        for bad in ("", "abc", "1:2:3:4", "-5"):
            with self.assertRaises(MediaError): parse_time(bad)

    def test_bitrate_never_exceeds_source(self):
        info = {"duration": 100, "audio_bitrate_kbps": 96, "audio_codec": "mp3"}
        self.assertEqual(plan_bitrate(info, "mp3", "high")[0], 96)
        lossless = {"duration": 100, "audio_bitrate_kbps": 900, "audio_codec": "flac"}
        self.assertEqual(plan_bitrate(lossless, "mp3", "high")[0], 192)

    def test_compress_shrinks_and_keeps_duration(self):
        r = compress_audio(self.m / "speech.wav", self.out, fmt="opus")
        self.assertLess(r["new_size"], r["original_size"] * 0.2)
        self.assertAlmostEqual(r["new_duration"], r["original_duration"], delta=0.3)

    def test_compress_never_grows_file(self):
        small = self.out / "tiny.mp3"
        from tests.helpers import ff
        ff("-i", self.m / "hi.mp3", "-b:a", "32k", small)
        r = compress_audio(small, self.out, fmt="mp3", preset="high")
        self.assertLessEqual(r["new_size"], r["original_size"])

    def test_silence_cut_matches_preview(self):
        info = probe(self.m / "speech.wav")
        rep = silence.analyze(self.m / "speech.wav", total_duration=info["duration"])
        r = compress_audio(self.m / "speech.wav", self.out, fmt="mp3", remove_silence=True)
        removed = r["original_duration"] - r["new_duration"]
        self.assertAlmostEqual(removed, rep["removable_seconds"], delta=1.0)
        self.assertGreater(removed, 4)

    def test_target_size(self):
        r = compress_audio(self.m / "hi.mp3", self.out, fmt="mp3", target_mb=0.1)
        self.assertLess(r["new_size"], 0.11 * 1024 * 1024)

    def test_trim_audio_and_video_exact(self):
        a = trim_media(self.m / "hi.mp3", self.out, 2, 8)
        self.assertAlmostEqual(a["actual_seconds"], 6, delta=0.15)
        v = trim_media(self.m / "clip.mp4", self.out, 3.5, 7.25)
        self.assertAlmostEqual(v["actual_seconds"], 3.75, delta=0.15)
        x = trim_media(self.m / "clip.mp4", self.out, 3, 6, extract_audio="mp3")
        self.assertTrue(x["file"].endswith(".mp3"))

    def test_trim_validation(self):
        with self.assertRaises(MediaError): trim_media(self.m / "hi.mp3", self.out, 99, 100)
        with self.assertRaises(MediaError): trim_media(self.m / "hi.mp3", self.out, 5, 3)
        r = trim_media(self.m / "hi.mp3", self.out, 10, 999)
        self.assertTrue(r["notes"])

    def test_bad_file(self):
        junk = self.out / "junk.mp3"; junk.write_bytes(b"not audio at all")
        with self.assertRaises(MediaError): compress_audio(junk, self.out)


if __name__ == "__main__":
    unittest.main()
