from copy import deepcopy
from fractions import Fraction

import pytest

from app.analysis import AnalysisProblem
from app.schema import Document
from app.subtitle_proposals import compile_subtitles


def fixture(origin=Fraction(0), fps=Fraction(25)):
    base = Fraction(1, 90000)
    in_pts = origin / base
    assert in_pts.denominator == 1
    doc = {"project_id": "p", "name": "test", "canvas": {"width": 640, "height": 360},
           "fps": {"num": fps.numerator, "den": fps.denominator}, "duration_frames": 600,
           "tracks": [{"id": "v", "kind": "video"}, {"id": "a", "kind": "audio"}, {"id": "s", "kind": "subtitle"}],
           "clips": [{"id": "c", "track_id": "v", "source": {"asset_id": "asset", "version": 1,
               "stream_index": 0, "time_base": {"num": 1, "den": 90000},
               "in_pts": str(in_pts.numerator), "out_pts": str(in_pts.numerator + 900000)},
               "timeline_start_frame": 30, "duration_frames": round(10 * fps)}], "subtitles": []}
    def clock(delta):
        time = origin + delta
        return {"num": time.numerator, "den": time.denominator}
    transcript = {"id": "t", "revision": 2, "binding": {"asset_id": "asset", "asset_version": 1}, "segments": [
        {"id": "one", "text": "已确认对白", "source_start": clock(Fraction(1)),
         "source_end": clock(Fraction(2)), "status": "confirmed", "human_confirmed": True}]}
    return Document.model_validate(doc).model_dump(), transcript


def test_exact_mapping_preserves_large_pts_and_untouched_media():
    doc, transcript = fixture(Fraction(9007199254740993, 90000), Fraction(30000, 1001))
    original = deepcopy(doc)
    result = compile_subtitles(doc, transcript, "c", ["one"])
    subtitle = result["document"]["subtitles"][0]
    assert (subtitle["start_frame"], subtitle["end_frame"]) == (60, 90)
    assert doc == original
    assert result["document"]["clips"][0]["source"] == original["clips"][0]["source"]
    assert subtitle["binding"] == {"asset_id": "asset", "asset_version": 1, "stream_index": 0,
        "transcript_id": "t", "transcript_revision": 2, "segment_id": "one", "review_required": False,
        "spans": [{"clip_id": "c", "start_frame": 30, "end_frame": 60}]}


def test_audio_offset_uses_common_source_clock_and_selected_copy_only():
    doc, transcript = fixture()
    doc["clips"][0]["track_id"] = "a"
    transcript["segments"][0]["source_start"] = {"num": 13, "den": 10}
    transcript["segments"][0]["source_end"] = {"num": 23, "den": 10}
    duplicate = deepcopy(doc["clips"][0])
    duplicate.update(id="copy", timeline_start_frame=300)
    doc["clips"].append(duplicate)
    result = compile_subtitles(doc, transcript, "copy", ["one"])
    sub = result["document"]["subtitles"][0]
    assert (sub["start_frame"], sub["end_frame"]) == (333, 358)
    assert result["document"]["clips"] == doc["clips"]


def test_reimport_is_idempotent_and_keeps_human_caption_changes():
    doc, transcript = fixture()
    first = compile_subtitles(doc, transcript, "c", ["one"])
    first["document"]["subtitles"][0]["text"] = "之后的人工改字"
    again = compile_subtitles(first["document"], transcript, "c", ["one"])
    assert not again["added_ids"] and again["skipped_count"] == 1
    assert again["document"]["subtitles"][0]["text"] == "之后的人工改字"


def test_legacy_independent_caption_retains_its_original_idempotency_anchor():
    doc, transcript = fixture()
    first = compile_subtitles(doc, transcript, "c", ["one"])
    caption = first["document"]["subtitles"][0]
    del caption["binding"]
    caption["text"] = "旧工程人工字幕"
    again = compile_subtitles(first["document"], transcript, "c", ["one"])
    assert again["added_ids"] == [] and again["skipped_count"] == 1
    assert again["document"]["subtitles"] == [caption]


def test_reimport_after_a_media_split_skips_provenance_before_partial_sentence_rejection():
    doc, transcript = fixture()
    imported = compile_subtitles(doc, transcript, "c", ["one"])["document"]
    left = imported["clips"][0]
    right = deepcopy(left)
    offset = 35
    left["duration_frames"] = offset
    left["source"]["out_pts"] = str(offset * 3600)
    right.update(id="new-right", timeline_start_frame=65, duration_frames=215)
    right["source"]["in_pts"] = str(offset * 3600)
    imported["clips"].append(right)
    caption = imported["subtitles"][0]
    caption["text"] = "保留拆片前的人工修改"
    caption["binding"]["spans"] = [
        {"clip_id": "c", "start_frame": 25, "end_frame": 35},
        {"clip_id": "new-right", "start_frame": 0, "end_frame": 15}]
    before = deepcopy(imported)
    # The original sentence begins before the new right clip's source in-point.
    # Existing provenance must protect it rather than reject or duplicate it.
    for clip_id in ("c", "new-right"):
        repeated = compile_subtitles(imported, transcript, clip_id, ["one"])
        assert repeated["added_ids"] == [] and repeated["skipped_count"] == 1
        assert repeated["document"] == before


def test_copied_project_reimport_uses_binding_instead_of_old_project_hash():
    doc, transcript = fixture()
    imported = compile_subtitles(doc, transcript, "c", ["one"])["document"]
    imported["project_id"] = "copied-project"
    imported["subtitles"][0]["text"] = "复制剧集中的独立人工修改"
    repeated = compile_subtitles(imported, transcript, "c", ["one"])
    assert repeated["added_ids"] == [] and repeated["skipped_count"] == 1
    assert repeated["document"] == imported


def test_same_source_used_again_has_its_own_caption_and_binding():
    doc, transcript = fixture()
    imported = compile_subtitles(doc, transcript, "c", ["one"])["document"]
    copy = deepcopy(imported["clips"][0])
    copy.update(id="second-instance", timeline_start_frame=300)
    imported["clips"].append(copy)
    imported["subtitles"][0]["text"] = "第一个实例的人工修改"
    result = compile_subtitles(imported, transcript, "second-instance", ["one"])
    assert len(result["added_ids"]) == 1 and result["skipped_count"] == 0
    captions = result["document"]["subtitles"]
    assert captions[0] == imported["subtitles"][0]
    assert captions[1]["text"] == "已确认对白"
    assert captions[1]["id"] != captions[0]["id"]
    assert captions[1]["binding"]["spans"] == [{"clip_id": "second-instance", "start_frame": 25, "end_frame": 50}]


@pytest.mark.parametrize("status,confirmed", [("pending", False), ("rejected", False), ("confirmed", False)])
def test_unconfirmed_content_is_never_imported(status, confirmed):
    doc, transcript = fixture()
    transcript["segments"][0].update(status=status, human_confirmed=confirmed)
    with pytest.raises(AnalysisProblem) as error:
        compile_subtitles(doc, transcript, "c", ["one"])
    assert error.value.code == "UNCONFIRMED_TRANSCRIPT"


def test_overlap_and_partial_sentence_fail_atomically_without_erasing_existing():
    doc, transcript = fixture()
    doc["subtitles"] = [{"id": "manual", "track_id": "s", "text": "保留", "start_frame": 55, "end_frame": 65}]
    before = deepcopy(doc)
    with pytest.raises(AnalysisProblem) as error:
        compile_subtitles(doc, transcript, "c", ["one"])
    assert error.value.code == "SUBTITLE_OVERLAP" and doc == before
    doc["subtitles"] = []
    transcript["segments"][0]["source_start"] = {"num": -1, "den": 1}
    with pytest.raises(AnalysisProblem) as error:
        compile_subtitles(doc, transcript, "c", ["one"])
    assert error.value.code == "PARTIAL_SENTENCE"


def test_outside_sentences_skip_and_wrong_asset_and_zero_frame_fail():
    doc, transcript = fixture()
    sentence = transcript["segments"][0]
    sentence.update(source_start={"num": 11, "den": 1}, source_end={"num": 12, "den": 1})
    assert compile_subtitles(doc, transcript, "c", ["one"])["outside_count"] == 1
    transcript["binding"]["asset_version"] = 2
    with pytest.raises(AnalysisProblem) as error:
        compile_subtitles(doc, transcript, "c", ["one"])
    assert error.value.code == "TRANSCRIPT_ASSET_MISMATCH"
    transcript["binding"]["asset_version"] = 1
    sentence.update(source_start={"num": 1, "den": 1000}, source_end={"num": 2, "den": 1000})
    with pytest.raises(AnalysisProblem) as error:
        compile_subtitles(doc, transcript, "c", ["one"])
    assert error.value.code == "SUB_FRAME_SENTENCE"
