"""Isolated worker tests; never import the user's workspace database in pytest."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


SETUP = '''
import hashlib, json, os, signal, sqlite3, subprocess, sys, time, uuid
from pathlib import Path
from app import storage as db, worker
from app.main import demo, submit, retry, get_job, get_project
from app.schema import RevisionRequest
from app.media import probe_media
db.migrate()
pid = demo()["project_id"]
def queue():
    return submit(pid, RevisionRequest(expected_revision=1, request_id=uuid.uuid4().hex))
def state(jid):
    with db.engine.connect() as c:
        return dict(get_job(c, jid)._mapping)
'''


def run_case(tmp_path, code, timeout=60):
    env = os.environ.copy()
    env["STUDIO_DATA_DIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", SETUP + textwrap.dedent(code)],
        env=env, cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_render_uses_frozen_document_and_preserves_originals(tmp_path):
    run_case(tmp_path, '''
    job = queue()
    with db.engine.begin() as c:
        original = json.loads(get_project(c, pid).document_json)
        changed = original | {"duration_frames": 1000, "name": "edited after submission"}
        c.execute(db.projects.update().values(document_json=db.dumps(changed), revision=2))
        assets = db.asset_map(c)
    before = {key: hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest() for key,a in assets.items()}
    worker.process_job(worker.claim())
    result = state(job["id"])
    assert result["status"] == "succeeded", result
    assert result["attempt"] == 1 and result["progress"] == 100
    assert result["revision"] == 1
    meta = probe_media(Path(result["output_path"]))
    video = next(s for s in meta["streams"] if s["type"] == "video")
    assert video["frame_count"] == original["duration_frames"]
    assert before == {key: hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest() for key,a in assets.items()}
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        output = client.get(f"/api/v1/jobs/{job['id']}/output")
        assert output.status_code == 200 and output.content == Path(result["output_path"]).read_bytes()
    ''')


def test_running_cancel_and_retry_keep_snapshot(tmp_path):
    run_case(tmp_path, '''
    job = queue()
    original_renderer = worker.render_document
    def cancelling_render(doc, assets, output_path, progress, cancelled):
        def trigger(value):
            progress(value)
            with db.engine.begin() as c:
                c.execute(db.jobs.update().where(db.jobs.c.id == job["id"]).values(cancel_requested=1))
        return original_renderer(doc, assets, output_path, progress=trigger, cancelled=cancelled)
    worker.render_document = cancelling_render
    worker.process_job(worker.claim())
    cancelled = state(job["id"])
    assert cancelled["status"] == "cancelled" and cancelled["output_path"] is None
    assert not list((db.DATA / "renders").glob("*.mp4"))
    worker.render_document = original_renderer
    new = retry(job["id"])
    assert new["id"] != job["id"] and new["revision"] == job["revision"]
    assert state(new["id"])["document_json"] == cancelled["document_json"]
    worker.process_job(worker.claim())
    assert state(new["id"])["status"] == "succeeded"
    assert state(job["id"])["status"] == "cancelled"
    ''')


def test_stale_attempt_cannot_publish_and_corruption_is_classified(tmp_path):
    run_case(tmp_path, '''
    job = queue()
    original_renderer = worker.render_document
    def superseded(doc, assets, path, progress, cancelled):
        original_renderer(doc, assets, path, progress=progress, cancelled=cancelled)
        with db.engine.begin() as c:
            c.execute(db.jobs.update().where(db.jobs.c.id == job["id"]).values(attempt=2))
    worker.render_document = superseded
    worker.process_job(worker.claim())
    result = state(job["id"])
    assert result["attempt"] == 2 and result["status"] == "running"
    assert result["output_path"] is None and not list((db.DATA / "renders").glob("*.mp4"))
    worker.render_document = original_renderer
    worker.recover()
    with db.engine.connect() as c:
        asset = next(iter(db.asset_map(c).values()))
    Path(asset["path"]).write_bytes(b"test-only deliberate corruption")
    worker.process_job(worker.claim())
    result = state(job["id"])
    assert result["status"] == "failed" and result["output_path"] is None
    assert str(db.DATA) not in result["error"] and "Traceback" not in result["error"]
    ''')


def test_real_process_crash_restart_and_single_worker_lock(tmp_path):
    run_case(tmp_path, '''
    # A bounded, longer real render gives us time to interrupt an active FFmpeg.
    with db.engine.begin() as c:
        doc = json.loads(get_project(c, pid).document_json)
        sources = doc["clips"]
        doc["clips"] = [sources[i % 2] | {"id": f"long-{i}", "timeline_start_frame": i * 75} for i in range(20)]
        doc["duration_frames"] = 1500
        doc["canvas"] = {"width": 1280, "height": 720}
        c.execute(db.projects.update().values(document_json=db.dumps(doc)))
    job = queue()
    def launch():
        return subprocess.Popen([sys.executable, "-m", "app.worker"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    def until(predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        raise AssertionError("worker did not reach expected state")
    def has_ffmpeg_child(process):
        result = subprocess.run(["ps", "-axo", "ppid=,comm="], capture_output=True, text=True)
        return any(line.strip().startswith(str(process.pid) + " ") and "ffmpeg" in line for line in result.stdout.splitlines())
    first = launch()
    second = None
    try:
        until(lambda: state(job["id"])["status"] == "running")
        conflict = subprocess.run([sys.executable, "-m", "app.worker"], capture_output=True, text=True, timeout=5)
        assert conflict.returncode != 0 and "已有本地 worker" in conflict.stderr
        until(lambda: has_ffmpeg_child(first), 10)
        os.killpg(first.pid, signal.SIGKILL)
        first.wait(timeout=5)
        interrupted = state(job["id"])
        assert interrupted["status"] == "running" and interrupted["attempt"] == 1
        assert interrupted["output_path"] is None
        second = launch()
        until(lambda: state(job["id"])["status"] in {"succeeded", "failed"}, 45)
        final = state(job["id"])
        assert final["status"] == "succeeded" and final["attempt"] == 2, final
        assert probe_media(Path(final["output_path"]))["streams"][0]["frame_count"] == 1500
        assert len(list((db.DATA / "renders").glob("*.mp4"))) == 1
    finally:
        for process in [first, second]:
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
    ''', timeout=75)


def test_recovery_is_bounded_and_preserves_explicit_cancel(tmp_path):
    run_case(tmp_path, '''
    one, two = queue(), queue()
    with db.engine.begin() as c:
        c.execute(db.jobs.update().where(db.jobs.c.id == one["id"]).values(status="running", attempt=3))
        c.execute(db.jobs.update().where(db.jobs.c.id == two["id"]).values(status="running", attempt=1, cancel_requested=1))
    worker.recover()
    assert state(one["id"])["status"] == "failed"
    assert state(two["id"])["status"] == "cancelled"
    assert worker.claim() is None
    ''')


def test_accepted_cancel_wins_over_simultaneous_render_failure(tmp_path):
    run_case(tmp_path, '''
    job = queue()
    def failing(doc, assets, path, progress, cancelled):
        with db.engine.begin() as c:
            c.execute(db.jobs.update().where(db.jobs.c.id == job["id"]).values(cancel_requested=1))
        raise RuntimeError("private ffmpeg diagnostic")
    worker.render_document = failing
    worker.process_job(worker.claim())
    result = state(job["id"])
    assert result["status"] == "cancelled" and result["error"] is None
    assert result["output_path"] is None
    ''')


def test_database_migration_in_chinese_path_with_percent(tmp_path):
    run_case(tmp_path / "中文素材 100%", '''
    job = queue()
    assert state(job["id"])["status"] == "queued"
    assert (db.DATA / "studio.db").is_file()
    db.migrate()
    assert state(job["id"])["status"] == "queued"
    ''')
