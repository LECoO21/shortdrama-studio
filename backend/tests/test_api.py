"""Exercise public API contracts against an isolated, real local service.

The child receives STUDIO_DATA_DIR before importing app.storage. No application
module is imported into pytest, so collection order cannot point these tests at
the user's workspace. A real demo is generated once; no model is called.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
import pytest


@dataclass
class Workspace:
    client: httpx.Client
    data: Path
    project: dict

    @property
    def project_url(self):
        return f"/api/v1/projects/{self.project['id']}"

    def save(self, document, revision=None, request_id=None):
        return self.client.put(self.project_url, json={
            "document": document,
            "expected_revision": self.project["revision"] if revision is None else revision,
            "request_id": request_id or uuid.uuid4().hex,
        })

    def submit(self, revision=None, request_id=None):
        return self.client.post(self.project_url + "/renders", json={
            "expected_revision": self.project["revision"] if revision is None else revision,
            "request_id": request_id or uuid.uuid4().hex,
        })

    def persisted_job(self, job_id):
        with sqlite3.connect(self.data / "studio.db") as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row)


@pytest.fixture(scope="module")
def api_service(tmp_path_factory):
    data = tmp_path_factory.mktemp("isolated-api")
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    env = os.environ.copy()
    env["STUDIO_DATA_DIR"] = str(data)
    env["PYTHONUNBUFFERED"] = "1"
    backend = Path(__file__).resolve().parents[1]
    log_path = data / "api-test.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=backend, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
        )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30,
                          headers={"X-Studio-Client": "1"}, trust_env=False)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Isolated API exited before readiness; log: {log_path}")
            try:
                response = client.get("/api/v1/health")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail(f"Isolated API did not become ready; log: {log_path}")
        yield client, data
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def workspace(api_service):
    client, data = api_service
    # There is deliberately no worker in this API contract suite. All writes
    # have finished before reset, and only this fixture's temporary DB is used.
    with sqlite3.connect(data / "studio.db") as connection:
        for table in ("jobs", "receipts", "snapshots", "projects"):
            connection.execute(f"DELETE FROM {table}")
    response = client.post("/api/v1/demo")
    assert response.status_code == 200, response.text
    project = client.get(f"/api/v1/projects/{response.json()['project_id']}").json()
    assert project["revision"] == 1
    return Workspace(client, data, project)


def error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    return response.json()["error"]


def revisions(workspace):
    response = workspace.client.get(workspace.project_url + "/snapshots")
    assert response.status_code == 200
    return [row["revision"] for row in response.json()["snapshots"]]


def test_save_cas_idempotency_and_new_client_read_back(workspace):
    document = copy.deepcopy(workspace.project["document"])
    document["name"] = "用户刚保存的工程"
    request_id = uuid.uuid4().hex
    saved = workspace.save(document, request_id=request_id)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    replay = workspace.save(document, request_id=request_id)
    assert replay.status_code == 200 and replay.json() == saved.json()
    stale = copy.deepcopy(document)
    stale["name"] = "旧标签页不能覆盖"
    error(workspace.save(stale), 409, "REVISION_CONFLICT")
    with httpx.Client(base_url=str(workspace.client.base_url), trust_env=False) as fresh:
        assert fresh.get(workspace.project_url).json() == saved.json()
    assert revisions(workspace) == [2, 1]


def test_same_idempotency_key_different_payload_cannot_overwrite(workspace):
    first = copy.deepcopy(workspace.project["document"])
    first["name"] = "先提交的内容"
    request_id = uuid.uuid4().hex
    accepted = workspace.save(first, request_id=request_id)
    assert accepted.status_code == 200
    different = copy.deepcopy(first)
    different["name"] = "重用请求编号的不同内容"
    error(workspace.save(different, request_id=request_id), 409, "IDEMPOTENCY_CONFLICT")
    assert workspace.client.get(workspace.project_url).json() == accepted.json()
    assert revisions(workspace) == [2, 1]


def test_concurrent_saves_have_exactly_one_winner(workspace):
    variants = [copy.deepcopy(workspace.project["document"]) for _ in range(2)]
    for index, document in enumerate(variants):
        document["name"] = f"并发编辑 {index}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(workspace.save, variants))
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(r.json() for r in responses if r.status_code == 200)
    loser = next(r for r in responses if r.status_code == 409)
    error(loser, 409, "REVISION_CONFLICT")
    assert workspace.client.get(workspace.project_url).json() == winner
    assert revisions(workspace) == [2, 1]


def test_restore_creates_new_revision_and_retains_later_history(workspace):
    original = copy.deepcopy(workspace.project["document"])
    modified = copy.deepcopy(original)
    modified["name"] = "修订二"
    assert workspace.save(modified).status_code == 200
    modified["name"] = "修订三"
    assert workspace.save(modified, revision=2).status_code == 200
    body = {"revision": 1, "expected_revision": 3, "request_id": uuid.uuid4().hex}
    restored = workspace.client.post(workspace.project_url + "/restore", json=body)
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 4
    assert restored.json()["document"] == original
    replay = workspace.client.post(workspace.project_url + "/restore", json=body)
    assert replay.json() == restored.json()
    assert revisions(workspace) == [4, 3, 2, 1]


@pytest.mark.parametrize("problem", ["gap", "overlap", "subtitle_safe"])
def test_qc_blocks_invalid_timeline_and_render(workspace, problem):
    document = copy.deepcopy(workspace.project["document"])
    if problem == "gap":
        document["clips"][1]["timeline_start_frame"] += 1
        document["duration_frames"] += 1
        expected = "GAP"
    elif problem == "overlap":
        document["clips"][1]["timeline_start_frame"] -= 1
        expected = "OVERLAP"
    else:
        document["subtitles"][0]["bottom_percent"] = 4.0
        expected = "SUBTITLE_SAFE_AREA"
    saved = workspace.save(document)
    assert saved.status_code == 200, saved.text
    report = workspace.client.post(workspace.project_url + "/qc")
    assert report.status_code == 200 and report.json()["valid"] is False
    assert report.json()["revision"] == 2
    assert expected in {issue["code"] for issue in report.json()["issues"]}
    blocked = error(workspace.submit(revision=2), 422, "QC_BLOCKED")
    assert expected in {issue["code"] for issue in blocked["details"]}
    assert workspace.client.get("/api/v1/jobs").json() == {"jobs": []}


def test_previous_valid_qc_cannot_authorize_modified_engineering(workspace):
    assert workspace.client.post(workspace.project_url + "/qc").json()["valid"] is True
    document = copy.deepcopy(workspace.project["document"])
    document["subtitles"][0]["bottom_percent"] = 1.0
    assert workspace.save(document).status_code == 200
    error(workspace.submit(revision=1), 409, "REVISION_CONFLICT")
    error(workspace.submit(revision=2), 422, "QC_BLOCKED")
    document["subtitles"][0]["bottom_percent"] = 8.0
    assert workspace.save(document, revision=2).status_code == 200
    report = workspace.client.post(workspace.project_url + "/qc").json()
    assert report["valid"] is True and report["revision"] == 3
    assert report["issues"] == []
    assert workspace.submit(revision=3).status_code == 200


def test_queued_cancel_is_terminal_and_unpublished(workspace):
    submitted = workspace.submit()
    assert submitted.status_code == 200, submitted.text
    job = submitted.json()
    assert job["status"] == "queued"
    url = f"/api/v1/jobs/{job['id']}"
    cancelled = workspace.client.post(url + "/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested"] == 1
    assert cancelled.json()["output_url"] is None
    assert workspace.client.post(url + "/cancel").json() == cancelled.json()
    error(workspace.client.get(url + "/output"), 409, "OUTPUT_UNAVAILABLE")
    assert workspace.client.get(workspace.project_url).json() == workspace.project


def test_render_freezes_snapshot_and_idempotent_submission(workspace):
    original = copy.deepcopy(workspace.project["document"])
    request_id = uuid.uuid4().hex
    first = workspace.submit(request_id=request_id)
    assert first.status_code == 200, first.text
    job = first.json()
    modified = copy.deepcopy(original)
    modified["name"] = "渲染期间继续修改"
    modified["subtitles"][0]["text"] = "后续字幕不属于已经提交的成片"
    assert workspace.save(modified).status_code == 200
    persisted = workspace.persisted_job(job["id"])
    assert persisted["revision"] == 1
    assert json.loads(persisted["document_json"]) == original
    replay = workspace.submit(request_id=request_id)
    assert replay.status_code == 200 and replay.json() == job
    assert len(workspace.client.get("/api/v1/jobs").json()["jobs"]) == 1
    error(workspace.submit(revision=1), 409, "REVISION_CONFLICT")
    assert "document_json" not in job and "output_path" not in job


def test_retry_keeps_frozen_revision_and_original_task(workspace):
    first = workspace.submit().json()
    old_url = f"/api/v1/jobs/{first['id']}"
    error(workspace.client.post(old_url + "/retry"), 409, "INVALID_STATE")
    workspace.client.post(old_url + "/cancel")
    original = workspace.persisted_job(first["id"])
    modified = copy.deepcopy(workspace.project["document"])
    modified["name"] = "当前 head 已经改变"
    assert workspace.save(modified).status_code == 200
    retried = workspace.client.post(old_url + "/retry")
    assert retried.status_code == 200, retried.text
    new = retried.json()
    assert new["id"] != first["id"] and new["status"] == "queued"
    assert new["revision"] == 1
    assert workspace.persisted_job(new["id"])["document_json"] == original["document_json"]
    assert workspace.persisted_job(first["id"]) == original
    assert workspace.client.get(workspace.project_url).json()["revision"] == 2


@pytest.mark.parametrize("headers", [
    {"X-Studio-Client": ""},
    {"X-Studio-Client": "not-1"},
    {"Origin": "https://untrusted.example", "X-Studio-Client": "1"},
    {"Origin": "http://127.0.0.1:3031.evil.example", "X-Studio-Client": "1"},
])
def test_write_boundary_rejects_missing_marker_and_untrusted_origin(workspace, headers):
    response = workspace.client.post(workspace.project_url + "/renders", headers=headers,
        json={"expected_revision": 1, "request_id": uuid.uuid4().hex})
    error(response, 403, "LOCAL_ACCESS_ONLY")
    assert workspace.client.get("/api/v1/jobs").json()["jobs"] == []


@pytest.mark.parametrize("origin", ["http://127.0.0.1:3031", "http://localhost:3031"])
def test_allowed_local_origin_can_write_and_receive_cors_header(workspace, origin):
    response = workspace.client.post(workspace.project_url + "/qc", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_untrusted_host_is_rejected_before_reading_project(workspace):
    response = workspace.client.get(workspace.project_url, headers={"Host": "attacker.example"})
    assert response.status_code == 400
    assert workspace.project["document"]["name"] not in response.text


@pytest.mark.parametrize("filename,expected", [
    ("pretending.mp4", "INVALID_MEDIA"),
    ("../../escape.mp4", "INVALID_MEDIA"),
    ("payload.exe", "FORMAT_UNSUPPORTED"),
])
def test_bad_media_upload_is_rejected_without_registering_or_leaving_temp(workspace, filename, expected):
    before = workspace.client.get("/api/v1/bootstrap").json()["assets"]
    response = workspace.client.post("/api/v1/assets", files={"file": (filename, b"not a video", "video/mp4")})
    error(response, 422, expected)
    after = workspace.client.get("/api/v1/bootstrap").json()["assets"]
    assert before == after
    assert not list((workspace.data / "work").glob("upload-*"))
    assert not (workspace.data.parent / "escape.mp4").exists()
    assert str(workspace.data) not in response.text
    assert all("path" not in asset for asset in after)


@pytest.mark.parametrize("change,expected", [
    ("version", "MISSING_ASSET"),
    ("asset_id", "MISSING_ASSET"),
    ("time_base", "SOURCE_CLOCK"),
    ("stream_index", "SOURCE_CLOCK"),
    ("in_pts", "SOURCE_RANGE"),
    ("out_pts", "SOURCE_RANGE"),
    ("duration", "DURATION_MISMATCH"),
])
def test_invalid_source_is_rejected_without_new_revision(workspace, change, expected):
    document = copy.deepcopy(workspace.project["document"])
    clip = document["clips"][0]
    source = clip["source"]
    if change == "version":
        source["version"] += 1
    elif change == "asset_id":
        source["asset_id"] = "invented-source-id"
    elif change == "time_base":
        source["time_base"]["den"] *= 2
    elif change == "stream_index":
        source["stream_index"] = 999
    elif change == "in_pts":
        source["in_pts"] = str(int(source["in_pts"]) - 1)
    elif change == "out_pts":
        source["out_pts"] = str(int(source["out_pts"]) + 1)
    else:
        clip["duration_frames"] -= 1
    rejected = error(workspace.save(document), 422, "INVALID_SOURCE")
    assert expected in {issue["code"] for issue in rejected["details"]}
    assert workspace.client.get(workspace.project_url).json() == workspace.project
    assert revisions(workspace) == [1]


def test_unknown_fields_are_rejected_instead_of_silently_lost(workspace):
    document = copy.deepcopy(workspace.project["document"])
    document["clips"][0]["unsupported_effect"] = {"name": "magic"}
    rejected = error(workspace.save(document), 422, "INVALID_INPUT")
    assert any("unsupported_effect" in item["field"] for item in rejected["details"])
    assert revisions(workspace) == [1]
