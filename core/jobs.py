"""Background jobs with progress. In-memory for now.

WEBSITE NOTE: when you host this for many users, replace JobManager with a real queue
(Celery/RQ + Redis) and keep the same Job fields -- the web layer only uses get()/submit()/cancel().
"""
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import config
from core.errors import MediaError


class Job:
    def __init__(self, job_id, kind):
        self.id, self.kind = job_id, kind
        self.status = "queued"        # queued | running | done | error | cancelled
        self.progress = 0.0
        self.message = "Waiting to start…"
        self.result, self.error = None, None
        self.cancelled = False
        self.created = time.time()

    def update(self, progress=None, message=None):
        if progress is not None:
            self.progress = round(float(progress), 1)
        if message:
            self.message = message

    def public(self):
        return {"id": self.id, "kind": self.kind, "status": self.status, "progress": self.progress,
                "message": self.message, "result": self.result, "error": self.error}


class JobManager:
    def __init__(self, workers=config.MAX_WORKERS):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.jobs = {}
        self.lock = threading.Lock()

    @staticmethod
    def new_id():
        return uuid.uuid4().hex

    def submit(self, kind, fn, job_id=None, cleanup_dirs=()):
        job = Job(job_id or self.new_id(), kind)
        with self.lock:
            self.jobs[job.id] = job

        def run():
            if job.cancelled:
                job.status = "cancelled"
                return
            job.status, job.message = "running", "Working…"
            try:
                job.result = fn(job)
                job.status, job.progress, job.message = "done", 100.0, "Done"
            except MediaError as e:
                job.status, job.error = ("cancelled", None) if "Cancelled" in str(e) else ("error", str(e))
                job.message = "Cancelled" if job.status == "cancelled" else "Failed"
            except Exception as e:                      # never leak stack traces to users
                job.status, job.error, job.message = "error", "Something unexpected went wrong.", "Failed"
                import logging
                logging.exception("Job %s crashed: %s", job.id, e)
            finally:
                for d in cleanup_dirs:                  # uploads are no longer needed
                    shutil.rmtree(d, ignore_errors=True)

        self.pool.submit(run)
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job and job.status in ("queued", "running"):
            job.cancelled = True
        return job


def cleanup_old_files(ttl_hours=config.FILE_TTL_HOURS):
    cutoff = time.time() - ttl_hours * 3600
    for base in (config.UPLOAD_DIR, config.OUTPUT_DIR):
        for d in base.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
