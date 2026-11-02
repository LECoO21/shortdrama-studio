import copy
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import av
import numpy as np
import pytest

from app.media import ffmpeg_binary, generate_demo_media, probe_media
from app.render import RenderCancelled, render_document, validate_media_document


def asset(path, identity="red"):
    return {"id": identity, "version": 1, "path": str(path), "metadata": probe_media(path)}


def clip(media, duration=25, offset=0, identity="clip", source_seconds=0, fps=Fraction(25)):
    stream = next(s for s in media["metadata"]["streams"] if s["type"] == "video")
    tb = Fraction(stream["time_base"]["num"], stream["time_base"]["den"])
    start = int(stream["start_pts"]) + int(Fraction(source_seconds) / tb)
    end = start + round(Fraction(duration, 1) / fps / tb)
    return {"id": identity, "track_id": "video", "source": {
        "asset_id": media["id"], "version": 1, "stream_index": stream["index"],
        "time_base": stream["time_base"], "in_pts": str(start), "out_pts": str(end)},
        "timeline_start_frame": offset, "duration_frames": duration,
        "gain_db": 0, "muted": False, "fade_in_frames": 0, "fade_out_frames": 0,
        "fit": "contain", "include_source_audio": True}


def document(clips, fps=Fraction(25)):
    return {"schema_version": 1, "project_id": "test", "name": "媒体回归",
            "canvas": {"width": 320, "height": 180}, "fps": {"num": fps.numerator, "den": fps.denominator},
            "sample_rate": 48000, "duration_frames": max(c["timeline_start_frame"] + c["duration_frames"] for c in clips),
            "tracks": [{"id": "video", "kind": "video", "enabled": True},
                       {"id": "audio", "kind": "audio", "enabled": True},
                       {"id": "subtitle", "kind": "subtitle", "enabled": True}],
            "clips": clips, "subtitles": []}


def frames(path):
    with av.open(str(path)) as container:
        return [frame.to_ndarray(format="rgb24") for frame in container.decode(video=0)]


def samples(path):
    with av.open(str(path)) as container:
        return np.concatenate([f.to_ndarray() for f in container.decode(audio=0)], axis=1)[0]


def rms(array):
    return float(np.sqrt(np.mean(np.square(array.astype(np.float64)))))


def create(path, args):
    subprocess.run([ffmpeg_binary(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args, str(path)],
                   check=True, capture_output=True, timeout=60)
    return path


@pytest.fixture(scope="module")
def demos(tmp_path_factory):
    return generate_demo_media(tmp_path_factory.mktemp("demo"))


def test_actual_cut_order_subtitle_mute_and_originals_unchanged(tmp_path, demos):
    red, blue = asset(demos[0]), asset(demos[1], "blue")
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in demos]
    first, second = clip(red), clip(blue, offset=25, identity="blue-clip")
    second["muted"] = True
    doc = document([first, second])
    doc["subtitles"] = [{"id": "sub", "track_id": "subtitle", "text": "第一幕：开始\n中文测试", "start_frame": 5,
                         "end_frame": 20, "font_size": 20, "color": "#FFFFFF", "bottom_percent": 10}]
    progress = []
    output = tmp_path / "cut.mp4"
    metadata = render_document(doc, {"red": red, "blue": blue}, output, progress.append)
    decoded = frames(output)
    assert len(decoded) == 50
    assert metadata["streams"][0]["frame_count"] == 50
    assert decoded[24][20, 20, 0] > decoded[24][20, 20, 2] + 50
    assert decoded[25][20, 20, 2] > decoded[25][20, 20, 0] + 50
    # White subtitle pixels appear only inside the explicit frame interval.
    assert np.sum(np.all(decoded[10][80:, :, :] > 200, axis=2)) > 100
    assert np.sum(np.all(decoded[5][80:, :, :] > 200, axis=2)) > 100
    assert np.sum(np.all(decoded[0][80:, :, :] > 200, axis=2)) < 10
    assert np.sum(np.all(decoded[20][80:, :, :] > 200, axis=2)) < 10
    audio = samples(output)
    assert rms(audio[12000:36000]) > 0.03
    assert rms(audio[60000:84000]) < 0.0001
    assert progress[0] == 0 and progress[-1] == 1
    assert hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in demos]


@pytest.mark.parametrize("rate,vfr", [("30000/1001", False), ("25", True)])
def test_fractional_rate_and_vfr_retain_real_source_pts(tmp_path, rate, vfr):
    args = ["-f", "lavfi", "-i", f"testsrc2=s=160x90:r={rate}:d=2"]
    if vfr:
        args += ["-vf", "select='not(eq(mod(n,5),1))'", "-fps_mode", "vfr"]
    source = create(tmp_path / "source.mp4", args + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    media = asset(source)
    stream = media["metadata"]["streams"][0]
    assert stream["is_vfr"] is vfr
    fps = Fraction(rate)
    count = 60 if not vfr else 50
    doc = document([clip(media, duration=count, fps=fps)], fps)
    doc["canvas"] = {"width": 160, "height": 90}
    output = tmp_path / "output.mp4"
    metadata = render_document(doc, {"red": media}, output)
    out_stream = metadata["streams"][0]
    pts = list(map(int, out_stream["frame_pts"]))
    tb = Fraction(out_stream["time_base"]["num"], out_stream["time_base"]["den"])
    assert len(pts) == count
    assert all((b - a) * tb == 1 / fps for a, b in zip(pts, pts[1:]))
    assert rms(samples(output)) == 0
    # Check actual sampled content, including the repeated image at a VFR gap.
    original, rendered = frames(source), frames(output)
    source_tb = Fraction(stream["time_base"]["num"], stream["time_base"]["den"])
    source_pts = [int(p) * source_tb for p in stream["frame_pts"]]
    for frame_index in (0, 1, 5, 24, 40):
        expected = max(i for i, point in enumerate(source_pts) if point <= Fraction(frame_index, 1) / fps)
        error = np.abs(original[expected].astype(float) - rendered[frame_index].astype(float)).mean()
        assert error < 5


def test_nonzero_pts_and_delayed_audio_are_aligned(tmp_path):
    source = create(tmp_path / "offset.mp4", [
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=3",
        "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:duration=2.6",
        "-vf", "setpts=PTS+1/TB", "-af", "asetpts=PTS+1.4/TB", "-copyts",
        "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-avoid_negative_ts", "disabled"])
    media = asset(source)
    video, audio = media["metadata"]["streams"]
    vstart = int(video["start_pts"]) * Fraction(video["time_base"]["num"], video["time_base"]["den"])
    astart = int(audio["start_pts"]) * Fraction(audio["time_base"]["num"], audio["time_base"]["den"])
    assert vstart >= 1 and astart - vstart > Fraction(3, 10)
    doc = document([clip(media, duration=50)])
    output = tmp_path / "aligned.mp4"
    render_document(doc, {"red": media}, output)
    sound = samples(output)
    assert len(frames(output)) == 50
    assert rms(sound[2400:12000]) < 0.0001
    assert rms(sound[28800:48000]) > 0.03


def test_audio_track_gain_fade_and_track_switch(tmp_path, demos):
    media = asset(demos[0])
    video_clip = clip(media, duration=50)
    video_clip["include_source_audio"] = False
    audio_clip = copy.deepcopy(video_clip)
    audio_clip.update(id="audio-clip", track_id="audio", gain_db=-6, fade_in_frames=10, fade_out_frames=10)
    source = next(s for s in media["metadata"]["streams"] if s["type"] == "audio")
    audio_clip["source"].update(stream_index=source["index"], time_base=source["time_base"], in_pts="0", out_pts="96000")
    doc = document([video_clip, audio_clip])
    output = tmp_path / "audio.mp4"
    render_document(doc, {"red": media}, output)
    sound = samples(output)
    assert 0.02 < rms(sound[36000:60000]) < 0.04
    assert rms(sound[1000:2000]) < rms(sound[36000:60000]) / 4
    assert rms(sound[94000:95000]) < rms(sound[36000:60000]) / 4
    doc["tracks"][1]["enabled"] = False
    muted_output = tmp_path / "disabled.mp4"
    render_document(doc, {"red": media}, muted_output)
    assert rms(samples(muted_output)) == 0


def test_rejects_time_mapping_lies_and_cancel_does_not_publish(tmp_path, demos):
    media = asset(demos[0])
    doc = document([clip(media)])
    broken = copy.deepcopy(doc)
    broken["clips"][0]["duration_frames"] = 70
    assert "duration_1x" in {i["code"] for i in validate_media_document(broken, {"red": media})}
    broken["clips"][0]["source"]["out_pts"] = "999999"
    assert "source_range" in {i["code"] for i in validate_media_document(broken, {"red": media})}
    output = tmp_path / "cancelled.mp4"
    with pytest.raises(RenderCancelled):
        render_document(doc, {"red": media}, output, cancelled=lambda: True)
    assert not output.exists()
    checks = []
    def cancel_while_running():
        checks.append(1)
        return len(checks) >= 2
    with pytest.raises(RenderCancelled):
        render_document(doc, {"red": media}, output, cancelled=cancel_while_running)
    assert not output.exists()
    assert list(tmp_path.glob("render-*")) == []


def test_contain_cover_and_disabled_video(tmp_path, demos):
    media = asset(demos[0])
    doc = document([clip(media)])
    doc["canvas"] = {"width": 180, "height": 180}
    contain = tmp_path / "contain.mp4"
    render_document(doc, {"red": media}, contain)
    picture = frames(contain)[10]
    assert picture[5, 5].max() < 5 and picture[90, 90, 0] > 100
    doc["clips"][0]["fit"] = "cover"
    cover = tmp_path / "cover.mp4"
    render_document(doc, {"red": media}, cover)
    assert frames(cover)[10][5, 5, 0] > 100
    doc["tracks"][0]["enabled"] = False
    disabled = tmp_path / "black.mp4"
    render_document(doc, {"red": media}, disabled)
    assert frames(disabled)[10].max() < 5


def test_subtitle_literals_do_not_inject_ass_and_demo_publication_is_atomic(tmp_path, demos):
    media = asset(demos[0])
    doc = document([clip(media)])
    doc["subtitles"] = [{"id": "s", "track_id": "subtitle", "text": "{\\fs120}x\\Ny", "start_frame": 0,
                         "end_frame": 25, "font_size": 20, "color": "#FFFFFF", "bottom_percent": 10}]
    output = tmp_path / "literal.mp4"
    render_document(doc, {"red": media}, output)
    image = frames(output)[10]
    ys, xs = np.where(np.all(image > 200, axis=2))
    assert len(xs) > 100
    # Literal \N remains one line and literal fs120 does not enlarge the font.
    assert max(ys) - min(ys) < 20
    original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        render_document(doc, {"red": media}, output)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == original_hash
    directory = tmp_path / "concurrent-demo"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: generate_demo_media(directory), range(2)))
    assert results[0] == results[1]
    assert len(list(directory.iterdir())) == 2
    assert probe_media(results[0][0])["streams"][0]["frame_count"] == 75


def test_subtitle_precision_gate_is_explicit_above_100fps(tmp_path, demos):
    media = asset(demos[0])
    doc = document([clip(media, duration=120, fps=Fraction(120))], Fraction(120))
    doc["subtitles"] = [{"id": "s", "track_id": "subtitle", "text": "精确到帧", "start_frame": 1,
                         "end_frame": 2, "font_size": 20, "color": "#FFFFFF", "bottom_percent": 10}]
    issues = validate_media_document(doc, {"red": media})
    assert [i["code"] for i in issues] == ["subtitle_fps"]
    output = tmp_path / "unsupported.mp4"
    with pytest.raises(ValueError, match="100 fps"):
        render_document(doc, {"red": media}, output)
    assert not output.exists()
    doc["tracks"][2]["enabled"] = False
    assert validate_media_document(doc, {"red": media}) == []
