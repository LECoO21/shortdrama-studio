"""Durable local transcript candidates. A reviewed sentence is not global ASR QA."""
from __future__ import annotations

from fractions import Fraction
import json
import logging
import os
from pathlib import Path
import re
from typing import Literal
import uuid

from fastapi import APIRouter, Query
from pydantic import Field, model_validator

from . import storage as db
from .analysis import AnalysisProblem, asset_for, enqueue, get_job, public_job
from .local_asr import (BoundAudio, DEFAULT_ROOT, LocalASRError, WHISPER_VERSION,
                        local_model_spec, run_local_asr, sha256)
from .schema import StrictModel

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger("studio.transcription")
ASR_ROOT = DEFAULT_ROOT


class TranscribeRequest(StrictModel):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    model: Literal["small", "medium"] = "medium"
    language: Literal["zh", "auto"] = "zh"


class CorrectSegmentRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, gt=0)
    status: Literal["pending", "confirmed", "rejected"] | None = None

    @model_validator(mode="after")
    def changes(self):
        changes = self.model_fields_set - {"expected_revision"}
        if not changes or any(getattr(self, key) is None for key in changes):
            raise ValueError("至少提交一项文字、时间或审核状态，不能使用空值")
        if self.text is not None and not self.text.strip():
            raise ValueError("文字不能为空")
        return self


def local_models():
    result = []
    binary = ASR_ROOT / f"build-v{WHISPER_VERSION}/bin/whisper-cli"
    for model in ("small", "medium"):
        specification = local_model_spec(model)
        path = ASR_ROOT / "models" / specification["filename"]
        try:
            ready = binary.is_file() and os.access(binary, os.X_OK) and path.is_file() and path.stat().st_size == specification["bytes"]
        except OSError:
            ready = False
        result.append({"id": model, "ready": ready, **({} if ready else {
            "reason": "本机执行器或对应多语权重未准备；此页面不会下载模型。"})})
    return result


def get_transcript(c, transcript_id):
    row = c.execute(db.transcripts.select().where(db.transcripts.c.id == transcript_id)).first()
    if row is None:
        raise AnalysisProblem("NOT_FOUND", "逐字稿不存在", 404)
    return row


def _fraction(value: Fraction):
    return {"num": value.numerator, "den": value.denominator}


def public_transcript(row):
    raw = json.loads(row.raw_json)
    corrections = json.loads(row.corrections_json)
    binding = raw["binding"]
    origin = Fraction(binding["start_pts"] * binding["time_base_num"], binding["time_base_den"])
    maximum_ms = binding["sample_count"] * 1000 // binding["sample_rate"]
    segments = []
    for original in raw["segments"]:
        correction = corrections.get(original["id"], {})
        text = correction.get("text", original["text"])
        start_ms = correction.get("start_ms", original["start_ms"])
        # Retain raw values but keep the effective interval within actual PCM.
        end_ms = correction.get("end_ms", min(original["end_ms"], maximum_ms))
        status = correction.get("status", "pending")
        changed = text != original["text"] or start_ms != original["start_ms"] or end_ms != original["end_ms"]
        uncertainty = list(original.get("uncertainty", []))
        if changed and correction:
            uncertainty.append("raw_token_alignment_not_updated_after_manual_edit")
        elif changed:
            uncertainty.append("effective_end_clamped_to_actual_pcm")
        segments.append({**original, "text": text, "start_ms": start_ms, "end_ms": end_ms,
            "raw_text": original["text"], "raw_start_ms": original.get("raw_start_ms", original["start_ms"]),
            "raw_end_ms": original.get("raw_end_ms", original["end_ms"]),
            "source_start": _fraction(origin + Fraction(start_ms, 1000)),
            "source_end": _fraction(origin + Fraction(end_ms, 1000)),
            "status": status, "manual": bool(correction), "human_confirmed": status == "confirmed",
            "token_alignment_stale": changed, "uncertainty": sorted(set(uncertainty))})
    return {"id": row.id, "asset_id": row.asset_id, "version": row.version,
        "generation": row.generation, "revision": row.revision, "model": row.model,
        "binding": {**binding, "start_pts": str(binding["start_pts"])}, "config": raw.get("config", {}), "segments": segments,
        "warnings": raw.get("warnings", []), "quality_passed": False, "human_confirmed": False,
        "confirmed_count": sum(s["status"] == "confirmed" for s in segments),
        "created_at": row.created_at, "updated_at": row.updated_at}


@router.get("/assets/{asset_id}/transcription")
def get_asset_transcription(asset_id: str, transcript_id: str | None = Query(default=None, max_length=100)):
    with db.engine.connect() as c:
        asset = asset_for(c, asset_id)
        rows = list(c.execute(db.transcripts.select().where(db.transcripts.c.asset_id == asset_id)
            .order_by(db.transcripts.c.created_at.desc(), db.transcripts.c.generation.desc()).limit(100)))
        if transcript_id:
            selected = get_transcript(c, transcript_id)
            if selected.asset_id != asset_id:
                raise AnalysisProblem("NOT_FOUND", "该逐字稿不属于此素材", 404)
        else:
            selected = next((row for row in rows if row.version == asset["version"]), None)
        history = []
        for row in rows:
            view = public_transcript(row)
            history.append({key: view[key] for key in ("id", "version", "generation", "revision", "model", "created_at", "confirmed_count")} |
                           {"segment_count": len(view["segments"])})
        job = c.execute(db.analysis_jobs.select().where(db.analysis_jobs.c.asset_id == asset_id,
            db.analysis_jobs.c.version == asset["version"], db.analysis_jobs.c.mode == "local_asr")
            .order_by(db.analysis_jobs.c.created_at.desc()).limit(1)).first()
        return {"asset_id": asset_id, "version": asset["version"],
            "current": public_transcript(selected) if selected else None, "history": history,
            "latest_job": public_job(job) if job else None, "local_models": local_models()}


def _matching_manifest(manifest, asset):
    return (isinstance(manifest, dict) and manifest.get("schema_version") == 1
        and manifest.get("asset_id") == asset["id"] and manifest.get("asset_version") == asset["version"]
        and manifest.get("source_sha256") == asset["sha256"]
        and isinstance(manifest.get("artifact_id"), str)
        and re.fullmatch(r"[a-f0-9]{32}-a[1-3]", manifest["artifact_id"]))


def _prepared(c, asset):
    row = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset["id"])).first()
    candidates = [json.loads(row.manifest_json)] if row else []
    # A retained older analysis version must not force repeated preparation for
    # the new version; completed preparation also lives in durable ASR jobs.
    candidates.extend(json.loads(job.result_json or "{}").get("manifest") for job in c.execute(
        db.analysis_jobs.select().where(db.analysis_jobs.c.asset_id == asset["id"],
            db.analysis_jobs.c.version == asset["version"], db.analysis_jobs.c.mode == "local_asr",
            db.analysis_jobs.c.status.in_(["succeeded", "failed", "cancelled"]))
        .order_by(db.analysis_jobs.c.updated_at.desc()).limit(10)))
    for manifest in candidates:
        if _matching_manifest(manifest, asset):
            path = db.DATA / "analysis" / manifest["artifact_id"] / "audio.wav"
            if path.is_file() and path.resolve() == path:
                return manifest
    return None


@router.post("/assets/{asset_id}/transcription")
def submit_transcription(asset_id: str, request: TranscribeRequest):
    body = {"asset_id": asset_id, **request.model_dump()}
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        key = "transcription:" + request.request_id
        previous = c.execute(db.receipts.select().where(db.receipts.c.id == key)).first()
        if previous:
            if previous.fingerprint != db.fingerprint(body):
                raise AnalysisProblem("IDEMPOTENCY_CONFLICT", "请求编号已用于不同转写内容", 409)
            return json.loads(previous.response_json)
        asset = asset_for(c, asset_id)
        if asset["metadata"].get("audio_stream_index") is None:
            raise AnalysisProblem("NO_AUDIO", "素材没有可用音轨，不能生成对白候选", 422)
        if not next(m["ready"] for m in local_models() if m["id"] == request.model):
            raise AnalysisProblem("LOCAL_MODEL_NOT_READY", "对应本地模型尚未准备；未排队、未向云端发送素材", 422)
        specification = local_model_spec(request.model)
        checkpoint = {"steps": {}, "transcription": {"model": request.model, "language": request.language,
            "asset_id": asset["id"], "asset_version": asset["version"], "source_sha256": asset["sha256"],
            "model_sha256": specification["sha256"], "model_bytes": specification["bytes"],
            "model_revision": specification["revision"]}}
        manifest = _prepared(c, asset)
        if manifest:
            checkpoint.update(manifest=manifest)
            checkpoint["steps"]["prepare"] = "succeeded"
        result = enqueue(c, asset, "local_asr", checkpoint)
        c.execute(db.receipts.insert().values(id=key, fingerprint=db.fingerprint(body), response_json=db.dumps(result)))
        return result


@router.patch("/transcripts/{transcript_id}/segments/{segment_id}")
def correct_segment(transcript_id: str, segment_id: str, request: CorrectSegmentRequest):
    with db.engine.begin() as c:
        c.exec_driver_sql("BEGIN IMMEDIATE")
        row = get_transcript(c, transcript_id)
        asset = asset_for(c, row.asset_id)
        raw = json.loads(row.raw_json)
        if row.version != asset["version"] or raw["binding"]["source_sha256"] != asset["sha256"]:
            raise AnalysisProblem("ASSET_VERSION_CHANGED", "素材版本已变化，请查看当前版本的逐字稿", 409)
        if row.revision != request.expected_revision:
            raise AnalysisProblem("REVISION_CONFLICT", "逐字稿已更新，请刷新后再保存", 409)
        current = public_transcript(row)
        segment = next((s for s in current["segments"] if s["id"] == segment_id), None)
        if segment is None:
            raise AnalysisProblem("NOT_FOUND", "逐字稿句子不存在", 404)
        corrections = json.loads(row.corrections_json)
        edit = dict(corrections.get(segment_id, {}))
        changes = request.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if "text" in changes:
            changes["text"] = changes["text"].strip()
        edit.update(changes)
        if "status" not in changes and set(changes) & {"text", "start_ms", "end_ms"}:
            edit["status"] = "pending"
        start, end = edit.get("start_ms", segment["start_ms"]), edit.get("end_ms", segment["end_ms"])
        binding = raw["binding"]
        if not 0 <= start < end or Fraction(end, 1000) > Fraction(binding["sample_count"], binding["sample_rate"]):
            raise AnalysisProblem("TRANSCRIPT_RANGE", "对白区间须在原音轨范围内，结束须晚于开始", 422)
        edit["updated_at"] = db.now()
        corrections[segment_id] = edit
        c.execute(db.transcripts.update().where(db.transcripts.c.id == row.id,
            db.transcripts.c.revision == request.expected_revision).values(
                revision=row.revision + 1, corrections_json=db.dumps(corrections), updated_at=db.now()))
        return public_transcript(get_transcript(c, transcript_id))


def _bound_audio(asset, manifest):
    if not _matching_manifest(manifest, asset) or not isinstance(manifest.get("audio"), dict):
        raise LocalASRError("audio_unavailable", "未取得当前素材版本的音轨")
    audio = manifest["audio"]
    stream = next((s for s in asset["metadata"]["streams"] if s["index"] == asset["metadata"].get("audio_stream_index")), None)
    if (not stream or stream["type"] != "audio" or audio.get("path") != "audio.wav"
            or audio.get("source_stream_index") != stream["index"] or audio.get("time_base") != stream["time_base"]
            or audio.get("start_pts") != stream["start_pts"]):
        raise LocalASRError("audio_binding_mismatch", "音轨源流与素材时基不一致")
    path = db.DATA / "analysis" / manifest["artifact_id"] / "audio.wav"
    if path.resolve() != path:
        raise LocalASRError("audio_binding_mismatch", "音轨缓存路径无效")
    bound = BoundAudio(path, audio["sha256"], asset["sha256"], asset["id"], asset["version"],
        int(audio["start_pts"]), audio["time_base"]["num"], audio["time_base"]["den"],
        audio["sample_rate"], audio["sample_count"])
    bound.validate()
    return bound


def _recognize(bound, configuration, cancelled):
    state = run_local_asr(bound, root=ASR_ROOT, model_size=configuration["model"],
        language=configuration["language"], cancelled=cancelled)
    fingerprint = state.get("fingerprint", "")
    if state.get("state") != "succeeded" or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise LocalASRError("invalid_output", "本地识别未产生可校验候选")
    path = ASR_ROOT.resolve() / "runs" / fingerprint / "result.json"
    if path.resolve() != path or path.stat().st_size > 16 * 1024 * 1024 or sha256(path) != state.get("result_sha256"):
        raise LocalASRError("invalid_output", "本地识别结果完整性校验失败")
    raw = json.loads(path.read_text())
    if (raw.get("binding") != bound.binding() or raw.get("config", {}).get("model_sha256") != configuration["model_sha256"]
            or raw.get("config", {}).get("model") != configuration["model"] + "-multilingual"):
        raise LocalASRError("invalid_output", "本地识别结果不属于当前音轨和模型")
    raw["quality_passed"], raw["human_confirmed"] = False, False
    return raw


def process_job(job, stopped=lambda: False):
    from .preprocess import PreprocessCancelled, prepare_media
    checkpoint = json.loads(job["result_json"] or "{}")
    checkpoint.setdefault("steps", {})

    def live(c):
        row = get_job(c, job["id"])
        return row.status == "running" and row.attempt == job["attempt"] and not row.cancel_requested and not stopped()

    def cancelled():
        with db.engine.connect() as c:
            return not live(c)

    def save(progress):
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            if not live(c):
                raise PreprocessCancelled()
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                progress=progress, result_json=db.dumps(checkpoint), updated_at=db.now()))

    try:
        with db.engine.connect() as c:
            asset = asset_for(c, job["asset_id"])
        configuration = checkpoint["transcription"]
        specification = local_model_spec(configuration["model"])
        if (asset["version"] != job["version"] or configuration["asset_id"] != asset["id"]
                or configuration["asset_version"] != asset["version"] or configuration["source_sha256"] != asset["sha256"]
                or configuration["model_sha256"] != specification["sha256"] or configuration["model_bytes"] != specification["bytes"]
                or configuration["model_revision"] != specification["revision"]):
            raise LocalASRError("binding_changed", "素材或模型绑定已变化，请新建转写任务")
        if sha256(Path(asset["path"])) != asset["sha256"]:
            raise LocalASRError("source_changed", "原始媒体完整性校验失败")
        if not checkpoint.get("manifest"):
            token = f"{job['id']}-a{job['attempt']}"
            output = db.DATA / "analysis" / token
            output.parent.mkdir(exist_ok=True)
            manifest = prepare_media(asset, output, cancelled=cancelled, progress=lambda value: save(round(value * 30)))
            manifest["artifact_id"] = token
            if not manifest.get("audio"):
                raise LocalASRError("no_audio", "素材没有可用音轨")
            manifest["audio"]["sha256"] = sha256(output / "audio.wav")
            checkpoint["manifest"] = manifest
            checkpoint["steps"]["prepare"] = "succeeded"
            save(35)
        bound = _bound_audio(asset, checkpoint["manifest"])
        if checkpoint.get("audio_binding", bound.binding()) != bound.binding():
            raise LocalASRError("audio_binding_changed", "音轨绑定已变化，请新建转写任务")
        checkpoint["audio_binding"] = bound.binding()
        checkpoint["steps"]["asr"] = "running"
        save(45)
        raw = _recognize(bound, configuration, cancelled)
        if raw["binding"] != bound.binding():
            raise LocalASRError("invalid_output", "识别结果与音轨来源不一致")
        checkpoint["steps"]["asr"] = "candidate_requires_review"
        with db.engine.begin() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            if not live(c):
                raise PreprocessCancelled()
            # Asset changes during inference cannot publish a stale candidate.
            current = asset_for(c, asset["id"])
            if current["version"] != asset["version"] or current["sha256"] != asset["sha256"]:
                raise LocalASRError("binding_changed", "素材已变化，识别候选未发布")
            existing_analysis = c.execute(db.analyses.select().where(db.analyses.c.asset_id == asset["id"])).first()
            if existing_analysis is None:
                # Publish only the missing preparation view. Transcript evidence
                # remains in its own immutable generation and correction store.
                manifest = json.loads(db.dumps(checkpoint["manifest"]))
                manifest["steps"] = {"prepare": "succeeded", "asr": "not_requested",
                    "vision": "not_requested", "embedding": "not_requested"}
                manifest["analysis_state"] = "local_ready"
                c.execute(db.analyses.insert().values(asset_id=asset["id"], version=asset["version"], revision=1,
                    manifest_json=db.dumps(manifest), updated_at=db.now()))
                checkpoint["steps"]["analysis_publication"] = "created_local_preparation"
            elif existing_analysis.version != asset["version"]:
                checkpoint["steps"]["analysis_publication"] = "retained_other_version"
                raw.setdefault("warnings", []).append("existing_analysis_other_version_retained")
            else:
                checkpoint["steps"]["analysis_publication"] = "existing_analysis_preserved"
            latest = c.execute(db.transcripts.select().where(db.transcripts.c.asset_id == asset["id"],
                db.transcripts.c.version == asset["version"]).order_by(db.transcripts.c.generation.desc()).limit(1)).first()
            transcript_id = uuid.uuid4().hex
            c.execute(db.transcripts.insert().values(id=transcript_id, asset_id=asset["id"], version=asset["version"],
                generation=latest.generation + 1 if latest else 1, revision=1, job_id=job["id"], model=configuration["model"],
                raw_json=db.dumps(raw), corrections_json="{}", created_at=db.now(), updated_at=db.now()))
            checkpoint["transcript_id"] = transcript_id
            c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                status="succeeded", progress=100, result_json=db.dumps(checkpoint), error=None, updated_at=db.now()))
        logger.info("local_transcript_done job=%s attempt=%s", job["id"], job["attempt"])
    except (PreprocessCancelled, LocalASRError) as exc:
        is_cancel = isinstance(exc, PreprocessCancelled) or exc.code == "cancelled"
        with db.engine.begin() as c:
            row = get_job(c, job["id"])
            if row.status == "running" and row.attempt == job["attempt"]:
                status = "cancelled" if row.cancel_requested else "running" if is_cancel else "failed"
                c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                    status=status, updated_at=db.now(), error=None if is_cancel or row.cancel_requested else str(exc)))
        if not is_cancel:
            logger.error("local_transcript_failed job=%s code=%s", job["id"], exc.code)
    except Exception as exc:
        with db.engine.begin() as c:
            row = get_job(c, job["id"])
            if row.status == "running" and row.attempt == job["attempt"]:
                c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == job["id"]).values(
                    status="cancelled" if row.cancel_requested else "failed", updated_at=db.now(),
                    error=None if row.cancel_requested else "本地转写未完成；旧逐字稿和人工修正保留，可按任务重试。"))
        logger.error("local_transcript_failed job=%s type=%s", job["id"], type(exc).__name__)
