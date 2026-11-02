import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from fractions import Fraction
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from . import storage as db
from .schema import CreateProjectRequest, Document, RestoreRequest, RevisionRequest, SaveRequest
from .analysis import AnalysisProblem, router as analysis_router
from .chatgpt_connection import router as chatgpt_router
from .transcription import router as transcription_router
from .subtitle_proposals import router as subtitle_proposal_router
from . import library
from .uploads import router as uploads_router


class Problem(Exception):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details


@asynccontextmanager
async def lifespan(_):
    db.migrate()
    yield


app = FastAPI(title="短剧剪辑工作台 · 工程验证", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://127.0.0.1:3031", "http://localhost:3031"],
                   allow_methods=["GET", "POST", "PUT", "PATCH"],
                   allow_headers=["Content-Type", "X-Studio-Client"])


@app.middleware("http")
async def local_boundary(request, call_next):
    paid_search = request.url.path == "/api/v1/search" and request.query_params.get("mode") == "semantic"
    if request.method in {"POST", "PUT", "DELETE", "PATCH"} or paid_search:
        origin = request.headers.get("origin")
        if (request.headers.get("x-studio-client") != "1" or
                origin not in (None, "http://127.0.0.1:3031", "http://localhost:3031")):
            return JSONResponse({"error": {"code": "LOCAL_ACCESS_ONLY", "message": "仅允许本机工作台提交修改"}}, status_code=403)
    return await call_next(request)


@app.exception_handler(Problem)
@app.exception_handler(AnalysisProblem)
async def problem_handler(_, exc):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message,
                                   "details": exc.details}}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_handler(_, exc):
    details = [{"field": ".".join(str(v) for v in e["loc"]), "message": e["msg"]}
               for e in exc.errors()]
    return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "请检查工程字段与范围", "details": details}}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_handler(_, exc):
    logging.error("request_failed type=%s", type(exc).__name__)
    return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "操作未完成，请重试并提供任务编号"}}, status_code=500)


app.include_router(analysis_router)
app.include_router(chatgpt_router)
app.include_router(transcription_router)
app.include_router(subtitle_proposal_router)
app.include_router(library.router)
app.include_router(uploads_router)


def project_value(row):
    return {"id": row.id, "revision": row.revision, "document": json.loads(row.document_json)}


def get_project(c, project_id):
    row = c.execute(db.projects.select().where(db.projects.c.id == project_id)).first()
    if row is None:
        raise Problem("NOT_FOUND", "工程不存在", 404)
    return row


def safe_asset(value):
    result = {k: v for k, v in value.items() if k != "path"}
    try:
        result["size_bytes"] = Path(value["path"]).stat().st_size
    except OSError:
        result["size_bytes"] = None
    return result | {"media_url": f"/api/v1/assets/{value['id']}/media"}


def job_value(row):
    d = dict(row._mapping)
    d.pop("document_json", None)
    d.pop("output_path", None)
    d["output_url"] = f"/api/v1/jobs/{row.id}/output" if row.status == "succeeded" else None
    return d


def receipt(c, key, value):
    row = c.execute(db.receipts.select().where(db.receipts.c.id == key)).first()
    if row:
        if row.fingerprint != db.fingerprint(value):
            raise Problem("IDEMPOTENCY_CONFLICT", "同一请求编号不能用于不同内容", 409)
        return json.loads(row.response_json)
    return None


def record_receipt(c, key, value, response):
    c.execute(db.receipts.insert().values(id=key, fingerprint=db.fingerprint(value), response_json=db.dumps(response)))


def snapshot(c, project_id, revision, document):
    c.execute(db.snapshots.insert().values(id=str(uuid.uuid4()), project_id=project_id,
        revision=revision, document_json=db.dumps(document), sha256=db.fingerprint(document), created_at=db.now()))


def media_issues(document, assets):
    issues = []
    for clip in document["clips"]:
        source = clip["source"]
        asset = assets.get(source["asset_id"])
        issue = None
        if not asset or source["version"] != asset["version"]:
            issue = ("MISSING_ASSET", "素材或素材版本不存在")
        elif not Path(asset["path"]).is_file():
            issue = ("MISSING_FILE", "原始媒体文件缺失")
        else:
            stream = next((s for s in asset["metadata"]["streams"] if s["index"] == source["stream_index"]), None)
            if not stream or stream["time_base"] != source["time_base"]:
                issue = ("SOURCE_CLOCK", "源流或时间基与入库元信息不一致")
            elif not int(stream["start_pts"]) <= int(source["in_pts"]) < int(source["out_pts"]) <= int(stream["end_pts"]):
                issue = ("SOURCE_RANGE", "源入出点超出素材范围")
            else:
                tb = Fraction(source["time_base"]["num"], source["time_base"]["den"])
                fps = Fraction(document["fps"]["num"], document["fps"]["den"])
                count = (int(source["out_pts"]) - int(source["in_pts"])) * tb * fps
                if abs(count - clip["duration_frames"]) > Fraction(1, 2) + tb * fps:
                    issue = ("DURATION_MISMATCH", "普通片段必须按 1× 播放，源范围与工程时长不相容")
                kind = next(t["kind"] for t in document["tracks"] if t["id"] == clip["track_id"])
                if stream["type"] != kind:
                    issue = ("STREAM_TYPE", "片段源流类型与轨道不一致")
        if issue:
            issues.append({"code": issue[0], "message": issue[1], "clip_id": clip["id"], "severity": "blocking"})
    return issues


def qc(document, assets):
    issues = media_issues(document, assets)
    fps = Fraction(document["fps"]["num"], document["fps"]["den"])
    subtitle_tracks = {t["id"] for t in document["tracks"] if t["enabled"] and t["kind"] == "subtitle"}
    if fps > 100 and any(s["track_id"] in subtitle_tracks for s in document["subtitles"]):
        issues.append({"code": "SUBTITLE_FPS", "message": "当前字幕渲染支持不高于 100 fps 的工程", "severity": "blocking"})
    for track in document["tracks"]:
        if not track["enabled"] or track["kind"] == "subtitle":
            continue
        clips = sorted((c for c in document["clips"] if c["track_id"] == track["id"]), key=lambda c: c["timeline_start_frame"])
        end = 0
        for clip in clips:
            if clip["timeline_start_frame"] < end:
                issues.append({"code": "OVERLAP", "message": "当前阶段同轨片段不可重叠", "clip_id": clip["id"], "severity": "blocking"})
            if track["kind"] == "video" and clip["timeline_start_frame"] > end:
                issues.append({"code": "GAP", "message": "视频轨存在空洞，请移动片段或补齐", "clip_id": clip["id"], "severity": "blocking"})
            end = max(end, clip["timeline_start_frame"] + clip["duration_frames"])
        if track["kind"] == "video" and end != document["duration_frames"]:
            issues.append({"code": "VIDEO_DURATION", "message": "视频轨需覆盖工程总时长", "severity": "blocking"})
    if not any(t["kind"] == "video" for t in document["tracks"]):
        issues.append({"code": "VIDEO_REQUIRED", "message": "当前阶段需要视频轨", "severity": "blocking"})
    enabled = {t["id"] for t in document["tracks"] if t["enabled"]}
    for sub in document["subtitles"]:
        if sub["track_id"] in enabled and sub["bottom_percent"] < 5:
            issues.append({"code": "SUBTITLE_SAFE_AREA", "message": "字幕距离底部需至少 5%", "subtitle_id": sub["id"], "severity": "blocking"})
    return issues


@app.get("/api/v1/health")
def health():
    from .media import ffmpeg_binary
    import av
    executable = ffmpeg_binary()
    r = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=5)
    return {"status": "ok", "schema_version": 1, "pyav": av.__version__,
            "ffmpeg": r.stdout.splitlines()[0] if r.stdout else "unavailable", "mode": "local-single-worker"}


@app.get("/api/v1/bootstrap")
def bootstrap():
    with db.engine.connect() as c:
        row = c.execute(db.projects.select().join(library.episodes,
            library.episodes.c.project_id == db.projects.c.id).join(library.productions,
            library.productions.c.id == library.episodes.c.production_id)
            .where(library.episodes.c.archived == 0, library.productions.c.archived == 0)
            .order_by(db.projects.c.updated_at.desc(), db.projects.c.id)).first()
        return {"project": project_value(row) if row else None,
                "assets": [safe_asset(a) for a in db.asset_map(c).values()],
                "jobs": [job_value(j) for j in c.execute(db.jobs.select().order_by(db.jobs.c.created_at.desc()).limit(100))]}


def register_asset(path, filename):
    from .media import probe_media
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with db.engine.connect() as c:
        for a in db.asset_map(c).values():
            if a["sha256"] == digest:
                check_registered_original(a)
                return a
    try:
        meta = probe_media(path)
    except Exception:
        raise Problem("INVALID_MEDIA", "媒体无法解码，请选择有效视频或音频文件", 422)
    asset_id = digest[:24]
    target = db.DATA / "originals" / f"{asset_id}{path.suffix.lower()}"
    created_at = db.now()
    value = {"id": asset_id, "version": 1, "sha256": digest, "filename": filename,
             "path": str(target), "metadata": meta, "created_at": created_at}
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        existing = db.asset_map(c).get(asset_id)
        if existing:
            if existing["sha256"] != digest:
                raise Problem("ASSET_ID_CONFLICT", "素材编号冲突，未覆盖已有原片，请保留文件并联系开发者。", 409)
            check_registered_original(existing)
            return existing
        # Serialize registration and publish a complete immutable file. Another
        # upload of the same asset cannot truncate a file a renderer is reading.
        temporary = target.with_name(f".{asset_id}-{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(path, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        c.execute(db.assets.insert().values(id=asset_id, version=1, sha256=digest, filename=filename,
                    path=str(target), metadata_json=db.dumps(meta), created_at=created_at))
    return value


def check_registered_original(asset):
    original = Path(asset["path"])
    try:
        valid = original.is_file() and not original.is_symlink() and hashlib.sha256(original.read_bytes()).hexdigest() == asset["sha256"]
    except OSError:
        valid = False
    if not valid:
        raise Problem("ORIGINAL_UNAVAILABLE", "相同内容已有素材记录，但原片缺失或校验失败。请恢复原片后重试；已有记录不会被覆盖。", 409)


@app.post("/api/v1/assets")
async def upload(file: UploadFile = File(...), production_id: str | None = Form(default=None)):
    suffix = Path(file.filename or "media").suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3", ".m4a"}:
        raise Problem("FORMAT_UNSUPPORTED", "本阶段支持 MP4/MOV/MKV/WebM/WAV/MP3/M4A", 422)
    temp = db.DATA / "work" / f"upload-{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with temp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise Problem("FILE_TOO_LARGE", "本地验证单文件限 100 MiB", 413)
                out.write(chunk)
        asset = await run_in_threadpool(register_asset, temp, Path(file.filename or "media").name)
        if production_id:
            with db.engine.begin() as c:
                c.exec_driver_sql("BEGIN IMMEDIATE")
                library.attach_assets(c, production_id, [asset["id"]])
        return safe_asset(asset)
    finally:
        temp.unlink(missing_ok=True)
        await file.close()


@app.post("/api/v1/demo")
def demo():
    project_id = "demo-project"
    with db.engine.connect() as c:
        row = c.execute(db.projects.select().where(db.projects.c.id == project_id)).first()
        if row:
            return {"project_id": row.id}
    from .media import generate_demo_media
    paths = generate_demo_media(db.DATA / "work")
    items = [register_asset(p, p.name) for p in paths]
    clips = []
    frame = 0
    for i, asset in enumerate(items):
        meta = asset["metadata"]
        stream = next(s for s in meta["streams"] if s["index"] == meta["video_stream_index"])
        tb = Fraction(stream["time_base"]["num"], stream["time_base"]["den"])
        count = (int(stream["end_pts"]) - int(stream["start_pts"])) * tb * 25
        frames = (count.numerator * 2 + count.denominator) // (2 * count.denominator)
        clips.append({"id": f"clip-{i+1}", "track_id": "video", "source": {
            "asset_id": asset["id"], "version": 1, "stream_index": stream["index"],
            "time_base": stream["time_base"], "in_pts": stream["start_pts"], "out_pts": stream["end_pts"]},
            "timeline_start_frame": frame, "duration_frames": frames})
        frame += frames
    doc = Document.model_validate({"project_id": project_id, "name": "第一幕 · 工程验证", "canvas": {"width": 640, "height": 360},
        "fps": {"num": 25, "den": 1}, "duration_frames": frame,
        "tracks": [{"id": "video", "kind": "video"}, {"id": "audio", "kind": "audio"}, {"id": "subtitle", "kind": "subtitle"}],
        "clips": clips, "subtitles": [{"id": "sub-1", "track_id": "subtitle", "text": "每一次剪辑，都留下可恢复的选择。", "start_frame": 10, "end_frame": min(frame, 125)}]}).model_dump()
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        if c.execute(db.projects.select().where(db.projects.c.id == project_id)).first():
            return {"project_id": project_id}
        c.execute(db.projects.insert().values(id=project_id, revision=1, document_json=db.dumps(doc), updated_at=db.now()))
        snapshot(c, project_id, 1, doc)
        library.ensure_episode(c, project_id, doc)
    return {"project_id": project_id}


@app.get("/api/v1/assets/{asset_id}/media")
def media(asset_id: str):
    with db.engine.connect() as c:
        asset = db.asset_map(c).get(asset_id)
    if not asset or not Path(asset["path"]).is_file():
        raise Problem("NOT_FOUND", "媒体不存在", 404)
    return FileResponse(asset["path"])


@app.get("/api/v1/projects/{project_id}")
def project(project_id: str):
    with db.engine.connect() as c:
        return project_value(get_project(c, project_id))


@app.get("/api/v1/projects")
def list_projects():
    with db.engine.connect() as c:
        rows = c.execute(db.projects.select().order_by(db.projects.c.updated_at.desc(), db.projects.c.id))
        return {"projects": [{"id": row.id, "name": json.loads(row.document_json)["name"],
                              "revision": row.revision, "updated_at": row.updated_at}
                             for row in rows]}


@app.post("/api/v1/projects")
def create_project(body: CreateProjectRequest):
    value = body.model_dump()
    key = f"projects:create:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = receipt(c, key, value)
        if previous:
            return previous
        project_id = str(uuid.uuid4())
        document = Document.model_validate({
            "project_id": project_id, "name": body.name,
            "canvas": value["canvas"], "fps": value["fps"], "duration_frames": 1,
            "tracks": [{"id": kind, "kind": kind, "enabled": True}
                       for kind in ("video", "audio", "subtitle")],
            "clips": [], "subtitles": [],
        }).model_dump()
        c.execute(db.projects.insert().values(id=project_id, revision=1,
            document_json=db.dumps(document), updated_at=db.now()))
        snapshot(c, project_id, 1, document)
        library.ensure_episode(c, project_id, document)
        response = {"id": project_id, "revision": 1, "document": document}
        record_receipt(c, key, value, response)
        return response


def commit_document(c, project_id, document, expected_revision):
    library.ensure_project_writable(c, project_id)
    if document["project_id"] != project_id:
        raise Problem("PROJECT_MISMATCH", "工程归属不一致", 422)
    issues = media_issues(document, db.asset_map(c))
    if issues:
        raise Problem("INVALID_SOURCE", "素材引用或时间范围校验失败", 422, issues)
    result = c.execute(db.projects.update().where(db.projects.c.id == project_id,
        db.projects.c.revision == expected_revision).values(revision=expected_revision + 1,
        document_json=db.dumps(document), updated_at=db.now()))
    if result.rowcount != 1:
        get_project(c, project_id)
        raise Problem("REVISION_CONFLICT", "工程已有新版本，本地修改已保留，请重新载入后处理", 409)
    snapshot(c, project_id, expected_revision + 1, document)
    library.ensure_episode(c, project_id, document)
    return {"id": project_id, "revision": expected_revision + 1, "document": document}


@app.put("/api/v1/projects/{project_id}")
def save(project_id: str, body: SaveRequest):
    value = body.model_dump()
    key = f"{project_id}:save:{body.request_id}"
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = receipt(c, key, value)
        if previous:
            return previous
        response = commit_document(c, project_id, value["document"], body.expected_revision)
        record_receipt(c, key, value, response)
        return response


@app.get("/api/v1/projects/{project_id}/snapshots")
def list_snapshots(project_id: str):
    with db.engine.connect() as c:
        get_project(c, project_id)
        return {"snapshots": [{"id": r.id, "revision": r.revision, "sha256": r.sha256, "created_at": r.created_at}
            for r in c.execute(db.snapshots.select().where(db.snapshots.c.project_id == project_id).order_by(db.snapshots.c.revision.desc()))]}


@app.post("/api/v1/projects/{project_id}/restore")
def restore(project_id: str, body: RestoreRequest):
    key = f"{project_id}:restore:{body.request_id}"
    value = body.model_dump()
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = receipt(c, key, value)
        if previous:
            return previous
        row = c.execute(db.snapshots.select().where(db.snapshots.c.project_id == project_id, db.snapshots.c.revision == body.revision)).first()
        if not row:
            raise Problem("NOT_FOUND", "历史版本不存在", 404)
        response = commit_document(c, project_id, json.loads(row.document_json), body.expected_revision)
        record_receipt(c, key, value, response)
        return response


@app.post("/api/v1/projects/{project_id}/qc")
def check(project_id: str):
    with db.engine.connect() as c:
        row = get_project(c, project_id)
        issues = qc(json.loads(row.document_json), db.asset_map(c))
        return {"revision": row.revision, "valid": not issues, "issues": issues, "rules_version": "local-1"}


def new_job(c, project_id, revision, document):
    job_id = uuid.uuid4().hex
    c.execute(db.jobs.insert().values(id=job_id, project_id=project_id, revision=revision,
        document_json=db.dumps(document), status="queued", attempt=0, progress=0, cancel_requested=0,
        created_at=db.now(), updated_at=db.now()))
    return job_value(c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first())


@app.post("/api/v1/projects/{project_id}/renders")
def submit(project_id: str, body: RevisionRequest):
    key = f"{project_id}:render:{body.request_id}"
    value = body.model_dump()
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        previous = receipt(c, key, value)
        if previous:
            return previous
        library.ensure_project_writable(c, project_id)
        row = get_project(c, project_id)
        if row.revision != body.expected_revision:
            raise Problem("REVISION_CONFLICT", "请保存并检查最新工程后导出", 409)
        document = json.loads(row.document_json)
        issues = qc(document, db.asset_map(c))
        if issues:
            raise Problem("QC_BLOCKED", "导出检查未通过，请先修复定位到的问题", 422, issues)
        response = new_job(c, project_id, row.revision, document)
        record_receipt(c, key, value, response)
        return response


def get_job(c, job_id):
    row = c.execute(db.jobs.select().where(db.jobs.c.id == job_id)).first()
    if not row:
        raise Problem("NOT_FOUND", "任务不存在", 404)
    return row


@app.get("/api/v1/jobs")
def list_jobs():
    with db.engine.connect() as c:
        return {"jobs": [job_value(r) for r in c.execute(db.jobs.select().order_by(db.jobs.c.created_at.desc()).limit(100))]}


@app.get("/api/v1/jobs/{job_id}")
def job(job_id: str):
    with db.engine.connect() as c:
        return job_value(get_job(c, job_id))


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel(job_id: str):
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = get_job(c, job_id)
        if row.status in ("queued", "running"):
            c.execute(db.jobs.update().where(db.jobs.c.id == job_id).values(cancel_requested=1,
                status="cancelled" if row.status == "queued" else "running", updated_at=db.now()))
        return job_value(get_job(c, job_id))


@app.post("/api/v1/jobs/{job_id}/retry")
def retry(job_id: str):
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = get_job(c, job_id)
        library.ensure_project_writable(c, row.project_id)
        if row.status not in ("failed", "cancelled"):
            raise Problem("INVALID_STATE", "仅失败或取消的任务可以重试", 409)
        document = json.loads(row.document_json)
        issues = qc(document, db.asset_map(c))
        if issues:
            raise Problem("QC_BLOCKED", "原快照已无法渲染", 422, issues)
        return new_job(c, row.project_id, row.revision, document)


@app.get("/api/v1/jobs/{job_id}/output")
def output(job_id: str):
    with db.engine.connect() as c:
        row = get_job(c, job_id)
    if row.status != "succeeded" or not row.output_path or not Path(row.output_path).is_file():
        raise Problem("OUTPUT_UNAVAILABLE", "成片尚未生成或已缺失", 409)
    return FileResponse(row.output_path, media_type="video/mp4", filename=f"cut-r{row.revision}-{job_id[:8]}.mp4")
