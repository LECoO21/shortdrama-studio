#!/usr/bin/env python3
"""Private, read-only workspace snapshots. Never imports app/storage or restores.

Uses SQLite's backup API against a pinned read transaction, including committed
WAL data. Only DB-referenced originals, published renders, analysis artifacts
and acknowledged resumable-upload parts are copied. Other work files, logs,
environment files and account state are never scanned.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUPS = Path.home() / "Library/Application Support/Shortdrama Studio/backups"
FORMAT_VERSION = 1


class SnapshotError(Exception):
    """A non-content-bearing failure code safe for CLI output."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _absolute(path):
    return Path(os.path.abspath(Path(path).expanduser()))


def _no_symlink_chain(path):
    for part in (path, *path.parents):
        try:
            if stat.S_ISLNK(part.lstat().st_mode):
                raise SnapshotError("SYMLINK_REFUSED")
        except FileNotFoundError:
            continue


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise SnapshotError("INVALID_REFERENCE")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in (".", "..") or p.startswith(".") for p in value.split("/")):
        raise SnapshotError("REFERENCE_OUTSIDE_PUBLISHED_DATA")
    if any(not p for p in value.split("/")):
        raise SnapshotError("INVALID_REFERENCE")
    return path


@contextmanager
def _open_regular(root, relative):
    """Walk each referenced component without following symlinks."""
    relative = _relative(str(relative))
    _no_symlink_chain(root)
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("NON_REGULAR_REFERENCE")
        with os.fdopen(os.dup(fd), "rb") as stream:
            yield stream, info
    except OSError as exc:
        if exc.errno in (40, 62):  # ELOOP on Linux/macOS.
            raise SnapshotError("SYMLINK_REFUSED") from None
        raise SnapshotError("MISSING_OR_UNSAFE_REFERENCE") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _file_state(root, relative):
    with _open_regular(root, relative) as (stream, before):
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        if _identity(before) != _identity(after):
            raise SnapshotError("SOURCE_CHANGED")
        return {"sha256": digest, "size": after.st_size, "identity": _identity(after)}


def _private_dir(path):
    _no_symlink_chain(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _copy_file(root, relative, destination):
    _private_dir(destination.parent)
    with _open_regular(root, relative) as (source, before):
        digest = hashlib.sha256()
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        after = os.fstat(source.fileno())
        if _identity(before) != _identity(after):
            raise SnapshotError("SOURCE_CHANGED")
        return {"sha256": digest.hexdigest(), "size": after.st_size, "identity": _identity(after)}


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _value(value):
    if value is None:
        return ["null"]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, float):
        return ["float", value.hex()]
    return [type(value).__name__, value]


def _database_state(connection):
    schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    tables = {}
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")]
        rows = sorted(_sha([_value(v) for v in row])
                      for row in connection.execute(f"SELECT * FROM {_quote(name)}"))
        tables[name] = {"rows": len(rows), "sha256": _sha([columns, rows])}
    revisions = []
    if "projects" in tables:
        revisions = [{"id": row[0], "revision": row[1]} for row in
                     connection.execute("SELECT id,revision FROM projects ORDER BY id")]
    state = {"tables": tables, "schema_sha256": _sha(schema), "project_revisions": revisions}
    return state | {"logical_sha256": _sha(state)}


def _readonly(path):
    _no_symlink_chain(path)
    # SQLite itself reads WAL/shared-memory sidecars, so reject redirects there
    # before handing the filename to the database library as well.
    for suffix in ("-wal", "-shm", "-journal"):
        _no_symlink_chain(Path(str(path) + suffix))
    if not path.is_file():
        raise SnapshotError("DATABASE_MISSING")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def _check_database(connection):
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise SnapshotError("DATABASE_INTEGRITY_FAILED")


def _upload_parts(connection):
    """Derive only the exact acknowledged parts from a pinned DB checkpoint."""
    if not connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='uploads'").fetchone():
        return {}
    result = {}
    for upload_id, filename, size, offset, status, raw in connection.execute(
            "SELECT id,filename,size,offset,status,chunks_json FROM uploads"):
        if status in {"completed", "cancelled"}:
            continue
        if (status not in {"uploading", "validating", "failed"}
                or type(offset) is not int or type(size) is not int
                or not 0 <= offset <= size <= 100 * 1024 * 1024 or size == 0):
            raise SnapshotError("INVALID_UPLOAD_CHECKPOINT")
        if not offset:
            # No acknowledged bytes to restore. A possible uncommitted tail is
            # deliberately not treated as saved progress.
            continue
        if (not isinstance(upload_id, str) or not re.fullmatch(r"[a-f0-9]{32}", upload_id)
                or not isinstance(filename, str) or any(c in filename for c in ("/", "\\", "\0"))
                or Path(filename).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a"}):
            raise SnapshotError("INVALID_UPLOAD_CHECKPOINT")
        try:
            chunks = json.loads(raw)
        except (ValueError, TypeError):
            raise SnapshotError("INVALID_UPLOAD_CHECKPOINT") from None
        if not isinstance(chunks, list) or not chunks:
            raise SnapshotError("INVALID_UPLOAD_CHECKPOINT")
        position = 0
        for chunk in chunks:
            if (not isinstance(chunk, dict) or type(chunk.get("offset")) is not int
                    or chunk["offset"] != position or type(chunk.get("size")) is not int
                    or not 0 < chunk["size"] <= 4 * 1024 * 1024
                    or not isinstance(chunk.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", chunk["sha256"])):
                raise SnapshotError("INVALID_UPLOAD_CHECKPOINT")
            position += chunk["size"]
        if position != offset:
            raise SnapshotError("INVALID_UPLOAD_CHECKPOINT")
        relative = f"work/uploads/{upload_id}.part{Path(filename).suffix.lower()}"
        result[relative] = {"offset": offset, "chunks": chunks}
    return result


def _validate_upload_part(root, relative, checkpoint):
    """An upload checkpoint promises exactly these acknowledged bytes."""
    with _open_regular(root, relative) as (stream, before):
        if before.st_size != checkpoint["offset"]:
            raise SnapshotError("UPLOAD_PART_SIZE_MISMATCH")
        for chunk in checkpoint["chunks"]:
            content = stream.read(chunk["size"])
            if len(content) != chunk["size"] or hashlib.sha256(content).hexdigest() != chunk["sha256"]:
                raise SnapshotError("UPLOAD_PART_HASH_MISMATCH")
        if _identity(before) != _identity(os.fstat(stream.fileno())):
            raise SnapshotError("SOURCE_CHANGED")


def _references(connection, data):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    references = {}

    def add(relative, expected=None, *, upload_part=False):
        relative = str(_relative(relative))
        if (relative.split("/", 1)[0] not in {"originals", "renders", "analysis"}
                and not (upload_part and re.fullmatch(
                    r"work/uploads/[a-f0-9]{32}\.part\.(mp4|mov|mkv|webm|wav|mp3|m4a)", relative))):
            raise SnapshotError("REFERENCE_OUTSIDE_PUBLISHED_DATA")
        if expected is not None and not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise SnapshotError("INVALID_REFERENCE_HASH")
        previous = references.setdefault(relative, expected)
        if previous is not None and expected is not None and previous != expected:
            raise SnapshotError("CONFLICTING_REFERENCE_HASH")
        if expected is not None:
            references[relative] = expected

    def stored_path(value, category, expected=None):
        if not isinstance(value, str) or not value:
            raise SnapshotError("INVALID_REFERENCE")
        path = Path(value)
        if not path.is_absolute():
            path = data / path
        try:
            relative = path.relative_to(data)
        except ValueError:
            raise SnapshotError("REFERENCE_OUTSIDE_PUBLISHED_DATA") from None
        if not relative.parts or relative.parts[0] != category:
            raise SnapshotError("REFERENCE_OUTSIDE_PUBLISHED_DATA")
        add(relative.as_posix(), expected)

    def manifest(value):
        if not isinstance(value, dict):
            raise SnapshotError("INVALID_ANALYSIS_MANIFEST")
        artifact = value.get("artifact_id")
        if not isinstance(artifact, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", artifact):
            raise SnapshotError("INVALID_ANALYSIS_MANIFEST")
        for key in ("proxy", "audio"):
            entry = value.get(key)
            if entry is not None:
                if not isinstance(entry, dict) or "path" not in entry:
                    raise SnapshotError("INVALID_ANALYSIS_MANIFEST")
                name = str(_relative(entry["path"]))
                add(f"analysis/{artifact}/{name}", entry.get("sha256"))
        shots = value.get("shots", [])
        if not isinstance(shots, list):
            raise SnapshotError("INVALID_ANALYSIS_MANIFEST")
        for shot in shots:
            if not isinstance(shot, dict) or "thumbnail" not in shot:
                raise SnapshotError("INVALID_ANALYSIS_MANIFEST")
            add(f"analysis/{artifact}/{_relative(shot['thumbnail'])}")

    if "assets" in tables:
        for path, digest in connection.execute("SELECT path,sha256 FROM assets"):
            stored_path(path, "originals", digest)
    if "jobs" in tables:
        for (path,) in connection.execute("SELECT output_path FROM jobs WHERE output_path IS NOT NULL"):
            stored_path(path, "renders")
    if "analyses" in tables:
        for (raw,) in connection.execute("SELECT manifest_json FROM analyses"):
            manifest(json.loads(raw))
    if "analysis_jobs" in tables:
        for (raw,) in connection.execute("SELECT result_json FROM analysis_jobs WHERE result_json IS NOT NULL"):
            checkpoint = json.loads(raw)
            if not isinstance(checkpoint, dict):
                raise SnapshotError("INVALID_ANALYSIS_CHECKPOINT")
            if checkpoint.get("manifest") is not None:
                manifest(checkpoint["manifest"])
    for relative in _upload_parts(connection):
        add(relative, upload_part=True)
    return dict(sorted(references.items()))


def _write_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _summary(path, manifest, source_matches=None):
    return {"ok": True, "snapshot_dir": str(path), "tables": len(manifest["database"]["tables"]),
            "rows": sum(t["rows"] for t in manifest["database"]["tables"].values()),
            "project_revision_count": len(manifest["database"]["project_revisions"]),
            "media_files": len(manifest["files"]), "logical_sha256": manifest["database"]["logical_sha256"],
            "source_matches": source_matches}


def create_snapshot(data_dir=ROOT / "data", backup_root=DEFAULT_BACKUPS):
    data, backups = _absolute(data_dir), _absolute(backup_root)
    _no_symlink_chain(data)
    _no_symlink_chain(backups)
    if backups == data or data in backups.parents or "Mobile Documents" in backups.parts:
        raise SnapshotError("BACKUP_LOCATION_NOT_PRIVATE")
    if any((parent / ".git").exists() for parent in (backups, *backups.parents)):
        raise SnapshotError("BACKUP_LOCATION_IN_GIT")
    _private_dir(backups)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    staging, final = backups / (".incomplete-" + name), backups / name
    staging.mkdir(mode=0o700)
    source = None
    try:
        db_path = data / "studio.db"
        source = _readonly(db_path)
        db_identity = _identity(db_path.stat())[:2]
        version = source.execute("PRAGMA data_version").fetchone()[0]
        source.execute("BEGIN")
        _check_database(source)
        state = _database_state(source)
        references = _references(source, data)
        upload_parts = _upload_parts(source)
        for relative, checkpoint in upload_parts.items():
            _validate_upload_part(data, relative, checkpoint)
        target_path = staging / "studio.db"
        fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(target_path)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            _check_database(target)
            if _database_state(target) != state:
                raise SnapshotError("DATABASE_BACKUP_MISMATCH")
        files = {}
        for relative, expected in references.items():
            copied = _copy_file(data, relative, staging / "media" / relative)
            if expected is not None and copied["sha256"] != expected:
                raise SnapshotError("REFERENCE_HASH_MISMATCH")
            files[relative] = copied
        for relative, copied in files.items():
            if _file_state(data, relative) != copied:
                raise SnapshotError("SOURCE_CHANGED")
        source.rollback()
        if (source.execute("PRAGMA data_version").fetchone()[0] != version
                or _identity(db_path.stat())[:2] != db_identity):
            raise SnapshotError("SOURCE_DATABASE_CHANGED")
        source.close()
        source = None
        with closing(_readonly(db_path)) as current:
            current.execute("BEGIN")
            if _database_state(current) != state:
                raise SnapshotError("SOURCE_DATABASE_CHANGED")
        manifest = {"format_version": FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                    "source_data_dir": str(data), "database": state, "files": files}
        _write_json(staging / "manifest.json", manifest)
        verify_snapshot(staging)
        staging.rename(final)
        return _summary(final, manifest, True)
    except Exception:
        if source is not None:
            source.close()
        shutil.rmtree(staging)
        raise


def verify_snapshot(snapshot_dir, *, check_source=False):
    snapshot = _absolute(snapshot_dir)
    _no_symlink_chain(snapshot)
    if not snapshot.is_dir():
        raise SnapshotError("SNAPSHOT_MISSING")
    with _open_regular(snapshot, "manifest.json") as (stream, _):
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise SnapshotError("SNAPSHOT_FORMAT_UNSUPPORTED")
    data = _absolute(manifest["source_data_dir"])
    with closing(_readonly(snapshot / "studio.db")) as connection:
        connection.execute("BEGIN")
        _check_database(connection)
        state = _database_state(connection)
        if state != manifest["database"]:
            raise SnapshotError("DATABASE_BACKUP_MISMATCH")
        references = _references(connection, data)
        upload_parts = _upload_parts(connection)
    if set(references) != set(manifest["files"]):
        raise SnapshotError("REFERENCE_INVENTORY_MISMATCH")
    expected_files = {"manifest.json", "studio.db"}
    for relative, copied in manifest["files"].items():
        actual = _file_state(snapshot / "media", relative)
        if actual["sha256"] != copied["sha256"] or actual["size"] != copied["size"]:
            raise SnapshotError("BACKUP_MEDIA_MISMATCH")
        if references[relative] is not None and actual["sha256"] != references[relative]:
            raise SnapshotError("REFERENCE_HASH_MISMATCH")
        if relative in upload_parts:
            _validate_upload_part(snapshot / "media", relative, upload_parts[relative])
        expected_files.add("media/" + relative)
    actual_files = set()
    for directory, directories, filenames in os.walk(snapshot, followlinks=False):
        directory = Path(directory)
        if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
            raise SnapshotError("SNAPSHOT_PERMISSIONS_UNSAFE")
        for name in directories + filenames:
            path = directory / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SnapshotError("SYMLINK_REFUSED")
            if name in filenames:
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise SnapshotError("SNAPSHOT_PERMISSIONS_UNSAFE")
                actual_files.add(path.relative_to(snapshot).as_posix())
    if actual_files != expected_files:
        raise SnapshotError("UNEXPECTED_SNAPSHOT_FILES")
    if check_source:
        with closing(_readonly(data / "studio.db")) as source:
            source.execute("BEGIN")
            if _database_state(source) != state:
                raise SnapshotError("SOURCE_DATABASE_CHANGED")
        for relative, copied in manifest["files"].items():
            if _file_state(data, relative) != copied:
                raise SnapshotError("SOURCE_CHANGED")
    return _summary(snapshot, manifest, True if check_source else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create a private snapshot; never overwrites product data")
    create.add_argument("--data-dir", type=Path, default=ROOT / "data")
    verify = commands.add_parser("verify", help="Read back DB, revisions and every referenced media hash")
    verify.add_argument("snapshot_dir", type=Path)
    verify.add_argument("--check-source", action="store_true", help="Also compare the current workspace")
    args = parser.parse_args(argv)
    try:
        result = create_snapshot(args.data_dir) if args.command == "create" else verify_snapshot(
            args.snapshot_dir, check_source=args.check_source)
    except Exception:
        exc = sys.exc_info()[1]
        code = str(exc) if isinstance(exc, SnapshotError) else "SNAPSHOT_FAILED"
        print(json.dumps({"ok": False, "error_code": code}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
