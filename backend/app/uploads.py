"""Bounded resumable local imports and reversible production-library removal.

Committed SQLite offsets are authoritative. A session lock serializes writers;
fsynced but uncommitted tail bytes are discarded before the next write. Only a
full size/hash/media validation may publish through register_asset.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import Column, Integer, String, Table, Text
from starlette.concurrency import run_in_threadpool

from . import storage as db
from .analysis import AnalysisProblem as Problem
from .schema import StrictModel

router = APIRouter(prefix="/api/v1")
CHUNK_SIZE = 4 * 1024 * 1024
MAX_SIZE = 100 * 1024 * 1024
SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a"}
uploads = Table("uploads", db.metadata,
    Column("id", String, primary_key=True), Column("request_id", String, nullable=False, unique=True),
    Column("fingerprint", String, nullable=False), Column("filename", String, nullable=False),
    Column("size", Integer, nullable=False), Column("sha256", String, nullable=False),
    Column("last_modified", Integer, nullable=False), Column("production_id", String),
    Column("offset", Integer, nullable=False), Column("status", String, nullable=False),
    Column("chunks_json", Text, nullable=False), Column("asset_id", String),
    Column("duplicate", Integer), Column("error_json", Text),
    Column("created_at", String, nullable=False), Column("updated_at", String, nullable=False))


class UploadCreate(StrictModel):
    request_id: str = Field(min_length=8, max_length=100)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=MAX_SIZE)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    last_modified: int = Field(ge=0)
    production_id: str | None = Field(default=None, min_length=1, max_length=100)


class RemoveRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=100)


def _root():
    directory = db.DATA / "work" / "uploads"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.resolve().parent != (db.DATA / "work").resolve():
        raise Problem("UPLOAD_STORAGE_UNSAFE", "导入临时目录不安全，请检查工作目录", 409)
    return directory


def _get(c, upload_id):
    row = c.execute(uploads.select().where(uploads.c.id == upload_id)).first()
    if row is None:
        raise Problem("NOT_FOUND", "导入记录不存在", 404)
    return row


@contextmanager
def _locked(upload_id):
    if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
        raise Problem("NOT_FOUND", "导入记录不存在", 404)
    path = _root() / f"{upload_id}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _registration_locked():
    """Make concurrent session duplicate notifications deterministic too."""
    fd = os.open(_root() / "registration.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _part(row):
    return _root() / f"{row.id}.part{Path(row.filename).suffix.lower()}"


def _public(c, row):
    asset = db.asset_map(c).get(row.asset_id) if row.asset_id else None
    if asset:
        from .main import safe_asset
        asset = safe_asset(asset)
    return {key: getattr(row, key) for key in (
        "id", "request_id", "filename", "size", "sha256", "last_modified", "production_id",
        "offset", "status", "created_at", "updated_at")} | {
        "chunk_size": CHUNK_SIZE, "asset": asset,
        "duplicate": bool(row.duplicate) if row.duplicate is not None else None,
        "error": json.loads(row.error_json) if row.error_json else None}


def _state(upload_id):
    with db.engine.connect() as c:
        return _public(c, _get(c, upload_id))


def _update(upload_id, **values):
    with db.engine.begin() as c:
        c.execute(uploads.update().where(uploads.c.id == upload_id).values(updated_at=db.now(), **values))


def _error(upload_id, code, message):
    _update(upload_id, status="failed", error_json=db.dumps({"code": code, "message": message}))
    return _state(upload_id)


def _reconcile(row):
    """Never trust bytes that were not acknowledged by a committed offset."""
    path = _part(row)
    if not path.exists():
        if row.offset:
            return False
        return True
    if path.is_symlink() or not path.is_file():
        raise Problem("UPLOAD_STORAGE_UNSAFE", "导入临时文件不安全", 409)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        actual = os.fstat(fd).st_size
        if actual < row.offset:
            return False
        if actual > row.offset:
            os.ftruncate(fd, row.offset)
            os.fsync(fd)
    finally:
        os.close(fd)
    return True


@router.post("/uploads")
def create_upload(body: UploadCreate):
    if (body.filename in {".", ".."} or any(c in body.filename for c in ("/", "\\", "\0"))
            or any(ord(c) < 32 for c in body.filename)):
        raise Problem("INVALID_FILENAME", "请选择文件名，不要填写路径", 422)
    if Path(body.filename).suffix.lower() not in SUFFIXES:
        raise Problem("FORMAT_UNSUPPORTED", "本阶段支持 MP4/MOV/MKV/WebM/WAV/MP3/M4A", 422)
    value = body.model_dump()
    fingerprint = db.fingerprint(value)
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = c.execute(uploads.select().where(uploads.c.request_id == body.request_id)).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise Problem("IDEMPOTENCY_CONFLICT", "同一导入请求编号不能用于不同文件", 409)
            return _public(c, previous)
        if body.production_id:
            from .library import attach_assets
            attach_assets(c, body.production_id, [])
        upload_id = uuid.uuid4().hex
        stamp = db.now()
        c.execute(uploads.insert().values(id=upload_id, fingerprint=fingerprint, **value,
            offset=0, status="uploading", chunks_json="[]", created_at=stamp, updated_at=stamp))
        return _public(c, _get(c, upload_id))


@router.get("/uploads")
def list_uploads(production_id: str | None = Query(default=None, max_length=100)):
    with db.engine.connect() as c:
        query = uploads.select()
        if production_id is not None:
            query = query.where(uploads.c.production_id == production_id)
        rows = c.execute(query.order_by(uploads.c.updated_at.desc(), uploads.c.id).limit(100)).all()
        return {"uploads": [_public(c, row) for row in rows]}


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str):
    return _state(upload_id)


def _append(upload_id, offset, content):
    digest = hashlib.sha256(content).hexdigest()
    with _locked(upload_id):
        with db.engine.connect() as c:
            row = _get(c, upload_id)
        if row.status in {"uploading", "validating"} and not _reconcile(row):
            return _error(upload_id, "UPLOAD_PART_MISSING", "临时文件缺失或不完整；点击重试后从头上传")
        ledger = json.loads(row.chunks_json)
        previous = next((v for v in ledger if v["offset"] == offset), None)
        if previous:
            if previous["size"] != len(content) or previous["sha256"] != digest:
                raise Problem("UPLOAD_CHUNK_CONFLICT", "此位置已接收不同内容，请重新选择原文件", 409)
            return _state(upload_id)
        if row.status != "uploading":
            raise Problem("UPLOAD_NOT_WRITABLE", "当前导入状态不可接收分片，请先重试或新建导入", 409)
        if offset != row.offset:
            raise Problem("UPLOAD_OFFSET_CONFLICT", "上传位置已变化，请读取最新进度后继续", 409,
                          {"offset": row.offset})
        if not content or len(content) > CHUNK_SIZE or offset + len(content) > row.size:
            raise Problem("UPLOAD_CHUNK_SIZE", "分片不能为空、超过 4 MiB 或超出文件大小", 422)
        fd = os.open(_part(row), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.lseek(fd, row.offset, os.SEEK_SET)
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        ledger.append({"offset": offset, "size": len(content), "sha256": digest})
        _update(upload_id, offset=offset + len(content), chunks_json=db.dumps(ledger), error_json=None)
        return _state(upload_id)


@router.put("/uploads/{upload_id}/chunks")
async def put_chunk(upload_id: str, request: Request, offset: int = Query(ge=0)):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/octet-stream":
        raise Problem("UPLOAD_CONTENT_TYPE", "分片需要 application/octet-stream", 415)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > CHUNK_SIZE:
            raise Problem("UPLOAD_CHUNK_SIZE", "单个分片最多 4 MiB", 413)
        content.extend(chunk)
    return await run_in_threadpool(_append, upload_id, offset, bytes(content))


@router.post("/uploads/{upload_id}/complete")
def complete_upload(upload_id: str):
    with _locked(upload_id):
        with db.engine.connect() as c:
            row = _get(c, upload_id)
            if row.status == "completed":
                return _public(c, row)
        if row.status not in {"uploading", "validating"}:
            raise Problem("UPLOAD_NOT_COMPLETABLE", "请先重试失败的导入，再完成校验", 409)
        if row.offset != row.size:
            raise Problem("UPLOAD_INCOMPLETE", "文件尚未传完，不会登记为素材", 409, {"offset": row.offset})
        if not _reconcile(row):
            return _error(upload_id, "UPLOAD_PART_MISSING", "临时文件缺失或不完整；点击重试后从头上传")
        _update(upload_id, status="validating", error_json=None)
        path = _part(row)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != row.sha256:
            return _error(upload_id, "UPLOAD_HASH_MISMATCH", "文件指纹不一致；请重试并重新选择同一原文件，从头上传")
        try:
            from .media import probe_media
            metadata = probe_media(path)
            if metadata["duration_seconds"] > 600:
                return _error(upload_id, "MEDIA_TOO_LONG", "本地验证单素材最长 10 分钟，请选择较短素材")
            if row.production_id:
                from .library import attach_assets
                with db.engine.begin() as c:
                    attach_assets(c, row.production_id, [])
            with _registration_locked():
                if row.duplicate is None:
                    with db.engine.connect() as c:
                        duplicate = c.execute(db.assets.select().where(db.assets.c.sha256 == digest)).first() is not None
                    _update(upload_id, duplicate=int(duplicate))
                from .main import register_asset
                asset = register_asset(path, row.filename)
            # Registration is independently durable. If the target becomes
            # archived before association, retain the safe shared asset and
            # retryable session instead of claiming that media validation failed.
            _update(upload_id, asset_id=asset["id"])
            with db.engine.begin() as c:
                c.exec_driver_sql("BEGIN IMMEDIATE")
                if row.production_id:
                    attach_assets(c, row.production_id, [asset["id"]])
                c.execute(uploads.update().where(uploads.c.id == upload_id).values(
                    asset_id=asset["id"], status="completed", error_json=None, updated_at=db.now()))
        except Exception as exc:
            if hasattr(exc, "code") and hasattr(exc, "message"):
                return _error(upload_id, exc.code, exc.message)
            return _error(upload_id, "INVALID_MEDIA", "媒体无法解码或登记未完成，请检查文件后重试")
        path.unlink(missing_ok=True)
        return _state(upload_id)


@router.post("/uploads/{upload_id}/cancel")
def cancel_upload(upload_id: str):
    with _locked(upload_id):
        with db.engine.connect() as c:
            row = _get(c, upload_id)
        if row.status == "completed":
            return _state(upload_id)
        _update(upload_id, status="cancelled", error_json=None)
        _part(row).unlink(missing_ok=True)
        return _state(upload_id)


@router.post("/uploads/{upload_id}/retry")
def retry_upload(upload_id: str):
    with _locked(upload_id):
        with db.engine.connect() as c:
            row = _get(c, upload_id)
        if row.status in {"completed", "uploading", "validating"}:
            return _state(upload_id)
        if row.status == "cancelled":
            raise Problem("UPLOAD_CANCELLED", "此导入已取消，请重新选择文件创建新导入", 409)
        code = json.loads(row.error_json or "{}").get("code")
        reset = code in {"UPLOAD_PART_MISSING", "UPLOAD_HASH_MISMATCH"} or not _reconcile(row)
        if reset:
            _part(row).unlink(missing_ok=True)
            _update(upload_id, status="uploading", offset=0, chunks_json="[]", error_json=None)
        else:
            _update(upload_id, status="uploading", error_json=None)
        return _state(upload_id)


def _uses_asset(document_json, asset_id):
    try:
        document = json.loads(document_json)
        return any(clip.get("source", {}).get("asset_id") == asset_id for clip in document.get("clips", []))
    except (ValueError, TypeError, AttributeError):
        # A broken saved document must not silently authorize removal.
        raise Problem("REFERENCE_CHECK_FAILED", "工程引用无法安全读取，不能移除素材", 409) from None


def asset_references(c, asset_id, production_id=None):
    if not c.execute(db.assets.select().where(db.assets.c.id == asset_id)).first():
        raise Problem("NOT_FOUND", "素材不存在", 404)
    from .library import production_assets, project_ids_for_production
    target_ids = set(project_ids_for_production(c, production_id)) if production_id else None
    references = {key: [] for key in ("projects", "snapshots", "jobs", "analyses", "analysis_jobs", "transcripts", "productions")}
    blockers = []
    for row in c.execute(db.projects.select()):
        if _uses_asset(row.document_json, asset_id):
            item = {"id": row.id, "project_id": row.id, "revision": row.revision,
                    "name": json.loads(row.document_json).get("name", row.id)}
            references["projects"].append(item)
            if target_ids is None or row.id in target_ids:
                blockers.append({"kind": "project", **item, "message": "当前工程仍引用此素材"})
    for table, key in ((db.snapshots, "snapshots"), (db.jobs, "jobs")):
        for row in c.execute(table.select()):
            if _uses_asset(row.document_json, asset_id):
                item = {"id": row.id, "project_id": row.project_id, "revision": row.revision}
                if key == "jobs":
                    item["status"] = row.status
                    item["has_output"] = bool(row.output_path)
                references[key].append(item)
                if target_ids is None or row.project_id in target_ids:
                    blockers.append({"kind": key[:-1], **item, "message": "历史快照或冻结导出任务仍引用此素材"})
    for table, key in ((db.analyses, "analyses"), (db.analysis_jobs, "analysis_jobs"), (db.transcripts, "transcripts")):
        for row in c.execute(table.select().where(table.c.asset_id == asset_id)):
            item = {"asset_id": asset_id, "version": row.version}
            for field in ("id", "revision", "status", "mode", "generation"):
                if hasattr(row, field):
                    item[field] = getattr(row, field)
            references[key].append(item)
    for row in c.execute(production_assets.select().where(production_assets.c.asset_id == asset_id)):
        references["productions"].append({"production_id": row.production_id})
    linked = production_id is None or any(v["production_id"] == production_id for v in references["productions"])
    return {"asset_id": asset_id, "production_id": production_id, "references": references,
            "can_remove": linked and not blockers, "blockers": blockers, "linked": linked,
            "removal_scope": "production_link_only", "original_preserved": True}


@router.get("/assets/{asset_id}/references")
def get_asset_references(asset_id: str, production_id: str | None = Query(default=None, max_length=100)):
    with db.engine.connect() as c:
        return asset_references(c, asset_id, production_id)


@router.post("/productions/{production_id}/assets/{asset_id}/remove")
def remove_production_asset(production_id: str, asset_id: str, body: RemoveRequest):
    from .library import get_production, production_assets
    from .main import receipt, record_receipt
    key = f"production-asset-remove:{body.request_id}"
    value = {"production_id": production_id, "asset_id": asset_id}
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = receipt(c, key, value)
        if previous:
            return previous
        if get_production(c, production_id).archived:
            raise Problem("PRODUCTION_ARCHIVED", "项目已归档，请先恢复后再移除素材", 409)
        refs = asset_references(c, asset_id, production_id)
        if refs["blockers"]:
            raise Problem("ASSET_REFERENCED", "工程、历史快照或导出任务仍引用此素材，暂不能从项目移除", 409, refs)
        c.execute(production_assets.delete().where((production_assets.c.production_id == production_id)
                                                   & (production_assets.c.asset_id == asset_id)))
        response = {"asset_id": asset_id, "production_id": production_id, "removed": refs["linked"],
                    "original_preserved": True, "removal_scope": "production_link_only"}
        record_receipt(c, key, value, response)
        return response
