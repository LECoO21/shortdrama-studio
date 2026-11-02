"""Compile reviewed source dialogue into a draft; the normal Document save is authoritative."""
import hashlib
import json
from copy import deepcopy
from fractions import Fraction

from fastapi import APIRouter
from pydantic import Field

from . import storage as db
from .analysis import AnalysisProblem
from .schema import Document, StrictModel

router = APIRouter(prefix="/api/v1")


class SubtitleProposalRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    transcript_id: str = Field(min_length=1, max_length=100)
    transcript_revision: int = Field(ge=1)
    clip_id: str = Field(min_length=1, max_length=100)
    segment_ids: list[str] = Field(min_length=1, max_length=500)


def _problem(code, message, status=422):
    raise AnalysisProblem(code, message, status)


def _fraction(value):
    if (not isinstance(value, dict) or type(value.get("num")) is not int
            or type(value.get("den")) is not int or value["den"] <= 0):
        _problem("TRANSCRIPT_CLOCK", "对白源时间无效，请重新读取转写结果")
    return Fraction(value["num"], value["den"])


def _nearest_frame(value):
    # All mapped offsets are nonnegative. Ties go to the later project frame.
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def compile_subtitles(document, transcript, clip_id, segment_ids):
    """Return a deterministic candidate without editing source, transcript or input Document."""
    document = Document.model_validate(document).model_dump()
    clip = next((c for c in document["clips"] if c["id"] == clip_id), None)
    if not clip:
        _problem("CLIP_NOT_FOUND", "所选片段已不存在，请重新选择", 409)
    source = clip["source"]
    binding = transcript["binding"]
    if source["asset_id"] != binding["asset_id"] or source["version"] != binding["asset_version"]:
        _problem("TRANSCRIPT_ASSET_MISMATCH", "对白与所选片段的素材或版本不一致")
    if not segment_ids or len(set(segment_ids)) != len(segment_ids):
        _problem("INVALID_SEGMENT_SELECTION", "请选择不重复的已确认句子")
    segments = {segment["id"]: segment for segment in transcript["segments"]}
    if any(key not in segments for key in segment_ids):
        _problem("SEGMENT_NOT_FOUND", "对白列表已变化，请重新读取结果", 409)
    selected = [segments[key] for key in segment_ids]
    if any(s.get("status") != "confirmed" or s.get("human_confirmed") is not True for s in selected):
        _problem("UNCONFIRMED_TRANSCRIPT", "只有已保存并确认的句子可以生成字幕")
    track = next((t for t in document["tracks"] if t["kind"] == "subtitle"), None)
    if track is None:
        _problem("SUBTITLE_TRACK_MISSING", "工程没有字幕轨道，请先添加字幕轨道")
    clock = Fraction(source["time_base"]["num"], source["time_base"]["den"])
    clip_start, clip_end = int(source["in_pts"]) * clock, int(source["out_pts"]) * clock
    fps = Fraction(document["fps"]["num"], document["fps"]["den"])
    next_document = deepcopy(document)
    added, skipped, outside = [], 0, 0
    existing_ids = {s["id"] for s in document["subtitles"]}
    linked_sentences = set()
    for old in document["subtitles"]:
        attached = old.get("binding")
        if attached and any(span["clip_id"] == clip_id for span in attached["spans"]):
            linked_sentences.add((attached.get("transcript_id"), attached.get("segment_id")))
    for segment in sorted(selected, key=lambda s: _fraction(s["source_start"])):
        start, end = _fraction(segment["source_start"]), _fraction(segment["source_end"])
        if end <= start:
            _problem("TRANSCRIPT_CLOCK", "对白结束时间须晚于开始时间")
        identity = json.dumps([document["project_id"], clip_id, transcript["id"], segment["id"]],
                              ensure_ascii=False, separators=(",", ":"))
        subtitle_id = "asr-" + hashlib.sha256(identity.encode()).hexdigest()[:28]
        if (transcript["id"], segment["id"]) in linked_sentences or subtitle_id in existing_ids:
            # Provenance follows split clip instances and copied projects. The
            # old ID remains a compatibility anchor for independent captions.
            # Check this before sentence-range rejection: an already imported
            # sentence can now span two media halves without being split itself.
            skipped += 1
            continue
        if end <= clip_start or start >= clip_end:
            outside += 1
            continue
        if start < clip_start or end > clip_end:
            _problem("PARTIAL_SENTENCE", "已确认句子跨出所选片段，请先调整片段切点或对白区间")
        text = segment["text"].strip()
        if not text or len(text) > 1000:
            _problem("INVALID_SUBTITLE_TEXT", "字幕文字不能为空或超过 1000 字")
        first = clip["timeline_start_frame"] + _nearest_frame((start - clip_start) * fps)
        last = clip["timeline_start_frame"] + _nearest_frame((end - clip_start) * fps)
        first = max(clip["timeline_start_frame"], first)
        last = min(clip["timeline_start_frame"] + clip["duration_frames"], last)
        if first >= last:
            _problem("SUB_FRAME_SENTENCE", "对白区间短于一个工程帧，请校对时间后重试")
        if any(first < old["end_frame"] and old["start_frame"] < last for old in next_document["subtitles"]):
            _problem("SUBTITLE_OVERLAP", "已有字幕与待加入句子重叠；请先调整区间，原字幕未被覆盖")
        next_document["subtitles"].append({"id": subtitle_id, "track_id": track["id"],
            "text": text, "start_frame": first, "end_frame": last,
            "font_size": 28, "color": "#FFFFFF", "bottom_percent": 8.0,
            "binding": {"asset_id": source["asset_id"], "asset_version": source["version"],
                "stream_index": source["stream_index"], "transcript_id": transcript["id"],
                "transcript_revision": transcript["revision"], "segment_id": segment["id"],
                "review_required": False, "spans": [{"clip_id": clip_id,
                    "start_frame": first - clip["timeline_start_frame"],
                    "end_frame": last - clip["timeline_start_frame"]}]}})
        added.append(subtitle_id)
    if len(next_document["subtitles"]) > 500:
        _problem("SUBTITLE_LIMIT", "当前工程最多支持 500 条字幕")
    next_document["subtitles"].sort(key=lambda s: (s["start_frame"], s["end_frame"], s["id"]))
    return {"document": Document.model_validate(next_document).model_dump(), "added_ids": added,
            "skipped_count": skipped, "outside_count": outside}


@router.post("/projects/{project_id}/subtitle-proposals")
def propose_subtitles(project_id: str, body: SubtitleProposalRequest):
    from .transcription import get_transcript, public_transcript
    with db.engine.connect() as c:
        # Read project, transcript and assets from one SQLite snapshot.
        c.exec_driver_sql("BEGIN")
        row = c.execute(db.projects.select().where(db.projects.c.id == project_id)).first()
        if row is None:
            _problem("NOT_FOUND", "工程不存在", 404)
        if row.revision != body.expected_revision:
            _problem("REVISION_CONFLICT", "工程已变化，请保存并重新加入字幕", 409)
        transcript_row = get_transcript(c, body.transcript_id)
        if transcript_row.revision != body.transcript_revision:
            _problem("TRANSCRIPT_REVISION_CONFLICT", "对白校对已更新，请刷新后重试", 409)
        transcript = public_transcript(transcript_row)
        binding = transcript["binding"]
        asset = db.asset_map(c).get(binding["asset_id"])
        if (asset is None or asset["version"] != binding["asset_version"]
                or asset["sha256"] != binding["source_sha256"]):
            _problem("TRANSCRIPT_ASSET_MISMATCH", "素材或版本已变化，不能使用旧对白")
        result = compile_subtitles(json.loads(row.document_json), transcript, body.clip_id, body.segment_ids)
        return {"project_id": project_id, "base_revision": row.revision,
                "transcript_id": transcript_row.id, "transcript_revision": transcript_row.revision,
                "clip_id": body.clip_id, **result}
