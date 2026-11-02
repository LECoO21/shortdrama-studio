"""Project/episode contracts against isolated child service and migration DBs."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from test_api import error


def request(**fields):
    return {"request_id": uuid.uuid4().hex, **fields}


def local_environment(data):
    return {"PATH": "/Users/leco/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "STUDIO_DATA_DIR": str(data), "STUDIO_MODEL_PROVIDER": "none", "STUDIO_MODELS_ENABLED": "0",
        "PYTHONUNBUFFERED": "1", "FFMPEG_BIN": "/Users/leco/.local/bin/ffmpeg"}


@pytest.fixture(scope="module")
def library_service(tmp_path_factory):
    data = tmp_path_factory.mktemp("isolated-library")
    backend = Path(__file__).resolve().parents[1]
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0)); port = port_socket.getsockname()[1]
    log_path = data / "api-test.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port)], cwd=backend, env=local_environment(data),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30,
        headers={"X-Studio-Client": "1"}, trust_env=False)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Isolated library API exited; log: {log_path}")
            try:
                response = client.get("/api/v1/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail(f"Isolated library API not ready; log: {log_path}")
        yield client, data
    finally:
        client.close(); process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


@pytest.fixture
def library_api(library_service):
    client, data = library_service
    with sqlite3.connect(data / "studio.db") as c:
        for table in ("production_assets", "episodes", "productions", "jobs", "receipts", "snapshots", "projects"):
            c.execute(f"DELETE FROM {table}")
    return client, data


def create(client, **fields):
    response = client.post("/api/v1/productions", json=request(name="候选短剧", **fields))
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, production):
    response = client.get(f"/api/v1/productions/{production['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_create_and_reopen_keeps_metadata_document_separate(library_api):
    client, _ = library_api
    assert client.get("/api/v1/productions").json() == {"productions": []}
    created = create(client, description="项目简介", language="zh-CN",
        canvas={"width": 1080, "height": 1920}, fps={"num": 30000, "den": 1001})
    production, project = created["production"], created["project"]
    assert production["name"] == "候选短剧" and production["description"] == "项目简介"
    assert production["language"] == "zh-CN" and production["revision"] == 1
    episode = production["episodes"][0]
    assert episode["project_id"] == project["id"] and episode["name"] == "第 1 集"
    assert episode["document_revision"] == episode["revision"] == 1
    assert episode["canvas"] == project["document"]["canvas"] == production["canvas"]
    assert episode["fps"] == project["document"]["fps"] == production["fps"]
    assert episode["duration_frames"] == 1
    assert client.get(f"/api/v1/projects/{project['id']}").json() == project
    assert detail(client, production) == production
    context = client.get(f"/api/v1/library/context/{project['id']}").json()
    assert context == {"production": production, "episode": episode, "assets": []}
    summary = client.get("/api/v1/productions").json()["productions"][0]
    assert "episodes" not in summary and summary["episode_count"] == 1


def test_project_create_idempotency_conflict_and_concurrent_duplicate(library_api):
    client, data = library_api
    body = request(name="双击建项目")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1/productions", json=body), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    error(client.post("/api/v1/productions", json={**body, "name": "另一份"}), 409, "IDEMPOTENCY_CONFLICT")
    with sqlite3.connect(data / "studio.db") as c:
        assert c.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM productions").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_metadata_cas_and_defaults_only_apply_to_new_episodes(library_api):
    client, _ = library_api
    first = create(client)
    pid = first["production"]["id"]
    body = request(expected_revision=1, name="新片名", description="补充说明", language="yue",
        canvas={"width": 720, "height": 1280}, fps={"num": 30, "den": 1})
    updated = client.patch(f"/api/v1/productions/{pid}", json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2 and updated.json()["name"] == "新片名"
    assert client.patch(f"/api/v1/productions/{pid}", json=body).json() == updated.json()
    error(client.patch(f"/api/v1/productions/{pid}", json=request(expected_revision=1, name="陈旧标签页")),
        409, "REVISION_CONFLICT")
    assert client.get(f"/api/v1/projects/{first['project']['id']}").json() == first["project"]
    second = client.post(f"/api/v1/productions/{pid}/episodes", json=request()).json()
    assert second["project"]["document"]["name"] == "第 2 集"
    assert second["project"]["document"]["canvas"] == {"width": 720, "height": 1280}
    assert second["project"]["document"]["fps"] == {"num": 30, "den": 1}
    assert second["production"]["episode_count"] == 2


def test_episode_rename_uses_document_revisions_and_snapshot(library_api):
    client, _ = library_api
    first = create(client)
    project_id = first["project"]["id"]
    body = request(expected_revision=1, expected_document_revision=1, name="首集·冲突")
    renamed = client.patch(f"/api/v1/episodes/{project_id}", json=body)
    assert renamed.status_code == 200, renamed.text
    value = renamed.json()
    assert value["episode"]["revision"] == 2 and value["episode"]["document_revision"] == 2
    assert value["project"]["revision"] == 2 and value["project"]["document"]["name"] == "首集·冲突"
    assert client.patch(f"/api/v1/episodes/{project_id}", json=body).json() == value
    snapshots = client.get(f"/api/v1/projects/{project_id}/snapshots").json()["snapshots"]
    assert [row["revision"] for row in snapshots] == [2, 1]
    error(client.patch(f"/api/v1/episodes/{project_id}", json=request(
        expected_revision=2, expected_document_revision=1, name="过期文档")), 409, "REVISION_CONFLICT")
    error(client.patch(f"/api/v1/episodes/{project_id}", json=request(expected_revision=1, archived=True)),
        409, "REVISION_CONFLICT")
    error(client.patch(f"/api/v1/episodes/{project_id}", json=request(expected_revision=2, name="漏修订号")),
        422, "INVALID_INPUT")


def test_document_rename_is_visible_and_stale_episode_rename_cannot_clobber(library_api):
    client, _ = library_api
    first = create(client)
    project = first["project"]
    edited = {**project["document"], "name": "工作台中的新名"}
    saved = client.put(f"/api/v1/projects/{project['id']}", json=request(document=edited, expected_revision=1))
    assert saved.status_code == 200, saved.text
    value = detail(client, first["production"])
    assert value["episodes"][0]["name"] == "工作台中的新名"
    assert value["episodes"][0]["revision"] == 1
    error(client.patch(f"/api/v1/episodes/{project['id']}", json=request(
        expected_revision=1, expected_document_revision=1, name="不能覆盖")), 409, "REVISION_CONFLICT")
    assert client.get(f"/api/v1/projects/{project['id']}").json() == saved.json()


def test_archive_and_restore_keep_original_document_and_history(library_api):
    client, data = library_api
    created = create(client)
    pid, project = created["production"]["id"], created["project"]
    before = client.get(f"/api/v1/projects/{project['id']}/snapshots").json()
    archived = client.patch(f"/api/v1/productions/{pid}", json=request(expected_revision=1, archived=True))
    assert archived.status_code == 200 and archived.json()["archived"] is True
    assert client.get("/api/v1/productions").json() == {"productions": []}
    assert client.get("/api/v1/productions?include_archived=true").json()["productions"][0]["id"] == pid
    error(client.post(f"/api/v1/productions/{pid}/episodes", json=request()), 409, "PRODUCTION_ARCHIVED")
    error(client.put(f"/api/v1/projects/{project['id']}", json=request(
        document=project["document"], expected_revision=1)), 409, "PRODUCTION_ARCHIVED")
    error(client.post(f"/api/v1/projects/{project['id']}/restore", json=request(
        expected_revision=1, revision=1)), 409, "PRODUCTION_ARCHIVED")
    restored = client.patch(f"/api/v1/productions/{pid}", json=request(expected_revision=2, archived=False))
    assert restored.status_code == 200 and restored.json()["archived"] is False
    body = request(expected_revision=1, archived=True)
    archived_episode = client.patch(f"/api/v1/episodes/{project['id']}", json=body)
    assert archived_episode.status_code == 200, archived_episode.text
    assert archived_episode.json()["episode"]["archived"] is True
    error(client.put(f"/api/v1/projects/{project['id']}", json=request(
        document=project["document"], expected_revision=1)), 409, "EPISODE_ARCHIVED")
    episode_restored = client.patch(f"/api/v1/episodes/{project['id']}", json=request(expected_revision=2, archived=False))
    assert episode_restored.status_code == 200
    assert client.get(f"/api/v1/projects/{project['id']}").json() == project
    assert client.get(f"/api/v1/projects/{project['id']}/snapshots").json() == before
    with sqlite3.connect(data / "studio.db") as c:
        assert c.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def test_episode_copy_is_independent_and_only_starts_one_snapshot(library_api):
    client, data = library_api
    created = create(client)
    source = created["project"]
    edited = {**source["document"], "subtitles": [{"id": "manual", "track_id": "subtitle", "text": "原字幕",
        "start_frame": 0, "end_frame": 1}]}
    saved = client.put(f"/api/v1/projects/{source['id']}", json=request(document=edited, expected_revision=1)).json()
    with sqlite3.connect(data / "studio.db") as c:
        c.execute("INSERT INTO jobs (id,project_id,revision,document_json,status,attempt) VALUES ('source-frozen',?,2,?,'failed',1)",
            (source["id"], json.dumps(saved["document"])))
    body = request(expected_revision=2, name="副本集")
    response = client.post(f"/api/v1/episodes/{source['id']}/copy", json=body)
    assert response.status_code == 200, response.text
    copied = response.json()["project"]
    assert copied["id"] != source["id"] and copied["revision"] == 1
    assert copied["document"]["subtitles"] == saved["document"]["subtitles"]
    assert client.post(f"/api/v1/episodes/{source['id']}/copy", json=body).json() == response.json()
    change = copy.deepcopy(copied["document"]); change["subtitles"][0]["text"] = "仅改副本"
    assert client.put(f"/api/v1/projects/{copied['id']}", json=request(document=change, expected_revision=1)).status_code == 200
    assert client.get(f"/api/v1/projects/{source['id']}").json() == saved
    assert len(client.get(f"/api/v1/projects/{copied['id']}/snapshots").json()["snapshots"]) == 2
    with sqlite3.connect(data / "studio.db") as c:
        assert c.execute("SELECT count(*) FROM jobs WHERE project_id=?", (copied["id"],)).fetchone()[0] == 0
        assert c.execute("SELECT document_json FROM jobs WHERE id='source-frozen'").fetchone()[0] == json.dumps(saved["document"])
    error(client.post(f"/api/v1/episodes/{source['id']}/copy", json=request(expected_revision=1)),
        409, "REVISION_CONFLICT")


def test_production_copy_copies_all_saved_episodes_and_retains_archive_state(library_api):
    client, _ = library_api
    first = create(client, description="复制元数据", language="en")
    pid = first["production"]["id"]
    second = client.post(f"/api/v1/productions/{pid}/episodes", json=request(name="归档集")).json()
    client.patch(f"/api/v1/episodes/{second['project']['id']}", json=request(expected_revision=1, archived=True))
    body = request(expected_revision=1, name="全项目副本")
    response = client.post(f"/api/v1/productions/{pid}/copy", json=body)
    assert response.status_code == 200, response.text
    copied = response.json()
    assert copied["production"]["id"] != pid and copied["production"]["name"] == "全项目副本"
    assert copied["production"]["description"] == "复制元数据"
    assert copied["production"]["language"] == "en"
    rows = copied["production"]["episodes"]
    assert len(rows) == 2 and sum(row["archived"] for row in rows) == 1
    assert {row["project_id"] for row in rows}.isdisjoint({first["project"]["id"], second["project"]["id"]})
    assert all(row["document_revision"] == row["revision"] == 1 for row in rows)
    assert copied["project"]["document"]["name"] == "第 1 集"
    assert client.post(f"/api/v1/productions/{pid}/copy", json=body).json() == copied


def test_asset_links_are_shared_read_only_and_context_never_mutates(library_api):
    client, data = library_api
    demo = client.post("/api/v1/demo")
    assert demo.status_code == 200, demo.text
    demo_id = demo.json()["project_id"]
    context = client.get(f"/api/v1/library/context/{demo_id}")
    assert context.status_code == 200, context.text
    assets = context.json()["assets"]
    assert assets and all("path" not in item and item["media_url"] for item in assets)
    created = create(client)
    pid = created["production"]["id"]
    with sqlite3.connect(data / "studio.db") as c:
        original_assets = c.execute("SELECT * FROM assets ORDER BY id").fetchall()
    body = request(asset_ids=[assets[0]["id"], assets[0]["id"]])
    attached = client.post(f"/api/v1/productions/{pid}/assets", json=body)
    assert attached.status_code == 200, attached.text
    assert attached.json()["asset_count"] == 1
    assert client.post(f"/api/v1/productions/{pid}/assets", json=body).json() == attached.json()
    current = client.get(f"/api/v1/library/context/{created['project']['id']}").json()
    assert current["assets"][0]["id"] == assets[0]["id"]
    with sqlite3.connect(data / "studio.db") as c:
        before = {table: c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("productions", "episodes", "production_assets", "receipts")}
    client.get(f"/api/v1/library/context/{created['project']['id']}")
    client.get("/api/v1/productions?include_archived=true")
    with sqlite3.connect(data / "studio.db") as c:
        assert c.execute("SELECT * FROM assets ORDER BY id").fetchall() == original_assets
        assert {table: c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in before} == before
    copied = client.post(f"/api/v1/productions/{pid}/copy", json=request(expected_revision=1)).json()
    copied_context = client.get(f"/api/v1/library/context/{copied['project']['id']}").json()
    assert {item["id"] for item in copied_context["assets"]} == {assets[0]["id"]}
    error(client.post(f"/api/v1/productions/{pid}/assets", json=request(asset_ids=["missing"])), 404, "NOT_FOUND")


def test_editing_shared_asset_links_project_and_preserves_link_after_removal(library_api):
    client, _ = library_api
    assert client.post("/api/v1/demo").status_code == 200
    demo = client.get("/api/v1/projects/demo-project").json()
    created = create(client)
    project_id = created["project"]["id"]
    document = {**demo["document"], "project_id": project_id, "name": "共享素材剪辑"}
    saved = client.put(f"/api/v1/projects/{project_id}", json=request(document=document, expected_revision=1))
    assert saved.status_code == 200, saved.text
    refs = {clip["source"]["asset_id"] for clip in document["clips"]}
    context = client.get(f"/api/v1/library/context/{project_id}").json()
    assert {asset["id"] for asset in context["assets"]} == refs
    cleared = {**document, "clips": [], "subtitles": []}
    assert client.put(f"/api/v1/projects/{project_id}", json=request(document=cleared, expected_revision=2)).status_code == 200
    assert {asset["id"] for asset in client.get(f"/api/v1/library/context/{project_id}").json()["assets"]} == refs
    assert client.get("/api/v1/projects/demo-project").json() == demo


@pytest.mark.parametrize("fields", [
    {"name": " "}, {"language": " "}, {"language": "x" * 51}, {"description": "x" * 5001},
    {"canvas": {"width": 641, "height": 360}}, {"fps": {"num": 121, "den": 1}},
    {"fps": {"num": 1, "den": 2}}, {"unsupported": True},
])
def test_invalid_metadata_does_not_create_project(library_api, fields):
    client, _ = library_api
    error(client.post("/api/v1/productions", json=request(**{"name": "测试", **fields})), 422, "INVALID_INPUT")
    assert client.get("/api/v1/productions").json() == {"productions": []}


def test_library_uses_local_modification_boundary(library_api):
    client, _ = library_api
    for headers in ({"X-Studio-Client": ""}, {"Origin": "https://untrusted.example"}):
        error(client.post("/api/v1/productions", json=request(name="拒绝"), headers=headers), 403, "LOCAL_ACCESS_ONLY")


def test_004_migration_preserves_every_old_row_and_relinks_history(tmp_path):
    """Run Alembic in child at 003 then 004; never import workspace storage."""
    backend = Path(__file__).resolve().parents[1]
    data = tmp_path / "migration"; data.mkdir()
    database = data / "studio.db"
    script = r'''
from alembic import command
from alembic.config import Config
from pathlib import Path
import os
config=Config("alembic.ini")
config.set_main_option("script_location", str(Path("migrations").resolve()))
config.set_main_option("sqlalchemy.url", "sqlite:///"+str(Path(os.environ["TEST_DATABASE"]).resolve()))
command.upgrade(config, os.environ["TEST_REVISION"])
'''
    env = {**local_environment(data), "TEST_DATABASE": str(database), "TEST_REVISION": "003"}
    result = subprocess.run([sys.executable, "-c", script], cwd=backend, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    doc = {"project_id": "existing-edit", "name": "原有工程", "canvas": {"width": 640, "height": 360},
        "fps": {"num": 25, "den": 1}, "clips": []}
    def reference(asset_id):
        return {**doc, "clips": [{"source": {"asset_id": asset_id}}]}
    with sqlite3.connect(database) as c:
        for asset in ("current", "history", "frozen", "shared"):
            c.execute("INSERT INTO assets (id,version,sha256,filename,path,metadata_json,created_at) VALUES (?,1,?,?,?,?,?)",
                (asset, asset, asset + ".mp4", "/immutable/" + asset, "{}", "2026-10-01"))
        c.execute("INSERT INTO projects VALUES (?,2,?,?)", (doc["project_id"], json.dumps(reference("current")), "2026-10-02"))
        c.execute("INSERT INTO snapshots VALUES ('snapshot',?,1,?,'hash','2026-10-01')", (doc["project_id"], json.dumps(reference("history"))))
        c.execute("INSERT INTO jobs (id,project_id,revision,document_json,status,attempt) VALUES ('job',?,1,?,'failed',1)",
            (doc["project_id"], json.dumps(reference("frozen"))))
        c.execute("INSERT INTO receipts VALUES ('existing-receipt','fingerprint','{}')")
        old_tables = [row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] != "alembic_version"]
        before = {table: c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in old_tables}
        before_hash = hashlib.sha256(repr(before).encode()).hexdigest()
    env["TEST_REVISION"] = "004"
    result = subprocess.run([sys.executable, "-c", script], cwd=backend, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as c:
        after = {table: c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in old_tables}
        assert after == before and hashlib.sha256(repr(after).encode()).hexdigest() == before_hash
        mapped = c.execute("SELECT production_id FROM episodes WHERE project_id=?", (doc["project_id"],)).fetchone()[0]
        assert c.execute("SELECT name FROM productions WHERE id=?", (mapped,)).fetchone()[0] == "原有工程"
        assert {row[0] for row in c.execute("SELECT asset_id FROM production_assets WHERE production_id=?", (mapped,))} == {
            "current", "history", "frozen"}
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "004"
