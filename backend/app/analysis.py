"""Durable, bounded asset analysis. Media evidence never changes an edit project."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import unicodedata
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import Field

from . import storage as db
from .schema import StrictModel

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger("studio.analysis")


class AnalysisProblem(Exception):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details


class AnalyzeRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=100)
    mode: Literal["local", "cloud"] = "local"


class AnnotationRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    shot_id: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    tags: list[str] = Field(max_length=30)


def asset_for(c, asset_id):
    asset = db.asset_map(c).get(asset_id)
    if not asset:
        raise AnalysisProblem("NOT_FOUND", "素材不存在", 404)
    return asset


def public_job(row):
    result = dict(row._mapping)
    checkpoint = json.loads(result.pop("result_json") or "{}")
    result["steps"] = checkpoint.get("steps", {})
    return result


def audio_urls():
    try:
        result = json.loads(os.environ.get("STUDIO_ASR_AUDIO_URLS_JSON", "{}"))
        return result if isinstance(result, dict) else {}
    except ValueError:
        return {}


def cloud_info(asset):
    if os.getenv("STUDIO_MODEL_PROVIDER", "chatgpt") != "bailian":
        return {"enabled": False, "configured": False, "asr_audio_ready": False,
            "error": "旧百炼分析入口已停用。对白请使用“本地对白”；本地镜头与关键词检索仍可使用。视觉与语义向量候选暂未开放。",
            "provider": "ChatGPT（接入验证中）",
            "models": {"asr": "本地对白候选，须逐句核对", "vision": "视觉候选尚未接入", "embedding": "本地向量候选尚未接入"},
            "data_flow": "本地对白只处理已入库音轨，不上传音频。当前不会向第三方发送素材；后续若启用视觉，仅向 OpenAI 发送选定抽帧及必要文字，原视频和音轨留在本机。视觉与向量候选尚未接入素材分析。"}
    from .model_services import ModelServiceConfig, ModelsUnavailable
    configuration_error = None
    try:
        config = ModelServiceConfig.from_env()
    except ModelsUnavailable:
        config = ModelServiceConfig()
        configuration_error = "云模型配置无效，请核对后端超时与模型配置；本地处理仍可使用。"
    needs_audio = asset["metadata"].get("audio_stream_index") is not None
    binding = audio_urls().get(asset["id"], {})
    ready = (isinstance(binding, dict) and binding.get("asset_version") == asset["version"]
             and isinstance(binding.get("audio_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", binding["audio_sha256"])
             and isinstance(binding.get("url"), str) and bool(config.audio_hosts))
    return {"enabled": config.enabled, "configured": bool(config.api_key), "error": configuration_error,
            "asr_audio_ready": not needs_audio or bool(ready),
            "provider": "阿里云百炼（北京接入，实际推理范围以开通模型为准）",
            "models": {"asr": config.asr_model, "vision": config.vision_model, "embedding": config.embedding_model},
            "data_flow": "转写：提取音频；视觉：镜头缩略图；向量：对白及镜头描述文本。原片不会自动上传。服务留存期限须按账户协议核对。"}


def anchor(shot, manifest):
    video = manifest.get("video") or manifest.get("audio")
    return db.dumps({"start_pts": shot["start_pts"], "end_pts": shot["end_pts"],
                     "time_base": video["time_base"]})


def evidence_text(shot):
    evidence = shot.get("visual_evidence") or {}
    # A user correction replaces the descriptive candidate. Keep model
    # provenance visible, but do not silently reinsert rejected model labels.
    people = [] if shot.get("manual") else [person["description"] for person in evidence.get("people_candidates", [])]
    actions = [] if shot.get("manual") else evidence.get("actions", [])
    return unicodedata.normalize("NFKC", " ".join([
        shot.get("description") or "", *shot.get("tags", []), *people, *actions, shot.get("transcript") or ""]))


def manifest_view(c, asset_id, manifest):
    value = json.loads(db.dumps(manifest))
    edits = {row.anchor: row for row in c.execute(db.analysis_annotations.select().where(
        db.analysis_annotations.c.asset_id == asset_id, db.analysis_annotations.c.version == value["asset_version"]))}
    matched = set()
    base = f"/api/v1/assets/{asset_id}/analysis/files/{value['artifact_id']}"
    if value.get("proxy"):
        value["proxy"]["url"] = f"{base}/{value['proxy']['path']}"
    for shot in value.get("shots", []):
        key = anchor(shot, value)
        edit = edits.get(key)
        shot["model_description"] = shot.get("description")
        shot["tags"] = shot.get("tags", [])
        shot["manual"] = edit is not None
        if edit:
            matched.add(key)
            shot["description"], shot["tags"] = edit.description, json.loads(edit.tags_json)
        shot["thumbnail_url"] = f"{base}/{shot['thumbnail']}"
        # Raw vectors are internal evidence, not a browser API payload.
        shot.pop("embedding", None)
    value["unmatched_annotations"] = len(set(edits) - matched)
    return value


@router.get("/assets/{asset_id}/analysis")
def get_analysis(asset_id: str):
    with db.engine.connect() as c:
        asset = asset_for(c, asset_id)
        row = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset_id)).first()
        job = c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.asset_id == asset_id,
                        db.analysis_jobs.c.mode.in_(["local", "cloud"]))
                        .order_by(db.analysis_jobs.c.created_at.desc()).limit(1)).first()
        return {"asset_id": asset_id, "version": asset["version"], "revision": row.revision if row else 0,
                "manifest": manifest_view(c, asset_id, json.loads(row.manifest_json)) if row else None,
                "latest_job": public_job(job) if job else None, "cloud": cloud_info(asset)}


def enqueue(c, asset, mode, checkpoint=None):
    if c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.asset_id == asset["id"],
                    db.analysis_jobs.c.status.in_(["queued", "running"]))).first():
        raise AnalysisProblem("ANALYSIS_BUSY", "该素材已有分析任务，请等待或取消", 409)
    if mode == "cloud":
        info = cloud_info(asset)
        if not all(info[key] for key in ("enabled", "configured", "asr_audio_ready")):
            raise AnalysisProblem("CLOUD_NOT_READY", info.get("error") or "云分析尚未配置。可先生成本地镜头。", 422)
        if checkpoint is None:
            prepared = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset["id"])).first()
            if prepared:
                manifest = json.loads(prepared.manifest_json)
                if manifest["source_sha256"] == asset["sha256"] and manifest["asset_version"] == asset["version"]:
                    checkpoint = {"manifest": manifest, "steps": {"prepare": "succeeded"}}
    job_id = uuid.uuid4().hex
    c.execute(db.analysis_jobs.insert().values(id=job_id, asset_id=asset["id"], version=asset["version"],
        mode=mode, status="queued", attempt=0, progress=0, cancel_requested=0, error=None,
        result_json=db.dumps(checkpoint or {}), created_at=db.now(), updated_at=db.now()))
    return public_job(c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.id == job_id)).first())


@router.post("/assets/{asset_id}/analysis")
def submit_analysis(asset_id: str, request: AnalyzeRequest):
    body = {"asset_id": asset_id, **request.model_dump()}
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        key = "analysis:" + request.request_id
        previous = c.execute(db.receipts.select().where(db.receipts.c.id == key)).first()
        if previous:
            if previous.fingerprint != db.fingerprint(body):
                raise AnalysisProblem("IDEMPOTENCY_CONFLICT", "请求编号已用于不同分析内容", 409)
            return json.loads(previous.response_json)
        result = enqueue(c, asset_for(c, asset_id), request.mode)
        c.execute(db.receipts.insert().values(id=key, fingerprint=db.fingerprint(body), response_json=db.dumps(result)))
        return result


@router.get("/analysis-jobs")
def list_analysis_jobs():
    with db.engine.connect() as c:
        return {"jobs": [public_job(row) for row in c.execute(db.analysis_jobs.select()
                           .order_by(db.analysis_jobs.c.created_at.desc()).limit(100))]}


def get_job(c, job_id):
    row = c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.id == job_id)).first()
    if row is None:
        raise AnalysisProblem("NOT_FOUND", "分析任务不存在", 404)
    return row


@router.get("/analysis-jobs/{job_id}")
def analysis_job(job_id: str):
    with db.engine.connect() as c:
        return public_job(get_job(c, job_id))


@router.post("/analysis-jobs/{job_id}/cancel")
def cancel_analysis(job_id: str):
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = get_job(c, job_id)
        if row.status in ("queued", "running"):
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job_id).values(
                status="cancelled" if row.status == "queued" else "running", cancel_requested=1, updated_at=db.now()))
        return public_job(get_job(c, job_id))


@router.post("/analysis-jobs/{job_id}/retry")
def retry_analysis(job_id: str):
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = get_job(c, job_id)
        if row.status not in ("failed", "cancelled"):
            raise AnalysisProblem("NOT_RETRYABLE", "只有失败或取消的分析任务可重试", 409)
        checkpoint = json.loads(row.result_json or "{}")
        if checkpoint.get("asr_submitting") and not checkpoint.get("asr_task_id"):
            raise AnalysisProblem("ASR_SUBMISSION_UNKNOWN", "转写提交结果未知，可能已计费。请按任务编号核对百炼记录，禁止重新提交音频。", 409)
        asset = asset_for(c, row.asset_id)
        if row.mode == "local_asr":
            binding = checkpoint.get("transcription", {})
            if row.version != asset["version"] or binding.get("source_sha256") != asset["sha256"]:
                raise AnalysisProblem("ASSET_VERSION_CHANGED", "素材版本已变化，请新建当前素材的转写任务", 409)
        return enqueue(c, asset, row.mode, checkpoint)


@router.get("/assets/{asset_id}/analysis/files/{artifact_id}/{name}")
def analysis_file(asset_id: str, artifact_id: str, name: str):
    if not re.fullmatch(r"[a-f0-9]{32}-a[1-3]", artifact_id) or not re.fullmatch(r"proxy\.mp4|shot-\d{4}\.jpg", name):
        raise AnalysisProblem("NOT_FOUND", "分析文件不存在", 404)
    with db.engine.connect() as c:
        row = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset_id)).first()
        if row is None or json.loads(row.manifest_json).get("artifact_id") != artifact_id:
            raise AnalysisProblem("NOT_FOUND", "分析文件不存在或已更新", 404)
    path = db.DATA / "analysis" / artifact_id / name
    if not path.is_file():
        raise AnalysisProblem("NOT_FOUND", "分析文件缺失，请重新生成", 404)
    return FileResponse(path, media_type="video/mp4" if name.endswith(".mp4") else "image/jpeg")


@router.patch("/assets/{asset_id}/analysis/annotations")
def annotate(asset_id: str, request: AnnotationRequest):
    if any(not tag.strip() or len(tag) > 60 for tag in request.tags):
        raise AnalysisProblem("INVALID_TAG", "标签不能为空或超过 60 字", 422)
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        asset = asset_for(c, asset_id)
        row = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset_id)).first()
        if row is None or row.revision != request.expected_revision:
            raise AnalysisProblem("REVISION_CONFLICT", "分析已更新，请刷新结果后再保存修正", 409)
        manifest = json.loads(row.manifest_json)
        shot = next((s for s in manifest["shots"] if s["id"] == request.shot_id), None)
        if not shot:
            raise AnalysisProblem("NOT_FOUND", "镜头已变化，请重新选择", 404)
        key = anchor(shot, manifest)
        current = c.execute(db.analysis_annotations.select().where(db.analysis_annotations.c.asset_id == asset_id,
            db.analysis_annotations.c.version == asset["version"], db.analysis_annotations.c.anchor == key)).first()
        values = {"description": request.description.strip(), "tags_json": db.dumps(list(dict.fromkeys(request.tags))), "updated_at": db.now()}
        if current:
            c.execute(db.analysis_annotations.update().where(db.analysis_annotations.c.id == current.id).values(**values))
        else:
            c.execute(db.analysis_annotations.insert().values(id=uuid.uuid4().hex, asset_id=asset_id,
                      version=asset["version"], anchor=key, **values))
        c.execute(db.analyses.update().where(db.analyses.c.asset_id == asset_id).values(revision=row.revision + 1, updated_at=db.now()))
    return get_analysis(asset_id)


@router.get("/search")
def search(query: str = Query(min_length=1, max_length=200), asset_ids: str = Query(default="", max_length=3000),
           mode: Literal["keyword", "semantic"] = "keyword"):
    if not query.strip():
        raise AnalysisProblem("EMPTY_QUERY", "请输入对白、场景或标签", 422)
    ids = set(filter(None, asset_ids.split(",")))
    results = []
    with db.engine.connect() as c:
        for row in c.execute(db.analyses.select()):
            if ids and row.asset_id not in ids:
                continue
            raw = json.loads(row.manifest_json)
            view = manifest_view(c, row.asset_id, raw)
            for shot, original in zip(view.get("shots", []), raw.get("shots", [])):
                text = evidence_text(shot)
                score = 1.0 if unicodedata.normalize("NFKC", query.strip()).casefold() in text.casefold() else 0.0
                results.append({"asset_id": row.asset_id, "shot_id": shot["id"],
                    "start_pts": shot["start_pts"], "end_pts": shot["end_pts"], "time_base": raw["video"]["time_base"],
                    "description": shot.get("description"), "tags": shot.get("tags", []), "transcript": shot.get("transcript", ""),
                    "thumbnail_url": shot["thumbnail_url"], "score": score, "match_kind": mode,
                    "_vector": original.get("embedding") if not shot["manual"] else None,
                    "_model": raw.get("embedding_model")})
    warnings = []
    if mode == "semantic":
        from .model_services import BailianModelServices, ModelServiceConfig
        config = ModelServiceConfig.from_env()
        available = [result for result in results if result["_vector"] and result["_model"] == config.embedding_model]
        if not available:
            raise AnalysisProblem("SEMANTIC_NOT_READY", "尚无当前模型的语义索引。请先完成云分析；人工修正内容可用关键词搜索。", 422)
        from .model_services import ModelServiceError
        try:
            with BailianModelServices(config) as services:
                embedding = services.embed_texts([query]).vectors[0]
        except ModelServiceError:
            raise AnalysisProblem("MODEL_UNAVAILABLE", "语义模型暂不可用，请检查后端配置、额度或改用关键词搜索", 422) from None
        for result in available:
            vector = result["_vector"]
            norm = math.sqrt(sum(x*x for x in embedding) * sum(x*x for x in vector))
            result["score"] = sum(a*b for a, b in zip(embedding, vector)) / norm if norm else 0
        results = available
        warnings.append("语义检索会向百炼发送查询文本；人工修正的镜头仅参与关键词检索，避免使用过期向量。")
    results = sorted((r for r in results if r["score"] > 0), key=lambda r: r["score"], reverse=True)[:30]
    for result in results:
        result.pop("_vector"); result.pop("_model")
    return {"results": results, "mode": mode, "warnings": warnings}


def recover():
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        for row in c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.status == "running")):
            status = "cancelled" if row.cancel_requested else "failed" if row.attempt >= 3 else "queued"
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == row.id).values(status=status,
                error="分析进程多次中断，请检查环境后重试" if status == "failed" else None, updated_at=db.now()))


def claim():
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.status == "queued")
                        .order_by(db.analysis_jobs.c.created_at).limit(1)).first()
        if row is None:
            return None
        attempt = row.attempt + 1
        c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == row.id).values(
            status="running", attempt=attempt, error=None, updated_at=db.now()))
        return dict(row._mapping) | {"attempt": attempt, "status": "running"}


def process_job(job, stopped=lambda: False):
    if job["mode"] == "local_asr":
        from .transcription import process_job as process_transcription
        return process_transcription(job, stopped=stopped)
    from .preprocess import PreprocessCancelled, prepare_media
    from .model_services import ModelCallCancelled
    checkpoint = json.loads(job["result_json"] or "{}")
    checkpoint.setdefault("steps", {})

    def live(c):
        row = get_job(c, job["id"])
        return row.status == "running" and row.attempt == job["attempt"] and not row.cancel_requested and not stopped()

    def cancelled():
        with db.engine.connect() as c:
            return not live(c)

    def save(progress, *, completed=False):
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            row = get_job(c, job["id"])
            current_attempt = row.status == "running" and row.attempt == job["attempt"]
            if not current_attempt or (not completed and not live(c)):
                raise PreprocessCancelled()
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                result_json=db.dumps(checkpoint), progress=progress, updated_at=db.now()))

    try:
        with db.engine.connect() as c:
            asset = asset_for(c, job["asset_id"])
        with Path(asset["path"]).open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != asset["sha256"]:
                raise ValueError("原始媒体完整性校验失败")
        if not checkpoint.get("manifest"):
            token = f"{job['id']}-a{job['attempt']}"
            output = db.DATA / "analysis" / token
            output.parent.mkdir(exist_ok=True)
            last_progress = [-1]
            def preparing(value):
                percent = round(value * 30)
                if percent != last_progress[0]:
                    save(percent)
                    last_progress[0] = percent
            manifest = prepare_media(asset, output, cancelled=cancelled, progress=preparing)
            manifest["artifact_id"] = token
            if manifest.get("audio"):
                with (output / "audio.wav").open("rb") as audio_file:
                    manifest["audio"]["sha256"] = hashlib.file_digest(audio_file, "sha256").hexdigest()
            checkpoint["manifest"] = manifest
            checkpoint["steps"]["prepare"] = "succeeded"
            save(35 if job["mode"] == "cloud" else 95, completed=True)
        manifest = checkpoint["manifest"]
        if job["mode"] == "cloud":
            cloud_steps(asset, manifest, checkpoint, save, cancelled)
        else:
            checkpoint["steps"].update(asr="not_requested", vision="not_requested", embedding="not_requested")
        manifest["steps"] = checkpoint["steps"]
        manifest["analysis_state"] = "cloud_ready" if job["mode"] == "cloud" else "local_ready"
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            if not live(c):
                raise PreprocessCancelled()
            existing = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset["id"])).first()
            values = {"version": asset["version"], "revision": (existing.revision + 1) if existing else 1,
                      "manifest_json": db.dumps(manifest), "updated_at": db.now()}
            if existing:
                c.execute(db.analyses.update().where(db.analyses.c.asset_id == asset["id"]).values(**values))
            else:
                c.execute(db.analyses.insert().values(asset_id=asset["id"], **values))
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                status="succeeded", progress=100, result_json=db.dumps(checkpoint), updated_at=db.now()))
    except (PreprocessCancelled, ModelCallCancelled):
        with db.engine.begin() as c:
            row = get_job(c, job["id"])
            if row.attempt == job["attempt"] and row.status == "running":
                c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                    status="cancelled" if row.cancel_requested else "running", updated_at=db.now()))
    except Exception as exc:
        # Never expose provider bodies, private media URLs, keys or filesystem paths.
        unknown_asr = checkpoint.get("asr_submitting") and not checkpoint.get("asr_task_id")
        message = ("转写提交结果未知，可能已计费。请按任务编号核对百炼记录，禁止重复提交音频。" if unknown_asr else
                   "分析未完成；已完成步骤保留，可重试。请检查媒体、模型配置和额度。")
        with db.engine.begin() as c:
            row = get_job(c, job["id"])
            if row.attempt == job["attempt"] and row.status == "running":
                c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                    status="cancelled" if row.cancel_requested else "failed", updated_at=db.now(),
                    error=None if row.cancel_requested else message))
        logger.error("analysis_failed job=%s attempt=%s type=%s", job["id"], job["attempt"], type(exc).__name__)


def cloud_steps(asset, manifest, checkpoint, save, cancelled):
    from .model_services import BailianModelServices, ModelServiceConfig
    config = ModelServiceConfig.from_env()
    with BailianModelServices(config) as services:
        steps = checkpoint["steps"]
        calls = checkpoint.setdefault("calls", [])
        configuration = {"asr": config.asr_model, "vision": config.vision_model,
                         "embedding": config.embedding_model, "source_sha256": asset["sha256"],
                         "version": asset["version"], "prompt": "shot_description_v1",
                         "audio_sha256": (manifest.get("audio") or {}).get("sha256")}
        if checkpoint.get("configuration", configuration) != configuration:
            raise ValueError("模型或素材版本已变化，不能混用原任务结果，请新建分析任务")
        checkpoint["configuration"] = configuration
        # Guard before submitting audio: fail without charging a partial
        # over-budget analysis rather than transcribing first and rejecting later.
        if len(manifest["shots"]) > 20:
            raise ValueError("本轮云端验证最多 20 个镜头，请缩短素材；不静默漏分析")
        if manifest.get("audio") and steps.get("asr") != "succeeded":
            binding = audio_urls().get(asset["id"], {})
            if (not isinstance(binding, dict) or binding.get("asset_version") != asset["version"]
                    or not manifest["audio"].get("sha256") or binding.get("audio_sha256") != manifest["audio"]["sha256"]):
                raise ValueError("音频链接未绑定本次提取文件的 SHA-256 和素材版本")
            if not checkpoint.get("asr_task_id"):
                if checkpoint.get("asr_submitting"):
                    raise ValueError("转写提交结果未知，请人工核对厂商任务，避免重复付费")
                checkpoint["asr_submitting"] = True
                save(40)
                submission = services.submit_asr(binding["url"], cancelled=cancelled)
                checkpoint["asr_task_id"] = submission.task_id
                calls.append(submission.metadata.model_dump())
                save(42, completed=True)
            result = services.poll_asr(checkpoint["asr_task_id"], cancelled=cancelled)
            duration_ms = math.ceil(manifest["audio"]["sample_count"] * 1000 / manifest["audio"]["sample_rate"])
            if any(segment.end_ms > duration_ms + 1 for segment in result.segments):
                raise ValueError("转写时间超出提取音频，未保存错误源时间映射")
            manifest["transcript"] = {"status": "succeeded", "segments": [segment.model_dump() for segment in result.segments]}
            calls.append(result.metadata.model_dump())
            steps["asr"] = "succeeded"
            save(55, completed=True)
        elif not manifest.get("audio"):
            steps["asr"] = "no_audio"
        root = db.DATA / "analysis" / manifest["artifact_id"]
        completed = checkpoint.setdefault("described", [])
        for index, shot in enumerate(manifest["shots"]):
            if cancelled():
                from .preprocess import PreprocessCancelled
                raise PreprocessCancelled()
            if shot["id"] in completed:
                continue
            result = services.describe_shot((root / shot["thumbnail"]).read_bytes(), cancelled=cancelled)
            description = result.description.model_dump()
            shot.update(description=description["scene"], tags=description.get("tags", []),
                        visual_evidence=description, description_status="candidate")
            completed.append(shot["id"])
            calls.append(result.metadata.model_dump())
            save(55 + round(30 * (index + 1) / max(1, len(manifest["shots"]))), completed=True)
        steps["vision"] = "succeeded" if manifest["shots"] else "no_video"
        # ASR times are relative to the extracted audio stream, then mapped back
        # to source clocks rather than to the proxy's resampled frames.
        for shot in manifest["shots"]:
            video_tb = Fraction(manifest["video"]["time_base"]["num"], manifest["video"]["time_base"]["den"])
            start, end = int(shot["start_pts"]) * video_tb, int(shot["end_pts"]) * video_tb
            audio = manifest.get("audio")
            words = []
            precision = "word"
            if audio:
                origin = int(audio["start_pts"]) * Fraction(audio["time_base"]["num"], audio["time_base"]["den"])
                for segment in manifest["transcript"]["segments"]:
                    if segment["words"]:
                        words.extend(word["text"] + word.get("punctuation", "") for word in segment["words"]
                            if origin + Fraction(word["start_ms"], 1000) < end and origin + Fraction(word["end_ms"], 1000) > start)
                    elif origin + Fraction(segment["start_ms"], 1000) < end and origin + Fraction(segment["end_ms"], 1000) > start:
                        words.append(segment["text"])
                        precision = "sentence_overlap_candidate"
            shot["transcript"] = " ".join(words)
            shot["transcript_precision"] = precision
        if steps.get("embedding") != "succeeded" and manifest["shots"]:
            texts = [evidence_text(shot) or "未知" for shot in manifest["shots"]]
            vectors = checkpoint.setdefault("vectors", [])
            for offset in range(0, len(texts), 10):
                if len(vectors) >= min(len(texts), offset + 10):
                    continue
                result = services.embed_texts(texts[offset:offset+10], cancelled=cancelled)
                vectors.extend(result.vectors)
                calls.append(result.metadata.model_dump())
                save(90, completed=True)
            for shot, vector in zip(manifest["shots"], vectors):
                shot["embedding"] = vector
            manifest["embedding_model"] = config.embedding_model
            steps["embedding"] = "succeeded"
        elif not manifest["shots"]:
            steps["embedding"] = "no_video"
        manifest["calls"] = calls
        save(95)
