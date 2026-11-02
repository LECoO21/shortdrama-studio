"""Deterministic, bounded FFmpeg renderer for the phase-one document contract."""
from __future__ import annotations

import bisect
import os
import selectors
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Callable

from .media import ffmpeg_binary, probe_media


class RenderCancelled(Exception):
    pass


def rational(value: dict) -> Fraction:
    return Fraction(int(value["num"]), int(value["den"]))


def _round_half_up(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _seconds(value: Fraction) -> str:
    return f"{float(value):.12f}"


def _stream(asset: dict, index: int) -> dict | None:
    return next((s for s in asset["metadata"]["streams"] if s["index"] == index), None)


def validate_media_document(document: dict, assets: dict) -> list[dict]:
    """Supplement schema/QC checks with actual media range and 1x checks."""
    issues = []
    fps = rational(document["fps"])
    tracks = {t["id"]: t for t in document["tracks"]}
    if fps > 100 and any(tracks[s["track_id"]]["enabled"] for s in document["subtitles"]):
        issues.append({"code": "subtitle_fps", "message": "当前字幕渲染支持不高于 100 fps 的工程"})
    for clip in document["clips"]:
        def issue(code, message):
            issues.append({"code": code, "message": message, "clip_id": clip["id"]})
        source = clip["source"]
        asset = assets.get(source["asset_id"])
        if asset is None:
            issue("missing_asset", "引用素材不存在")
            continue
        if str(asset.get("version", 1)) != str(source["version"]):
            issue("asset_version", "素材版本与工程引用不一致")
        stream = _stream(asset, source["stream_index"])
        if stream is None:
            issue("missing_stream", "引用音视频流不存在")
            continue
        if source["time_base"] != stream["time_base"]:
            issue("time_base", "源时间基与实际媒体不一致")
            continue
        track = tracks.get(clip["track_id"])
        if track and track["kind"] != stream["type"]:
            issue("stream_type", "轨道类型与引用媒体流不一致")
        start, end = int(source["in_pts"]), int(source["out_pts"])
        if start < int(stream["start_pts"]) or end > int(stream["end_pts"]) or end <= start:
            issue("source_range", "源入出点超出实际解码范围")
        time_base = rational(source["time_base"])
        source_frames = (end - start) * time_base * fps
        target = int(clip["duration_frames"])
        # One source tick may arise when a rational project boundary is split.
        tick = time_base * fps
        if abs(source_frames - target) > Fraction(1, 2) + tick:
            issue("duration_1x", "源区间与工程时长不符合 1 倍速")
        if min(clip.get("fade_in_frames", 0), clip.get("fade_out_frames", 0)) < 0:
            issue("audio_fade", "淡入淡出不能为负数")
        if max(clip.get("fade_in_frames", 0), clip.get("fade_out_frames", 0)) > target:
            issue("audio_fade", "淡入淡出不能超出片段时长")
    return issues


def _font() -> tuple[str, Path]:
    configured = os.environ.get("STUDIO_FONT_FILE")
    candidates = ([Path(configured)] if configured else []) + [
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for path in candidates:
        if path.is_file():
            # libass loads this file from the private job directory. The macOS
            # font is referenced locally only; it is never copied/distributed.
            return ("Hiragino Sans GB" if "Hiragino" in path.name else "Noto Sans CJK SC", path)
    raise RuntimeError("中文字幕需要中文字体；请配置 STUDIO_FONT_FILE（生产使用有许可的字体）")


def _ass_time(frame: int, fps: Fraction) -> str:
    centiseconds = (Fraction(frame, 1) / fps * 100).__floor__()
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def _write_subtitles(document: dict, directory: Path) -> bool:
    enabled = {t["id"] for t in document["tracks"] if t["enabled"] and t["kind"] == "subtitle"}
    subtitles = [s for s in document["subtitles"] if s["track_id"] in enabled]
    if not subtitles:
        return False
    fps = rational(document["fps"])
    if fps > 100:
        raise ValueError("当前字幕渲染支持不高于 100 fps 的工程")
    font_name, font_path = _font()
    fonts = directory / "fonts"
    fonts.mkdir()
    (fonts / font_path.name).symlink_to(font_path)
    width, height = document["canvas"]["width"], document["canvas"]["height"]
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}",
             f"PlayResY: {height}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
             "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             f"Style: Default,{font_name},28,&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,16,16,24,1", "",
             "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for item in subtitles:
        color = item.get("color", "#FFFFFF").lstrip("#")
        if len(color) != 6 or any(c not in "0123456789abcdefABCDEF" for c in color):
            raise ValueError("字幕颜色必须是六位十六进制颜色")
        bgr = color[4:6] + color[2:4] + color[:2]
        # libass still interprets the second slash of "\\\\N" as a newline.
        # A zero-width word joiner breaks literal user escape sequences while
        # preserving their visible text. Only real newlines become ASS \\N.
        text = item["text"].replace("\\", "\\\u2060").replace("{", "\\{").replace("}", "\\}")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\N")
        y = height * (1 - item.get("bottom_percent", 10) / 100)
        overrides = f"{{\\fs{item.get('font_size', 28)}\\c&H{bgr}&\\pos({width / 2:.2f},{y:.2f})\\an2}}"
        lines.append(f"Dialogue: 0,{_ass_time(item['start_frame'], fps)},{_ass_time(item['end_frame'], fps)},Default,,0,0,0,,{overrides}{text}")
    (directory / "subtitles.ass").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def render_document(document: dict, assets: dict, output_path: Path,
                    progress: Callable[[float], None] = lambda _: None,
                    cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Render a frozen document; caller owns durable job state and publication."""
    if cancelled():
        raise RenderCancelled("任务已取消")
    issues = validate_media_document(document, assets)
    if issues:
        raise ValueError("; ".join(item["message"] for item in issues))
    output_path = Path(output_path).resolve()
    if output_path.exists():
        raise FileExistsError("渲染不能覆盖已有产物")
    for asset in assets.values():
        if Path(asset["path"]).resolve() == output_path:
            raise ValueError("渲染不能覆盖原始素材")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fps = rational(document["fps"])
    frame_count = document["duration_frames"]
    duration = Fraction(frame_count, 1) / fps
    sample_rate = document["sample_rate"]
    total_samples = _round_half_up(duration * sample_rate)
    width, height = document["canvas"]["width"], document["canvas"]["height"]
    tracks = {t["id"]: t for t in document["tracks"]}
    enabled_clips = [c for c in document["clips"] if tracks[c["track_id"]]["enabled"]]
    videos = sorted([c for c in enabled_clips if tracks[c["track_id"]]["kind"] == "video"], key=lambda c: c["timeline_start_frame"])
    if videos:
        position = 0
        for clip in videos:
            if clip["timeline_start_frame"] != position:
                raise ValueError("当前视频轨不支持重叠或空洞")
            position += clip["duration_frames"]
        if position != frame_count:
            raise ValueError("视频轨必须覆盖工程时长")
    with tempfile.TemporaryDirectory(prefix="render-", dir=output_path.parent) as temporary:
        directory = Path(temporary)
        has_subtitles = _write_subtitles(document, directory)
        command = [ffmpeg_binary(), "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-copyts"]
        indices = {}
        for clip in enabled_clips:
            indices[clip["id"]] = len(indices)
            command += ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi,wav,mp3,flac,ogg,mpegts", "-i", str(Path(assets[clip["source"]["asset_id"]]["path"]).resolve())]
        filters = []
        video_labels = []
        for i, clip in enumerate(videos):
            source = clip["source"]
            asset = assets[source["asset_id"]]
            stream = _stream(asset, source["stream_index"])
            start, end = int(source["in_pts"]), int(source["out_pts"])
            points = [int(p) for p in stream["frame_pts"]]
            preceding = points[max(0, bisect.bisect_right(points, start) - 1)]
            if clip.get("fit", "contain") == "cover":
                fit = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
            else:
                fit = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            label = f"v{i}"
            filters.append(f"[{indices[clip['id']]}:{source['stream_index']}]trim=start_pts={preceding}:end_pts={end},setpts=PTS-{start},fps={fps.numerator}/{fps.denominator}:start_time=0:round=up,{fit},setsar=1,tpad=stop_mode=clone:stop_duration=1,trim=end_frame={clip['duration_frames']},setpts=N/({fps.numerator}/{fps.denominator}*TB)[{label}]")
            video_labels.append(f"[{label}]")
        if video_labels:
            filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vbase]")
        else:
            filters.append(f"color=c=black:s={width}x{height}:r={fps.numerator}/{fps.denominator}:d={_seconds(duration)},trim=end_frame={frame_count}[vbase]")
        filters.append(f"[vbase]settb=expr={fps.denominator}/{fps.numerator},setpts=N," + ("subtitles=filename=subtitles.ass:fontsdir=fonts" if has_subtitles else "null") + "[vout]")
        filters.append(f"anullsrc=r={sample_rate}:cl=stereo,atrim=end_sample={total_samples}[silence]")
        audio_labels = ["[silence]"]
        for i, clip in enumerate(enabled_clips):
            source = clip["source"]
            asset = assets[source["asset_id"]]
            kind = tracks[clip["track_id"]]["kind"]
            stream_index = source["stream_index"] if kind == "audio" else asset["metadata"].get("audio_stream_index")
            if clip.get("muted", False) or stream_index is None or (kind == "video" and not clip.get("include_source_audio", True)):
                continue
            source_base = rational(source["time_base"])
            start = int(source["in_pts"]) * source_base
            end = int(source["out_pts"]) * source_base
            clip_duration = Fraction(clip["duration_frames"], 1) / fps
            clip_samples = _round_half_up(clip_duration * sample_rate)
            delay = _round_half_up(Fraction(clip["timeline_start_frame"], 1) / fps * sample_rate)
            chain = f"[{indices[clip['id']]}:{stream_index}]atrim=start={_seconds(start)}:end={_seconds(end)},asetpts=PTS-({start.numerator}/{start.denominator})/TB,aresample={sample_rate}:async=1:first_pts=0,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=end_sample={clip_samples},volume={clip.get('gain_db', 0)}dB"
            fade_in = Fraction(clip.get("fade_in_frames", 0), 1) / fps
            fade_out = Fraction(clip.get("fade_out_frames", 0), 1) / fps
            if fade_in:
                chain += f",afade=t=in:st=0:d={_seconds(fade_in)}"
            if fade_out:
                chain += f",afade=t=out:st={_seconds(clip_duration - fade_out)}:d={_seconds(fade_out)}"
            chain += f",adelay={delay}S:all=1[a{i}]"
            filters.append(chain)
            audio_labels.append(f"[a{i}]")
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:normalize=0:duration=longest,atrim=end_sample={total_samples},asetpts=N/SR/TB[aout]")
        (directory / "filters.txt").write_text(";\n".join(filters), encoding="utf-8")
        temporary_output = directory / "output.mp4"
        command += ["-filter_complex_script", "filters.txt", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", f"{fps.numerator}/{fps.denominator}", "-fps_mode", "cfr", "-frames:v", str(frame_count), "-c:a", "aac", "-ar", str(sample_rate), "-ac", "2", "-movflags", "+faststart", str(temporary_output)]
        progress(0.0)
        with (directory / "stderr.txt").open("w+") as error_log:
            process = subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE, stderr=error_log)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + max(120, float(duration) * 20)
            try:
                while process.poll() is None:
                    if cancelled():
                        raise RenderCancelled("渲染已取消")
                    if time.monotonic() > deadline:
                        raise TimeoutError("渲染超过当前阶段的执行时限")
                    for key, _ in selector.select(timeout=0.1):
                        line = key.fileobj.readline().decode("utf-8", "replace").strip()
                        if line.startswith("out_time_us="):
                            try:
                                progress(min(0.99, max(0, int(line.split("=", 1)[1]) / 1e6 / float(duration))))
                            except ValueError:
                                pass
                if cancelled():
                    raise RenderCancelled("渲染已取消")
                if process.returncode != 0:
                    error_log.seek(0)
                    # Private worker diagnostics; API must expose a classified,
                    # sanitized message rather than this absolute-path log.
                    raise RuntimeError("FFmpeg 渲染失败: " + error_log.read()[-3000:])
            finally:
                selector.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
        metadata = probe_media(temporary_output)
        video = _stream({"metadata": metadata}, metadata["video_stream_index"])
        if video["frame_count"] != frame_count or (metadata["width"], metadata["height"]) != (width, height):
            raise ValueError("成片帧数或画幅与冻结工程不一致")
        if cancelled():
            raise RenderCancelled("渲染已取消")
        # Atomic publication without ever replacing an existing render.
        os.link(temporary_output, output_path)
        progress(1.0)
        return metadata
