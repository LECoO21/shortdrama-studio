import hashlib
import json
import subprocess
import wave
from fractions import Fraction

import av
import numpy as np
import pytest

from app import preprocess
from app.media import ffmpeg_binary, probe_media
from app.preprocess import PreprocessCancelled, prepare_media


def create(path, args):
    subprocess.run([ffmpeg_binary(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                    *args, str(path)], check=True, capture_output=True, timeout=30)
    return path


def asset(path):
    return {"id": "test-source", "version": 3, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "metadata": probe_media(path)}


@pytest.fixture
def red_blue(tmp_path):
    return create(tmp_path / "red-blue.mp4", [
        "-f", "lavfi", "-i", "color=c=red:s=800x450:r=25:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=800x450:r=25:d=1",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"])


def thumbnail_rgb(path):
    with av.open(str(path)) as container:
        return next(container.decode(video=0)).to_ndarray(format="rgb24").mean(axis=(0, 1))


def test_real_derivatives_scene_candidates_audio_and_unchanged_original(tmp_path, red_blue):
    media = asset(red_blue)
    output = tmp_path / "derivatives"
    updates = []
    result = prepare_media(media, output, progress=updates.append)
    # The manifest is persisted by the caller and contains no absolute paths.
    json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert result["asset_id"] == media["id"] and result["asset_version"] == 3
    assert result["source_sha256"] == hashlib.sha256(red_blue.read_bytes()).hexdigest()
    assert result["scene_detection"]["status"] == "candidate"
    assert result["transcript"] == {"status": "not_configured", "segments": []}
    shots = result["shots"]
    assert len(shots) == 2
    video = next(s for s in media["metadata"]["streams"] if s["type"] == "video")
    base = Fraction(video["time_base"]["num"], video["time_base"]["den"])
    assert shots[0]["start_pts"] == video["start_pts"]
    assert int(shots[0]["end_pts"]) * base == 1
    assert shots[0]["end_pts"] == shots[1]["start_pts"]
    assert shots[1]["end_pts"] == video["end_pts"]
    assert all(s["start_pts"] in video["frame_pts"] for s in shots)
    assert all(s["description_status"] == "unknown" and s["description"] is None for s in shots)
    red, blue = [thumbnail_rgb(output / s["thumbnail"]) for s in shots]
    assert red[0] > red[2] + 100 and blue[2] > blue[0] + 100
    assert result["proxy"]["width"] <= 640
    proxy = probe_media(output / result["proxy"]["path"])
    assert proxy["video_stream_index"] is not None and proxy["audio_stream_index"] is not None
    assert abs(proxy["duration_seconds"] - 2) < 0.2
    with wave.open(str(output / "audio.wav")) as audio:
        assert (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) == (1, 16000, 2)
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        assert np.abs(samples.astype(np.int32)).max() > 1000
    assert max(result["waveform"]) > 0.05 and len(result["waveform"]) >= 20
    assert updates[0] == 0 and updates[-1] == 1 and updates == sorted(updates)
    assert sorted(p.name for p in output.iterdir()) == ["audio.wav", "proxy.mp4", "shot-0001.jpg", "shot-0002.jpg"]


def test_audio_only_and_cancel_running_encoder_remove_partial_files(tmp_path, monkeypatch):
    source = create(tmp_path / "tone.wav", ["-f", "lavfi", "-i",
                    "sine=frequency=660:sample_rate=44100:duration=1", "-c:a", "pcm_s16le"])
    media = asset(source)
    result = prepare_media(media, tmp_path / "audio")
    assert result["video"] is None and result["proxy"] is None and result["shots"] == []
    assert result["audio"]["sample_count"] == 16000
    assert len(result["waveform"]) == 10 and max(result["waveform"]) > 0.1
    original_popen = subprocess.Popen
    processes = []

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_process)
    checks = []

    def cancelled():
        checks.append(1)
        return len(checks) >= 4

    output = tmp_path / "cancelled"
    with pytest.raises(PreprocessCancelled):
        prepare_media(media, output, cancelled=cancelled)
    assert processes and all(p.poll() is not None for p in processes)
    assert list(output.iterdir()) == []
    assert hashlib.sha256(source.read_bytes()).hexdigest() == media["sha256"]


def test_vfr_nonzero_source_pts_and_delayed_audio_mapping(tmp_path):
    source = create(tmp_path / "offset-vfr.mp4", [
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=1",
        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=1.6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,select='not(eq(mod(n,5),1))',setpts=PTS+1/TB[v];[2:a]asetpts=PTS+1.4/TB[a]",
        "-map", "[v]", "-map", "[a]", "-copyts", "-fps_mode", "vfr",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-avoid_negative_ts", "disabled"])
    media = asset(source)
    video, audio = media["metadata"]["streams"]
    assert video["is_vfr"] is True and int(video["start_pts"]) > 0
    output = tmp_path / "vfr"
    result = prepare_media(media, output)
    assert result["video"]["time_base"] == video["time_base"]
    assert result["proxy"]["source_start_pts"] == video["start_pts"]
    assert result["proxy"]["source_time_base"] == video["time_base"]
    assert result["audio"]["start_pts"] == audio["start_pts"]
    assert result["audio"]["time_base"] == audio["time_base"]
    assert len(result["shots"]) == 2
    assert result["shots"][0]["start_pts"] == video["start_pts"]
    assert result["shots"][-1]["end_pts"] == video["end_pts"]
    for left, right in zip(result["shots"], result["shots"][1:]):
        assert left["end_pts"] == right["start_pts"]
        assert right["start_pts"] in video["frame_pts"]
    # Source delay survives proxy normalization; it is not accidentally
    # replaced with independent STARTPTS normalization on both streams.
    with av.open(str(output / "proxy.mp4")) as container:
        sound = np.concatenate([f.to_ndarray().flatten() for f in container.decode(audio=0)])
    assert np.abs(sound[:8000]).max() < 0.001
    assert np.abs(sound[18000:30000]).max() > 0.02


def test_silent_video_produces_no_fake_audio_or_transcript(tmp_path):
    source = create(tmp_path / "silent.mp4", ["-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast"])
    result = prepare_media(asset(source), tmp_path / "silent")
    assert result["audio"] is None and result["waveform"] == []
    assert len(result["shots"]) == 1
    assert not (tmp_path / "silent" / "audio.wav").exists()


def test_limits_integrity_existing_outputs_and_safe_encoder_error(tmp_path, red_blue, monkeypatch):
    media = asset(red_blue)
    with pytest.raises(PreprocessCancelled):
        prepare_media(media, tmp_path / "never-created", cancelled=lambda: True)
    assert not (tmp_path / "never-created").exists()
    corrupt = dict(media, sha256="0" * 64)
    with pytest.raises(ValueError, match="完整性"):
        prepare_media(corrupt, tmp_path / "invalid")
    too_long = json.loads(json.dumps(media))
    stream = too_long["metadata"]["streams"][0]
    base = Fraction(stream["time_base"]["num"], stream["time_base"]["den"])
    stream["end_pts"] = str(int(stream["start_pts"]) + int(601 / base))
    with pytest.raises(ValueError, match="10 分钟"):
        prepare_media(too_long, tmp_path / "long")
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "proxy.mp4").write_bytes(b"keep me")
    with pytest.raises(ValueError, match="已存在"):
        prepare_media(media, existing)
    assert (existing / "proxy.mp4").read_bytes() == b"keep me"
    assert [p.name for p in existing.iterdir()] == ["proxy.mp4"]
    monkeypatch.setattr(preprocess, "ffmpeg_binary", lambda: "/usr/bin/false")
    failed = tmp_path / "failed"
    with pytest.raises(ValueError, match="媒体预处理失败") as failure:
        prepare_media(media, failed)
    assert str(tmp_path) not in str(failure.value)
    assert list(failed.iterdir()) == []
    monkeypatch.setattr(preprocess, "TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        prepare_media(media, tmp_path / "timeout")
