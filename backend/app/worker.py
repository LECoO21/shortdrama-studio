"""One durable local render worker. No claim of distributed queue semantics."""
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
import time
from pathlib import Path

from . import storage as db
from . import analysis
from .render import RenderCancelled, render_document
from .schema import Document

STOP = threading.Event()
MAX_RECOVERY_ATTEMPTS = 3
logger = logging.getLogger("studio.worker")


def recover():
    """Caller holds the exclusive worker lock; no other local attempt is live."""
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        for row in c.execute(db.jobs.select().where(db.jobs.c.status == "running")):
            status = "cancelled" if row.cancel_requested else (
                "failed" if row.attempt >= MAX_RECOVERY_ATTEMPTS else "queued")
            c.execute(db.jobs.update().where(db.jobs.c.id == row.id).values(
                status=status, progress=0, output_path=None, updated_at=db.now(),
                error="工作进程多次中断，请检查环境后重试" if status == "failed" else None))


def claim():
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = c.execute(db.jobs.select().where(db.jobs.c.status == "queued",
            db.jobs.c.cancel_requested == 0).order_by(db.jobs.c.created_at).limit(1)).first()
        if row is None:
            return None
        attempt = row.attempt + 1
        c.execute(db.jobs.update().where(db.jobs.c.id == row.id, db.jobs.c.status == "queued").values(
            status="running", attempt=attempt, progress=0, error=None, output_path=None, updated_at=db.now()))
        return dict(row._mapping) | {"status": "running", "attempt": attempt}


def _matches(row, job):
    return row is not None and row.status == "running" and row.attempt == job["attempt"]


def process_job(job):
    job_id, attempt = job["id"], job["attempt"]
    attempt_dir = db.DATA / "work" / f"job-{job_id}-attempt-{attempt}"
    # Attempts have unique paths, including after a crash. Existing files are
    # deliberately not reused or served to clients.
    attempt_dir.mkdir(parents=True, exist_ok=True)
    temporary = attempt_dir / "verified.mp4"
    published = db.DATA / "renders" / f"{job_id}-a{attempt}.mp4"
    last_progress = [-1, 0.0]

    def cancelled():
        if STOP.is_set():
            return True
        with db.engine.connect() as c:
            row = c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first()
            return not _matches(row, job) or bool(row.cancel_requested)

    def progress(value):
        percent = min(99, max(0, round(value * 100)))
        if percent == last_progress[0] or time.monotonic() - last_progress[1] < 0.2:
            return
        with db.engine.begin() as c:
            c.execute(db.jobs.update().where(db.jobs.c.id == job_id,
                db.jobs.c.status == "running", db.jobs.c.attempt == attempt).values(
                    progress=percent, updated_at=db.now()))
        last_progress[:] = [percent, time.monotonic()]

    try:
        document = Document.model_validate(json.loads(job["document_json"])).model_dump()
        with db.engine.connect() as c:
            all_assets = db.asset_map(c)
        assets = {clip["source"]["asset_id"]: all_assets[clip["source"]["asset_id"]]
                  for clip in document["clips"]}
        for asset in assets.values():
            with Path(asset["path"]).open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest != asset["sha256"]:
                raise ValueError("原始媒体完整性校验失败")
        render_document(document, assets, temporary, progress=progress, cancelled=cancelled)
        # The renderer decodes its output and verifies the frame count/canvas.
        # Publication is guarded again, so a late cancel cannot expose a file.
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            row = c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first()
            if not _matches(row, job) or row.cancel_requested or STOP.is_set():
                raise RenderCancelled()
            os.replace(temporary, published)
            c.execute(db.jobs.update().where(db.jobs.c.id == job_id, db.jobs.c.attempt == attempt).values(
                status="succeeded", progress=100, output_path=str(published), updated_at=db.now(), error=None))
        logger.info("render_done job=%s attempt=%s", job_id, attempt)
    except RenderCancelled:
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            row = c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first()
            if _matches(row, job):
                # A supervisor shutdown is resumable; an explicit user cancel
                # is terminal. Recovery at next start handles the former.
                status = "cancelled" if row.cancel_requested else "running"
                c.execute(db.jobs.update().where(db.jobs.c.id == job_id).values(
                    status=status, updated_at=db.now(), output_path=None))
        logger.info("render_interrupted job=%s attempt=%s", job_id, attempt)
    except Exception as exc:
        # Do not expose ffmpeg output, paths, source text or stack traces.
        message = ("渲染超时，请缩短工程后重试" if isinstance(exc, TimeoutError) else
                   "素材校验或渲染失败，请检查工程及原始媒体后重试")
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            row = c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first()
            if _matches(row, job):
                status = "cancelled" if row.cancel_requested else "failed"
                c.execute(db.jobs.update().where(db.jobs.c.id == job_id).values(
                    status=status, error=None if row.cancel_requested else message,
                    output_path=None, updated_at=db.now()))
        logger.error("render_failed job=%s attempt=%s type=%s", job_id, attempt, type(exc).__name__)
    finally:
        shutil.rmtree(attempt_dir, ignore_errors=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    with (db.DATA / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("已有本地 worker 运行，不能启动第二个")
        db.migrate()
        recover()
        analysis.recover()
        logger.info("worker_ready mode=local-single-worker")
        while not STOP.is_set():
            job = claim()
            if job:
                process_job(job)
            analysis_job = analysis.claim() if not STOP.is_set() else None
            if analysis_job:
                analysis.process_job(analysis_job, stopped=STOP.is_set)
            if not job and not analysis_job:
                STOP.wait(0.3)


if __name__ == "__main__":
    main()
