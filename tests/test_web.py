import io
import tempfile
import time
import unittest
from pathlib import Path

import config
from app import create_app
from tests.helpers import make_media


class Web(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = make_media(Path(tempfile.mkdtemp()))
        cls.c = create_app().test_client()

    def wait(self, job_id):
        for _ in range(100):
            j = self.c.get(f"/api/jobs/{job_id}").get_json()
            if j["status"] in ("done", "error", "cancelled"):
                return j
            time.sleep(0.2)
        self.fail("job timed out")

    def post_file(self, url, name, **data):
        return self.c.post(url, data={"file": (open(self.m / name, "rb"), name), **data}, content_type="multipart/form-data")

    def test_pages(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertIn("ffmpeg", self.c.get("/api/health").get_json())

    def test_compress_roundtrip(self):
        r = self.post_file("/api/compress", "speech.wav", fmt="mp3", preset="balanced", strip="1")
        j = self.wait(r.get_json()["job_id"])
        self.assertEqual(j["status"], "done", j)
        f = self.c.get(j["result"]["url"])
        self.assertEqual(f.status_code, 200)
        self.assertGreater(len(f.data), 1000)
        self.assertGreater(j["result"]["saved_percent"], 50)

    def test_trim_roundtrip(self):
        r = self.post_file("/api/trim", "clip.mp4", start="0:02", end="0:05", mode="precise")
        j = self.wait(r.get_json()["job_id"])
        self.assertEqual(j["status"], "done", j)
        self.assertAlmostEqual(j["result"]["actual_seconds"], 3, delta=0.2)

    def test_analyze(self):
        r = self.post_file("/api/analyze", "speech.wav")
        self.assertEqual(r.get_json()["gaps"], 2)

    def test_rejects_bad_input(self):
        r = self.c.post("/api/compress", data={"file": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/compress", data={}).status_code, 400)
        self.assertEqual(self.post_file("/api/compress", "hi.mp3", fmt="wav").status_code, 400)
        self.assertEqual(self.post_file("/api/trim", "hi.mp3", start="abc").status_code, 400)
        self.assertEqual(self.c.post("/api/download", json={"url": "http://127.0.0.1/x"}).status_code, 400)

    def test_no_path_traversal(self):
        self.assertEqual(self.c.get("/files/..%2f..%2fetc/passwd").status_code, 404)
        self.assertEqual(self.c.get("/files/" + "a" * 32 + "/../../config.py").status_code, 404)
        self.assertEqual(self.c.get("/api/jobs/not-an-id").status_code, 404)

    def test_bad_media_gives_friendly_error(self):
        r = self.c.post("/api/compress", data={"file": (io.BytesIO(b"junk"), "x.mp3")}, content_type="multipart/form-data")
        j = self.wait(r.get_json()["job_id"])
        self.assertEqual(j["status"], "error")
        self.assertIn("could not be read", j["error"])


if __name__ == "__main__":
    unittest.main()
