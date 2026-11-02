"""Project entry points use the existing child-process API fixture and temp DB.

No application module is imported in pytest and no user's data directory is
opened. The child gets STUDIO_DATA_DIR before importing storage.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import sqlite3
import uuid

import pytest

from test_api import api_service, error  # noqa: F401 — reuses isolated service fixture


@pytest.fixture
def projects_api(api_service):
    client, data = api_service
    with sqlite3.connect(data / "studio.db") as connection:
        for table in ("jobs", "receipts", "snapshots", "projects"):
            connection.execute(f"DELETE FROM {table}")
    return client, data


def create(client, **options):
    return client.post("/api/v1/projects", json={"request_id": uuid.uuid4().hex, **options})


def test_empty_project_list_and_default_creation(projects_api):
    client, _ = projects_api
    assert client.get("/api/v1/projects").json() == {"projects": []}
    response = create(client)
    assert response.status_code == 200, response.text
    project = response.json()
    assert str(uuid.UUID(project["id"])) == project["id"]
    assert project["revision"] == 1
    document = project["document"]
    assert document["project_id"] == project["id"]
    assert document["name"] == "未命名剪辑"
    assert document["canvas"] == {"width": 640, "height": 360}
    assert document["fps"] == {"num": 25, "den": 1}
    assert document["schema_version"] == 1 and document["sample_rate"] == 48000
    assert document["duration_frames"] == 1
    assert document["clips"] == [] and document["subtitles"] == []
    assert document["tracks"] == [{"id": kind, "kind": kind, "enabled": True}
                                  for kind in ("video", "audio", "subtitle")]
    assert client.get(f"/api/v1/projects/{project['id']}").json() == project
    snapshots = client.get(f"/api/v1/projects/{project['id']}/snapshots").json()["snapshots"]
    assert [snapshot["revision"] for snapshot in snapshots] == [1]
    # An empty draft is editable, but never represented as an exportable cut.
    assert client.post(f"/api/v1/projects/{project['id']}/qc").json()["valid"] is False


def test_creation_accepts_canvas_and_rational_frame_rate(projects_api):
    client, _ = projects_api
    response = create(client, name="第 2 集", canvas={"width": 1080, "height": 1920},
                      fps={"num": 30000, "den": 1001})
    assert response.status_code == 200, response.text
    document = response.json()["document"]
    assert document["name"] == "第 2 集"
    assert document["canvas"] == {"width": 1080, "height": 1920}
    assert document["fps"] == {"num": 30000, "den": 1001}


def test_projects_are_summaries_ordered_by_latest_saved_time(projects_api):
    client, _ = projects_api
    first = create(client, name="先建立").json()
    second = create(client, name="后建立").json()
    rows = client.get("/api/v1/projects").json()["projects"]
    assert [row["id"] for row in rows] == [second["id"], first["id"]]
    assert all(set(row) == {"id", "name", "revision", "updated_at"} for row in rows)
    assert [row["name"] for row in rows] == ["后建立", "先建立"]
    edited = copy.deepcopy(first["document"])
    edited["name"] = "刚刚修改"
    saved = client.put(f"/api/v1/projects/{first['id']}", json={
        "document": edited, "expected_revision": 1, "request_id": uuid.uuid4().hex})
    assert saved.status_code == 200, saved.text
    updated = client.get("/api/v1/projects").json()["projects"]
    assert [row["id"] for row in updated] == [first["id"], second["id"]]
    assert updated[0]["name"] == "刚刚修改" and updated[0]["revision"] == 2
    assert updated[0]["updated_at"] >= rows[0]["updated_at"]


def test_creation_idempotency_and_changed_content_conflict(projects_api):
    client, data = projects_api
    body = {"request_id": uuid.uuid4().hex, "name": "只建立一次"}
    first = client.post("/api/v1/projects", json=body)
    replay = client.post("/api/v1/projects", json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    error(client.post("/api/v1/projects", json={**body, "name": "不能偷换内容"}),
          409, "IDEMPOTENCY_CONFLICT")
    assert len(client.get("/api/v1/projects").json()["projects"]) == 1
    with sqlite3.connect(data / "studio.db") as connection:
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1


def test_concurrent_same_request_creates_one_project(projects_api):
    client, _ = projects_api
    body = {"request_id": uuid.uuid4().hex, "name": "重复点击"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1/projects", json=body), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert len(client.get("/api/v1/projects").json()["projects"]) == 1


def test_new_project_leaves_existing_demo_and_history_intact(projects_api):
    client, data = projects_api
    existing = create(client, name="另一已有工程").json()
    demo = client.post("/api/v1/demo")
    assert demo.status_code == 200, demo.text
    demo_id = demo.json()["project_id"]
    assert demo_id == "demo-project" and demo_id != existing["id"]
    original = client.get(f"/api/v1/projects/{demo_id}").json()
    original_history = client.get(f"/api/v1/projects/{demo_id}/snapshots").json()
    with sqlite3.connect(data / "studio.db") as connection:
        before = connection.execute("SELECT * FROM assets ORDER BY id").fetchall()
    new = create(client, name="新剪辑").json()
    assert new["id"] not in {demo_id, existing["id"]}
    assert client.get(f"/api/v1/projects/{demo_id}").json() == original
    assert client.get(f"/api/v1/projects/{demo_id}/snapshots").json() == original_history
    assert client.post("/api/v1/demo").json() == {"project_id": demo_id}
    assert client.get(f"/api/v1/projects/{existing['id']}").json() == existing
    assert len(client.get("/api/v1/projects").json()["projects"]) == 3
    with sqlite3.connect(data / "studio.db") as connection:
        assert connection.execute("SELECT * FROM assets ORDER BY id").fetchall() == before


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": "x" * 201}, {"name": 42},
    {"canvas": {"width": 641, "height": 360}},
    {"canvas": {"width": 640, "height": 1922}},
    {"fps": {"num": 121, "den": 1}},
    {"fps": {"num": 1, "den": 2}},
    {"fps": {"num": 25, "den": 0}},
    {"canvas": {"width": "640", "height": 360}},
    {"request_id": "short"}, {"request_id": "x" * 101},
])
def test_invalid_project_options_do_not_create_records(projects_api, change):
    client, _ = projects_api
    error(create(client, **change), 422, "INVALID_INPUT")
    assert client.get("/api/v1/projects").json() == {"projects": []}


@pytest.mark.parametrize("change,field", [
    ({"unsupported": True}, "unsupported"),
    ({"canvas": {"width": 640, "height": 360, "background": "black"}}, "background"),
    ({"fps": {"num": 25, "den": 1, "drop_frame": True}}, "drop_frame"),
])
def test_unknown_project_fields_are_rejected(projects_api, change, field):
    client, _ = projects_api
    rejected = error(create(client, **change), 422, "INVALID_INPUT")
    assert any(field in detail["field"] for detail in rejected["details"])
    assert client.get("/api/v1/projects").json() == {"projects": []}


def test_project_creation_uses_existing_local_write_boundary(projects_api):
    client, _ = projects_api
    for headers in ({"X-Studio-Client": ""}, {"Origin": "https://untrusted.example"}):
        response = client.post("/api/v1/projects", json={"request_id": uuid.uuid4().hex},
                               headers=headers)
        error(response, 403, "LOCAL_ACCESS_ONLY")
    assert client.get("/api/v1/projects").json() == {"projects": []}
