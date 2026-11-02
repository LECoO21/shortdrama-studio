"""Local fixtures only: no model download, cloud calls, or product data access."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import importlib.util
import json
from pathlib import Path
import stat
import sys
import time
import wave

import pytest

import app.local_asr as module
from app.local_asr import BoundAudio, LocalASRError, parse_whisper_json, run_local_asr, sha256


def audio(tmp_path, *, origin=Fraction(0), exact_silence=False):
    path = tmp_path / "fixture.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes((b"\0\0" if exact_silence else b"\1\0") * 32000)
    digest = sha256(path)
    return BoundAudio(path, digest, digest, "fixture", 1, origin.numerator, 1,
                     origin.denominator, 16000, 32000)


def output():
    return {"result": {"language": "zh"}, "transcription": [{
        "offsets": {"from": 100, "to": 1900}, "text": " 你好，回来。 ", "tokens": [
            {"text": "[_BEG_]", "p": 0.9},
            {"text": "你好", "p": 0.95, "offsets": {"from": 100, "to": 800}},
            {"text": "回来", "p": 0.3, "offsets": {"from": 900, "to": 1900}}]}]}


def prepare_fake(tmp_path, monkeypatch, *, sleep=0):
    root = tmp_path / "private"
    binary = root / f"build-v{module.WHISPER_VERSION}/bin/whisper-cli"
    binary.parent.mkdir(parents=True)
    binary.write_text(f"#!{Path(sys.executable).resolve()}\nimport json,pathlib,sys,time\ntime.sleep({sleep})\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--output-file')+1]+'.json')\n"
        f"out.write_text(json.dumps({output()!r},ensure_ascii=False))\n")
    binary.chmod(0o700)
    model = root / "models/ggml-small.bin"
    model.parent.mkdir()
    model.write_bytes(b"synthetic-no-model")
    monkeypatch.setattr(module, "SMALL_BYTES", model.stat().st_size)
    monkeypatch.setattr(module, "SMALL_SHA256", sha256(model))
    return root


def test_exact_origin_mapping_keeps_tokens_estimated_and_unconfirmed(tmp_path):
    bound = audio(tmp_path, origin=Fraction(-1, 3))
    parsed = parse_whisper_json(output(), bound)
    segment = parsed["segments"][0]
    assert segment["source_start"] == {"num": -7, "den": 30}
    assert segment["source_end"] == {"num": 47, "den": 30}
    assert segment["text"] == "你好，回来。"
    assert len(segment["tokens"]) == 2
    assert segment["tokens"][0]["timestamp_precision"] == "estimated_token"
    assert segment["tokens"][1]["decoder_probability"] == 0.3
    assert "low_decoder_probability" in segment["uncertainty"]
    assert "token_probability_is_not_transcript_accuracy" in parsed["warnings"]
    assert parsed["quality_passed"] is False and parsed["human_confirmed"] is False


@pytest.mark.parametrize("offsets", [None, {"from": 800, "to": 800},
    {"from": -1, "to": 900}, {"from": 100, "to": 2000}])
def test_unavailable_token_time_retains_text_without_inventing_alignment(tmp_path, offsets):
    payload = output()
    payload["transcription"][0]["tokens"][1]["offsets"] = offsets
    segment = parse_whisper_json(payload, audio(tmp_path))["segments"][0]
    token = segment["tokens"][0]
    assert token["text"] == "你好" and token["timestamp_precision"] == "unavailable"
    assert "source_start" not in token
    assert "token_time_unavailable_or_out_of_segment" in segment["uncertainty"]


@pytest.mark.parametrize("change", [
    lambda p: p["transcription"][0]["offsets"].update({"from": -1}),
    lambda p: p["transcription"][0]["offsets"].update({"to": 2100}),
    lambda p: p["transcription"][0]["offsets"].update({"from": True}),
    lambda p: p["transcription"][0]["tokens"][1].update({"p": float("nan")}),
    lambda p: p.update({"result": []}),
    lambda p: p.update({"transcription": "not a list"}),
])
def test_malformed_result_fails_closed(tmp_path, change):
    payload = output()
    change(payload)
    with pytest.raises(LocalASRError, match="结果|范围"):
        parse_whisper_json(payload, audio(tmp_path))


def test_small_end_quantization_is_explicit_not_silent(tmp_path):
    payload = output()
    payload["transcription"][0]["offsets"]["to"] = 2010
    parsed = parse_whisper_json(payload, audio(tmp_path))
    assert parsed["segments"][0]["raw_end_ms"] == 2010
    assert parsed["segments"][0]["end_ms"] == 2000
    assert "end_quantization_clamped" in parsed["warnings"]


def test_empty_output_is_only_no_speech_candidate(tmp_path):
    parsed = parse_whisper_json({"transcription": []}, audio(tmp_path))
    assert parsed["status"] == "no_speech_candidate"
    assert parsed["quality_passed"] is False


def test_model_music_marker_remains_annotation_candidate_not_spoken_dialogue(tmp_path):
    payload = output()
    payload["transcription"][0]["text"] = "[音乐]"
    segment = parse_whisper_json(payload, audio(tmp_path))["segments"][0]
    assert segment["kind"] == "non_speech_annotation_candidate"
    assert "model_non_dialogue_marker_requires_audio_review" in segment["uncertainty"]


def test_changed_input_or_invalid_format_is_not_processed(tmp_path):
    bound = audio(tmp_path)
    bound.path.write_bytes(b"not wav")
    with pytest.raises(LocalASRError) as err:
        bound.validate()
    assert err.value.code == "audio_changed"
    bound = replace(bound, audio_sha256=sha256(bound.path))
    with pytest.raises(LocalASRError) as err:
        bound.validate()
    assert err.value.code == "invalid_audio_format"


@pytest.mark.parametrize("remaining_pcm_bytes", [0, 1, 2, 31999, 32000, 63999])
@pytest.mark.parametrize("exact_silence", [True, False])
def test_rehashed_header_or_partial_pcm_is_rejected_not_silence(tmp_path, remaining_pcm_bytes, exact_silence):
    bound = audio(tmp_path, exact_silence=exact_silence)
    original = bound.path.read_bytes()
    # The fixture uses the canonical 44-byte WAV header, still declaring 32000
    # frames after truncation. Rebinding a new hash cannot legitimize missing PCM.
    assert len(original) == 44 + 64000
    bound.path.write_bytes(original[:44 + remaining_pcm_bytes])
    rebound = replace(bound, audio_sha256=sha256(bound.path))
    with pytest.raises(LocalASRError) as err:
        rebound.validate()
    assert err.value.code == "invalid_audio_format"
    with pytest.raises(LocalASRError) as err:
        module._exact_pcm_silence(bound.path)
    assert err.value.code == "invalid_audio_format"


def test_zero_byte_wav_is_rejected_even_with_new_matching_hash(tmp_path):
    bound = audio(tmp_path)
    bound.path.write_bytes(b"")
    rebound = replace(bound, audio_sha256=sha256(bound.path))
    with pytest.raises(LocalASRError) as err:
        rebound.validate()
    assert err.value.code == "invalid_audio_format"


@pytest.mark.parametrize("exact_silence", [True, False])
def test_complete_pcm_distinguishes_exact_silence_from_low_nonzero_samples(tmp_path, exact_silence):
    bound = audio(tmp_path, exact_silence=exact_silence)
    bound.validate()
    assert module._exact_pcm_silence(bound.path) is exact_silence


def test_invalid_pcm_is_rechecked_before_reusing_legacy_erroneous_success(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    bound = audio(tmp_path, exact_silence=True)
    bound.path.write_bytes(bound.path.read_bytes()[:44])
    rebound = replace(bound, audio_sha256=sha256(bound.path))
    # Simulate the old defect's cached header-only "silence success". Retain the
    # evidence but require current input validation before trusting that result.
    with monkeypatch.context() as legacy:
        legacy.setattr(BoundAudio, "validate", lambda self: None)
        legacy.setattr(module, "_exact_pcm_silence", lambda path: True)
        old_result = run_local_asr(rebound, root=root)
    assert old_result["state"] == "succeeded"
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("damaged input ran engine"))
    with pytest.raises(LocalASRError) as err:
        run_local_asr(rebound, root=root)
    assert err.value.code == "invalid_audio_format"
    assert (Path(old_result["job_dir"]) / "state.json").is_file()


def test_complete_job_replays_and_configuration_change_creates_new_job(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    bound = audio(tmp_path)
    first = run_local_asr(bound, root=root)
    second = run_local_asr(bound, root=root)
    different = run_local_asr(bound, root=root, language="en")
    assert first["state"] == "succeeded" and first["reused"] is False
    assert second["reused"] is True and second["attempt"] == 1
    assert different["fingerprint"] != first["fingerprint"]
    job = Path(first["job_dir"])
    assert "你好，回来。" in (job / "candidate.srt").read_text()
    assert "00:00:00,100 --> 00:00:01,900" in (job / "candidate.srt").read_text()
    assert stat.S_IMODE((job / "result.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert "你好" not in (job / "state.json").read_text()
    assert first["quality_passed"] is False


def test_cancel_then_resume_preserves_attempt_and_success(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch, sleep=0.2)
    bound = audio(tmp_path)
    calls = 0
    def cancelled_after_launch():
        nonlocal calls
        calls += 1
        return calls > 1
    with pytest.raises(LocalASRError) as err:
        run_local_asr(bound, root=root, cancelled=cancelled_after_launch)
    assert err.value.code == "cancelled"
    state_path = next((root / "runs").glob("*/state.json"))
    assert json.loads(state_path.read_text())["state"] == "cancelled"
    result = run_local_asr(bound, root=root)
    assert result["attempt"] == 2 and result["recovered_from"] == "cancelled"
    assert (state_path.parent / "attempt-0001/input.wav").exists()
    assert (state_path.parent / "attempt-0001/engine.log").exists()
    assert (state_path.parent / "attempt-0002/engine.log").exists()


def test_exact_silence_skips_engine_without_claiming_quality_pass(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("silence ran engine"))
    result = run_local_asr(audio(tmp_path, exact_silence=True), root=root)
    assert result["engine_executed"] is False
    assert result["segment_count"] == 0 and result["quality_passed"] is False
    parsed = json.loads((Path(result["job_dir"]) / "result.json").read_text())
    assert "exact_zero_pcm_confirmed_engine_skipped" in parsed["warnings"]


def test_changed_completed_result_cannot_be_replayed(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    bound = audio(tmp_path)
    result = run_local_asr(bound, root=root)
    (Path(result["job_dir"]) / "result.json").write_text("{}")
    with pytest.raises(LocalASRError) as err:
        run_local_asr(bound, root=root)
    assert err.value.code == "result_changed"


def test_concurrent_same_input_is_not_launched_twice(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch, sleep=0.5)
    bound = audio(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run_local_asr, bound, root=root)
        deadline = time.monotonic() + 3
        while not list((root / "runs").glob("*/state.json")) and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(LocalASRError) as err:
            run_local_asr(bound, root=root)
        assert err.value.code == "already_running"
        assert first.result()["attempt"] == 1


def test_crash_during_input_copy_can_resume_without_overwriting_partial_attempt(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    bound = audio(tmp_path)
    original_copy = module.shutil.copyfile
    def failed_copy(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(module.shutil, "copyfile", failed_copy)
    with pytest.raises(OSError):
        run_local_asr(bound, root=root)
    partial = next((root / "runs").glob("*/attempt-0001"))
    monkeypatch.setattr(module.shutil, "copyfile", original_copy)
    result = run_local_asr(bound, root=root)
    assert result["attempt"] == 2
    assert partial.exists()


def test_model_checksum_mismatch_is_not_executed(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    (root / "models/ggml-small.bin").write_bytes(b"changed")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("bad model ran"))
    with pytest.raises(LocalASRError) as err:
        run_local_asr(audio(tmp_path), root=root)
    assert err.value.code == "model_mismatch"


def test_explicit_model_choice_keeps_small_identity_and_separates_medium(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    medium = root / "models/ggml-medium.bin"
    medium.write_bytes(b"synthetic-medium-weights")
    monkeypatch.setattr(module, "MEDIUM_BYTES", medium.stat().st_size)
    monkeypatch.setattr(module, "MEDIUM_SHA256", sha256(medium))
    bound = audio(tmp_path)
    small = run_local_asr(bound, root=root)
    old_result = Path(small["job_dir"]) / "result.json"
    old_digest = sha256(old_result)
    explicit_small = run_local_asr(bound, root=root, model_size="small")
    candidate = run_local_asr(bound, root=root, model_size="medium")
    assert explicit_small["reused"] is True
    assert explicit_small["fingerprint"] == small["fingerprint"]
    assert candidate["config"]["model"] == "medium-multilingual"
    assert candidate["fingerprint"] != small["fingerprint"]
    assert sha256(old_result) == old_digest
    assert candidate["quality_passed"] is False
    assert run_local_asr(bound, root=root, model_size="medium")["reused"] is True


def test_wrong_medium_weights_are_rejected_without_falling_back_to_small(tmp_path, monkeypatch):
    root = prepare_fake(tmp_path, monkeypatch)
    (root / "models/ggml-medium.bin").write_bytes(b"not-the-pinned-medium")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("bad medium ran"))
    with pytest.raises(LocalASRError) as err:
        run_local_asr(audio(tmp_path), root=root, model_size="medium")
    assert err.value.code == "model_mismatch"


def test_model_choices_are_fixed_multilingual_specs_and_invalid_choice_fails_early(tmp_path, monkeypatch):
    for size in ("small", "medium"):
        specification = module.local_model_spec(size)
        assert specification["filename"] == f"ggml-{size}.bin"
        assert specification["url"] == (
            f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{module.MODEL_REVISION}/ggml-{size}.bin")
    bound = audio(tmp_path)
    monkeypatch.setattr(BoundAudio, "validate", lambda self: pytest.fail("invalid model read media"))
    with pytest.raises(LocalASRError) as err:
        run_local_asr(bound, model_size="../untrusted-model")
    assert err.value.code == "invalid_model"


def test_cli_prepare_status_and_run_route_same_explicit_model(monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/check_local_asr.py"
    spec = importlib.util.spec_from_file_location("local_asr_model_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: pytest.fail("test used network"))
    assert cli.main(["--model", "medium"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["selected_model"] == "medium"
    assert status["expected_model_bytes"] == module.MEDIUM_BYTES
    selected = []
    monkeypatch.setattr(cli, "prepare", lambda root, size: selected.append(size) or {"model": size})
    assert cli.main(["--prepare", "--model", "medium"]) == 0
    monkeypatch.setattr(cli, "load_designated_audio", lambda: object())
    monkeypatch.setattr(cli, "run_local_asr", lambda audio, **kwargs:
        selected.append(kwargs["model_size"]) or {"model": kwargs["model_size"]})
    assert cli.main(["--run-sample", "--backup-verified", "--model", "medium"]) == 0
    assert selected == ["medium", "medium"]


def test_private_storage_rejects_cloud_and_product(tmp_path):
    for directory in [module.PROJECT / ".runtime/asr", tmp_path / "Mobile Documents/asr"]:
        with pytest.raises(LocalASRError) as err:
            module.private_root(directory)
        assert err.value.code == "unsafe_storage"


def test_cli_status_is_read_only_and_sample_needs_backup_flag(monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/check_local_asr.py"
    spec = importlib.util.spec_from_file_location("local_asr_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "load_designated_audio", lambda: pytest.fail("sample read before backup"))
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda *a, **k: pytest.fail("status used network"))
    assert cli.main([]) == 0
    assert "未接入工作台" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--run-sample"])
