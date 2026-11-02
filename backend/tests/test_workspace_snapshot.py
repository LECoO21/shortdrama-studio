"""Exercise isolated SQLite/media fixtures; never imports the product storage."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import stat

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/workspace_snapshot.py"
SPEC = importlib.util.spec_from_file_location("workspace_snapshot", SCRIPT)
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


@pytest.fixture
def workspace(tmp_path):
    data = tmp_path / "workspace"
    for name in ("originals", "renders", "analysis/published", "analysis/checkpoint", "work"):
        (data / name).mkdir(parents=True)
    files = {"originals/source.mp4": b"fixture source bytes", "renders/completed.mp4": b"fixture render",
             "analysis/published/proxy.mp4": b"fixture proxy", "analysis/published/audio.wav": b"fixture audio",
             "analysis/published/shot-0001.jpg": b"fixture frame",
             "analysis/checkpoint/shot-0001.jpg": b"resumable published frame"}
    for relative, content in files.items():
        (data / relative).write_bytes(content)
    (data / "work/unfinished.mp4").write_bytes(b"must never copy")
    (data / ".env").write_text("must never open")
    connection = sqlite3.connect(data / "studio.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript('''
        CREATE TABLE assets(id TEXT PRIMARY KEY,path TEXT,sha256 TEXT);
        CREATE TABLE projects(id TEXT PRIMARY KEY,revision INTEGER,document_json TEXT);
        CREATE TABLE snapshots(project_id TEXT,revision INTEGER,document_json TEXT);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT,output_path TEXT);
        CREATE TABLE analyses(asset_id TEXT,manifest_json TEXT);
        CREATE TABLE analysis_jobs(id TEXT,result_json TEXT);
        CREATE TABLE receipts(id TEXT,response_json TEXT);
        CREATE TABLE future_business_table(id INTEGER, bytes BLOB, ratio REAL);
        CREATE INDEX project_revisions ON projects(revision);
    ''')
    manifest = {"artifact_id": "published", "proxy": {"path": "proxy.mp4"},
                "audio": {"path": "audio.wav", "sha256": hashlib.sha256(files["analysis/published/audio.wav"]).hexdigest()},
                "shots": [{"thumbnail": "shot-0001.jpg"}], "waveform": [0.1, 0.2]}
    checkpoint = {"manifest": {"artifact_id": "checkpoint", "shots": [{"thumbnail": "shot-0001.jpg"}]}}
    connection.execute("INSERT INTO assets VALUES(?,?,?)", ("a", str(data / "originals/source.mp4"),
                       hashlib.sha256(files["originals/source.mp4"]).hexdigest()))
    connection.execute("INSERT INTO projects VALUES(?,?,?)", ("p", 12, '{"name":"private title"}'))
    connection.execute("INSERT INTO snapshots VALUES(?,?,?)", ("p", 11, "{}"))
    connection.execute("INSERT INTO jobs VALUES(?,?,?)", ("render", "succeeded", str(data / "renders/completed.mp4")))
    connection.execute("INSERT INTO jobs VALUES(?,?,?)", ("running", "running", None))
    connection.execute("INSERT INTO analyses VALUES(?,?)", ("a", json.dumps(manifest)))
    connection.execute("INSERT INTO analysis_jobs VALUES(?,?)", ("failed", json.dumps(checkpoint)))
    connection.execute("INSERT INTO future_business_table VALUES(?,?,?)", (8, b"\0\xff", 0.125))
    connection.commit()
    yield data, connection, files, tmp_path / "private-backups"
    connection.close()


def test_wal_backup_reads_all_business_tables_and_referenced_publications(workspace):
    data, source, files, backups = workspace
    assert (data / "studio.db-wal").stat().st_size > 0
    before = snapshot._database_state(source)
    result = snapshot.create_snapshot(data, backups)
    copied = Path(result["snapshot_dir"])
    assert result["source_matches"] is True
    assert result["media_files"] == len(files)
    assert result["logical_sha256"] == before["logical_sha256"]
    assert snapshot.verify_snapshot(copied, check_source=True) == result
    manifest = json.loads((copied / "manifest.json").read_text())
    assert manifest["database"]["project_revisions"] == [{"id": "p", "revision": 12}]
    assert manifest["database"]["tables"]["future_business_table"]["rows"] == 1
    for relative, content in files.items():
        assert (copied / "media" / relative).read_bytes() == content
    assert not (copied / "media/work").exists()
    assert not (copied / ".env").exists()
    assert snapshot._database_state(source) == before
    for path in [copied, *copied.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_media_corruption_is_detected_without_reading_current_workspace(workspace):
    data, _, _, backups = workspace
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    (copied / "media/analysis/published/proxy.mp4").write_bytes(b"damaged")
    with pytest.raises(snapshot.SnapshotError, match="BACKUP_MEDIA_MISMATCH"):
        snapshot.verify_snapshot(copied)


def test_database_revision_corruption_is_detected(workspace):
    data, _, _, backups = workspace
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    with sqlite3.connect(copied / "studio.db") as backup:
        backup.execute("UPDATE projects SET revision=13")
    with pytest.raises(snapshot.SnapshotError, match="DATABASE_BACKUP_MISMATCH"):
        snapshot.verify_snapshot(copied)


def test_live_workspace_can_change_after_success_without_invalidating_snapshot(workspace):
    data, source, _, backups = workspace
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    source.execute("UPDATE projects SET revision=13")
    source.commit()
    assert snapshot.verify_snapshot(copied)["ok"] is True
    with pytest.raises(snapshot.SnapshotError, match="SOURCE_DATABASE_CHANGED"):
        snapshot.verify_snapshot(copied, check_source=True)


@pytest.mark.parametrize("change", ["missing", "corrupted", "outside", "work", "symlink-file", "symlink-parent"])
def test_unsafe_or_missing_references_never_publish_success(workspace, change):
    data, source, _, backups = workspace
    original = data / "originals/source.mp4"
    if change == "missing":
        original.unlink()
    elif change == "corrupted":
        original.write_bytes(b"wrong content")
    elif change in {"outside", "work"}:
        replacement = data.parent / "outside.mp4" if change == "outside" else data / "work/unfinished.mp4"
        source.execute("UPDATE assets SET path=?", (str(replacement),))
        source.commit()
    elif change == "symlink-file":
        original.rename(original.with_suffix(".real"))
        original.symlink_to(original.with_suffix(".real"))
    elif change == "symlink-parent":
        folder = data / "analysis/published"
        folder.rename(data / "real-published")
        folder.symlink_to(data / "real-published", target_is_directory=True)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create_snapshot(data, backups)
    assert list(backups.iterdir()) == []


@pytest.mark.parametrize("change", ["media", "database"])
def test_changes_during_copy_are_rejected(workspace, monkeypatch, change):
    data, source, _, backups = workspace
    original_copy = snapshot._copy_file
    changed = False

    def mutating_copy(root, relative, destination):
        nonlocal changed
        result = original_copy(root, relative, destination)
        if not changed:
            changed = True
            if change == "media":
                (data / relative).write_bytes(b"modified while snapshot in progress")
            else:
                source.execute("UPDATE projects SET revision=13")
                source.commit()
        return result

    monkeypatch.setattr(snapshot, "_copy_file", mutating_copy)
    with pytest.raises(snapshot.SnapshotError, match="SOURCE_(DATABASE_)?CHANGED"):
        snapshot.create_snapshot(data, backups)
    assert list(backups.iterdir()) == []


def test_snapshot_verification_rejects_symbolic_link_and_unknown_files(workspace):
    data, _, _, backups = workspace
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    extra = copied / "unexpected.log"
    extra.write_text("unwanted extra")
    extra.chmod(0o600)
    with pytest.raises(snapshot.SnapshotError, match="UNEXPECTED_SNAPSHOT_FILES"):
        snapshot.verify_snapshot(copied)
    extra.unlink()
    extra.symlink_to(data / "originals/source.mp4")
    with pytest.raises(snapshot.SnapshotError, match="SYMLINK_REFUSED"):
        snapshot.verify_snapshot(copied)


def test_manifest_cannot_traverse_outside_backup_media(workspace):
    data, _, _, backups = workspace
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    path = copied / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"]["../outside"] = manifest["files"].pop("originals/source.mp4")
    path.write_text(json.dumps(manifest))
    with pytest.raises(snapshot.SnapshotError, match="REFERENCE_INVENTORY_MISMATCH"):
        snapshot.verify_snapshot(copied)


def test_private_backup_location_cannot_be_inside_product_data(workspace):
    data, _, _, _ = workspace
    with pytest.raises(snapshot.SnapshotError, match="BACKUP_LOCATION_NOT_PRIVATE"):
        snapshot.create_snapshot(data, data / "backups")


def test_database_sidecar_symlinks_are_refused_before_sqlite_open(tmp_path):
    database = tmp_path / "studio.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE projects(id TEXT,revision INTEGER)")
    sidecar = tmp_path / "studio.db-wal"
    sidecar.symlink_to(tmp_path / "unrelated-file")
    with pytest.raises(snapshot.SnapshotError, match="SYMLINK_REFUSED"):
        snapshot._readonly(database)


def test_cli_failure_does_not_print_user_content_or_paths(tmp_path, capsys):
    private_path = tmp_path / "private-user-name" / "missing-snapshot"
    assert snapshot.main(["verify", str(private_path)]) == 1
    output = capsys.readouterr().out
    assert str(private_path) not in output
    assert json.loads(output) == {"ok": False, "error_code": "SNAPSHOT_MISSING"}


def upload_checkpoint(data, source, *, status="uploading", offset=None):
    """A committed two-chunk prefix, plus work files that must stay private."""
    content = b"first acknowledged chunk" + b"second acknowledged chunk"
    offset = len(content) if offset is None else offset
    first = len(b"first acknowledged chunk")
    chunks = [{"offset": 0, "size": first, "sha256": hashlib.sha256(content[:first]).hexdigest()},
              {"offset": first, "size": len(content) - first,
               "sha256": hashlib.sha256(content[first:]).hexdigest()}] if offset else []
    source.execute("CREATE TABLE uploads(id TEXT,filename TEXT,size INTEGER,offset INTEGER,status TEXT,chunks_json TEXT)")
    upload_id = "a" * 32
    source.execute("INSERT INTO uploads VALUES(?,?,?,?,?,?)",
        (upload_id, "private-original.MP4", len(content) + 100, offset, status, json.dumps(chunks)))
    source.commit()
    directory = data / "work/uploads"
    directory.mkdir()
    relative = f"work/uploads/{upload_id}.part.mp4"
    (data / relative).write_bytes(content)
    (directory / f"{upload_id}.lock").write_bytes(b"private lock not copied")
    (directory / "registration.lock").write_bytes(b"not a media reference")
    (directory / ("b" * 32 + ".part.mp4")).write_bytes(b"orphan must not be scanned")
    return relative, content


@pytest.mark.parametrize("status", ["uploading", "validating", "failed"])
def test_resumable_acknowledged_parts_survive_backup_without_scanning_work(workspace, status):
    data, source, files, backups = workspace
    relative, content = upload_checkpoint(data, source, status=status)
    before = snapshot._database_state(source)
    result = snapshot.create_snapshot(data, backups)
    copied = Path(result["snapshot_dir"])
    assert result["media_files"] == len(files) + 1
    assert (copied / "media" / relative).read_bytes() == content
    assert list((copied / "media/work/uploads").iterdir()) == [copied / "media" / relative]
    assert snapshot.verify_snapshot(copied, check_source=True)["source_matches"] is True
    with sqlite3.connect(copied / "studio.db") as saved:
        row = saved.execute("SELECT offset,status,chunks_json FROM uploads").fetchone()
        assert row[:2] == (len(content), status)
        assert sum(chunk["size"] for chunk in json.loads(row[2])) == len(content)
    assert snapshot._database_state(source) == before
    assert (data / relative).read_bytes() == content
    # Standalone verification needs neither the original part nor live upload
    # state; the persisted offset and exact acknowledged bytes remain together.
    (data / relative).unlink()
    assert snapshot.verify_snapshot(copied)["ok"] is True


@pytest.mark.parametrize("status,offset", [("completed", None), ("cancelled", None), ("uploading", 0)])
def test_completed_cancelled_and_unacknowledged_work_are_not_backup_references(workspace, status, offset):
    data, source, files, backups = workspace
    upload_checkpoint(data, source, status=status, offset=offset)
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    assert not (copied / "media/work").exists()
    manifest = json.loads((copied / "manifest.json").read_text())
    assert set(manifest["files"]) == set(files)


@pytest.mark.parametrize("change,code", [
    ("missing", "MISSING_OR_UNSAFE_REFERENCE"), ("truncated", "UPLOAD_PART_SIZE_MISMATCH"),
    ("uncommitted-tail", "UPLOAD_PART_SIZE_MISMATCH"), ("same-size-corrupt", "UPLOAD_PART_HASH_MISMATCH"),
    ("bad-ledger", "INVALID_UPLOAD_CHECKPOINT"), ("path", "INVALID_UPLOAD_CHECKPOINT"),
    ("symlink", "SYMLINK_REFUSED")])
def test_incomplete_or_unsafe_upload_checkpoint_cannot_claim_recoverable_backup(workspace, change, code):
    data, source, _, backups = workspace
    relative, content = upload_checkpoint(data, source)
    part = data / relative
    if change == "missing":
        part.unlink()
    elif change == "truncated":
        part.write_bytes(content[:-1])
    elif change == "uncommitted-tail":
        part.write_bytes(content + b"not acknowledged by database")
    elif change == "same-size-corrupt":
        part.write_bytes(b"x" + content[1:])
    elif change == "bad-ledger":
        source.execute("UPDATE uploads SET chunks_json='[]'")
        source.commit()
    elif change == "path":
        source.execute("UPDATE uploads SET filename='../private.mp4'")
        source.commit()
    elif change == "symlink":
        part.rename(part.with_suffix(".real"))
        part.symlink_to(part.with_suffix(".real"))
    before = snapshot._database_state(source)
    with pytest.raises(snapshot.SnapshotError, match=code):
        snapshot.create_snapshot(data, backups)
    assert list(backups.iterdir()) == []
    assert snapshot._database_state(source) == before


@pytest.mark.parametrize("change", ["part", "checkpoint"])
def test_upload_changes_during_backup_fail_existing_stability_checks(workspace, monkeypatch, change):
    data, source, _, backups = workspace
    relative, content = upload_checkpoint(data, source)
    original_copy = snapshot._copy_file

    def mutate_after_copy(root, candidate, destination):
        result = original_copy(root, candidate, destination)
        if candidate == relative:
            if change == "part":
                (data / relative).write_bytes(content + b"new uploading bytes")
            else:
                source.execute("UPDATE uploads SET status='cancelled'")
                source.commit()
        return result

    monkeypatch.setattr(snapshot, "_copy_file", mutate_after_copy)
    with pytest.raises(snapshot.SnapshotError, match="SOURCE_(DATABASE_)?CHANGED"):
        snapshot.create_snapshot(data, backups)
    assert list(backups.iterdir()) == []
    assert (data / relative).read_bytes() == (content + b"new uploading bytes" if change == "part" else content)


def test_backup_part_verification_checks_acknowledged_chunk_hashes_too(workspace):
    data, source, _, backups = workspace
    relative, content = upload_checkpoint(data, source)
    copied = Path(snapshot.create_snapshot(data, backups)["snapshot_dir"])
    altered = b"x" + content[1:]
    (copied / "media" / relative).write_bytes(altered)
    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][relative]["sha256"] = hashlib.sha256(altered).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(snapshot.SnapshotError, match="UPLOAD_PART_HASH_MISMATCH"):
        snapshot.verify_snapshot(copied)
