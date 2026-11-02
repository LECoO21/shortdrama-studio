"""Bound-caption geometry is part of the existing immutable Document contract."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schema import Document, SaveRequest
from test_subtitle_proposals import fixture


def linked_document():
    doc, _ = fixture()
    doc["subtitles"] = [{"id": "caption", "track_id": "s", "text": "一条完整字幕",
        "start_frame": 55, "end_frame": 80, "font_size": 28, "color": "#FFFFFF", "bottom_percent": 8.0,
        "binding": {"asset_id": "asset", "asset_version": 1, "stream_index": 0,
            "transcript_id": "t", "transcript_revision": 2, "segment_id": "one", "review_required": False,
            "spans": [{"clip_id": "c", "start_frame": 25, "end_frame": 50}]}}]
    return doc


def split_document():
    doc = linked_document()
    left = doc["clips"][0]
    right = deepcopy(left)
    left["duration_frames"] = 35
    left["source"]["out_pts"] = "126000"
    right.update(id="right", timeline_start_frame=65, duration_frames=215)
    right["source"]["in_pts"] = "126000"
    doc["clips"].append(right)
    doc["subtitles"][0]["binding"]["spans"] = [
        {"clip_id": "c", "start_frame": 25, "end_frame": 35},
        {"clip_id": "right", "start_frame": 0, "end_frame": 15}]
    return doc


def test_old_independent_subtitles_do_not_gain_null_fields_or_change_on_roundtrip():
    old = linked_document()
    del old["subtitles"][0]["binding"]
    before = deepcopy(old)
    assert Document.model_validate(old).model_dump() == before
    old["subtitles"][0]["binding"] = None
    assert Document.model_validate(old).model_dump() == before
    assert Document.model_validate_json(Document.model_validate(old).model_dump_json()).model_dump() == before


def test_bound_caption_survives_nested_save_and_json_roundtrip_without_converting_pts():
    doc = split_document()
    # Binding geometry uses local frames; very large source integers stay exact.
    origin = 9007199254740993
    for clip in doc["clips"]:
        clip["source"]["in_pts"] = str(origin + int(clip["source"]["in_pts"]))
        clip["source"]["out_pts"] = str(origin + int(clip["source"]["out_pts"]))
    doc["subtitles"][0]["binding"]["review_required"] = True
    before = deepcopy(doc)
    saved = SaveRequest.model_validate({"document": doc, "expected_revision": 3, "request_id": "save-request"})
    assert saved.model_dump()["document"] == before
    assert SaveRequest.model_validate_json(saved.model_dump_json()).model_dump()["document"] == before
    assert doc == before


def test_manual_binding_has_no_invented_transcript_provenance():
    doc = linked_document()
    binding = doc["subtitles"][0]["binding"]
    for key in ("transcript_id", "transcript_revision", "segment_id"):
        del binding[key]
    result = Document.model_validate(doc).model_dump()
    assert result == doc
    assert set(result["subtitles"][0]["binding"]) == {
        "asset_id", "asset_version", "stream_index", "review_required", "spans"}


@pytest.mark.parametrize("patch", [
    {"asset_id": "another-asset"}, {"asset_version": 2}, {"stream_index": 1},
    {"asset_version": 0}, {"stream_index": -1}, {"review_required": "yes"},
    {"arbitrary": "unsupported"},
])
def test_binding_identity_and_strict_fields_are_validated(patch):
    doc = linked_document()
    doc["subtitles"][0]["binding"].update(patch)
    with pytest.raises(ValidationError):
        Document.model_validate(doc)


@pytest.mark.parametrize("spans", [
    [], [{"clip_id": "missing", "start_frame": 25, "end_frame": 50}],
    [{"clip_id": "c", "start_frame": -1, "end_frame": 50}],
    [{"clip_id": "c", "start_frame": 25, "end_frame": 25}],
    [{"clip_id": "c", "start_frame": 25, "end_frame": 251}],
    [{"clip_id": "c", "start_frame": 24, "end_frame": 50}],
    [{"clip_id": "c", "start_frame": 25, "end_frame": 49}],
    [{"clip_id": "c", "start_frame": 25.5, "end_frame": 50}],
    [{"clip_id": "c", "start_frame": 25, "end_frame": 35},
     {"clip_id": "c", "start_frame": 35, "end_frame": 50}],
    [{"clip_id": "c", "start_frame": 25, "end_frame": 50}] * 201,
])
def test_invalid_or_ambiguous_span_geometry_is_rejected(spans):
    doc = linked_document()
    doc["subtitles"][0]["binding"]["spans"] = spans
    with pytest.raises(ValidationError):
        Document.model_validate(doc)


@pytest.mark.parametrize("missing", ["transcript_id", "transcript_revision", "segment_id"])
def test_incomplete_transcript_provenance_is_rejected(missing):
    doc = linked_document()
    del doc["subtitles"][0]["binding"][missing]
    with pytest.raises(ValidationError, match="完整"):
        Document.model_validate(doc)


@pytest.mark.parametrize("change", ["gap", "overlap", "order", "clock", "kind"])
def test_split_spans_cannot_be_made_disjoint_reordered_or_mixed(change):
    doc = split_document()
    right = doc["clips"][1]
    if change == "gap":
        right["timeline_start_frame"] += 1
    elif change == "overlap":
        right["timeline_start_frame"] -= 1
    elif change == "order":
        doc["subtitles"][0]["binding"]["spans"].reverse()
    elif change == "clock":
        right["source"]["time_base"] = {"num": 1, "den": 48000}
    elif change == "kind":
        right["track_id"] = "a"
    with pytest.raises(ValidationError):
        Document.model_validate(doc)


def test_an_audio_caption_is_valid_but_does_not_claim_another_instance():
    doc = linked_document()
    doc["clips"][0]["track_id"] = "a"
    assert Document.model_validate(doc).model_dump() == doc
    other = deepcopy(doc["clips"][0])
    other.update(id="same-source-second-use", timeline_start_frame=300)
    doc["clips"].append(other)
    assert Document.model_validate(doc).model_dump() == doc
    # Pointing at the other instance without changing the caption's times is invalid.
    doc["subtitles"][0]["binding"]["spans"][0]["clip_id"] = other["id"]
    with pytest.raises(ValidationError):
        Document.model_validate(doc)
