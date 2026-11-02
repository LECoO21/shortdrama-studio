"""Bounded, local media derivatives; scene boundaries are rule candidates only.

All returned paths are fixed filenames relative to an otherwise private output
directory. Source timestamps always refer to the original stream, never the
resampled proxy. No ASR, scene description or other model result is fabricated.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
import wave
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import numpy as np

from .media import ffmpeg_binary


MAX_DURATION_SECONDS = 600
TIMEOUT_SECONDS = 300
SAMPLE_INTERVAL_SECONDS = Fraction(1, 2)
SCENE_THRESHOLD = 0.32
WAVEFORM_BIN_SAMPLES = 1600
_INPUT_OPTIONS = {
    "protocol_whitelist": "file,pipe",
    "format_whitelist": "mov,matroska,webm,avi,wav,mp3,flac,ogg,mpegts",
}


class PreprocessCancelled(Exception):
    """Cancellation before publication; partial derivative files are removed."""


def _fraction(value: dict) -> Fraction:
    return Fraction(int(value["num"]), int(value["den"]))


def _run(command: list[str], check: Callable[[], None]) -> None:
    """Poll a single child so cancel/timeout also stop an in-flight encoder."""
    check()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        raise ValueError("无法启动媒体处理程序，请检查 FFmpeg 配置") from None
    try:
        while process.poll() is None:
            check()
            time.sleep(0.05)
        check()
        if process.returncode:
            raise ValueError("媒体预处理失败，请检查原始媒体或 FFmpeg 配置")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def _thumbnail(frame: av.VideoFrame, path: Path) -> None:
    width = min(320, frame.width)
    width = max(2, width // 2 * 2)
    height = max(2, round(frame.height * width / frame.width) // 2 * 2)
    picture = frame.reformat(width=width, height=height, format="yuvj420p")
    picture.pts = 0
    picture.time_base = Fraction(1)
    with av.open(str(path), "w", format="image2") as output:
        stream = output.add_stream("mjpeg", rate=1)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuvj420p"
        for packet in stream.encode(picture):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def _histogram(frame: av.VideoFrame) -> np.ndarray:
    pixels = frame.reformat(width=96, height=54, format="rgb24").to_ndarray()
    # Normalize each channel separately. Half the L1 distance averaged over
    # three channels is bounded by [0, 1], independent of source dimensions.
    return np.concatenate([np.bincount((pixels[:, :, channel] // 16).ravel(),
                                      minlength=16) for channel in range(3)]) / (96 * 54)


def _shots(source: Path, stream_info: dict, directory: Path,
           check: Callable[[], None], progress: Callable[[float], None]) -> list[dict]:
    base = _fraction(stream_info["time_base"])
    expected_start, expected_end = int(stream_info["start_pts"]), int(stream_info["end_pts"])
    shots: list[dict] = []
    previous_histogram = None
    previous_point = None
    next_sample_time = Fraction(0)
    terminal = None
    first_point = None
    with av.open(str(source), options=_INPUT_OPTIONS) as container:
        stream = container.streams[int(stream_info["index"])]
        if stream.type != "video" or Fraction(stream.time_base) != base:
            raise ValueError("源视频流与素材元数据不一致")
        for frame in container.decode(stream):
            check()
            if frame.pts is None or frame.time_base is None:
                raise ValueError("媒体缺少可追溯的呈现时间戳")
            point = Fraction(frame.pts) * Fraction(frame.time_base) / base
            duration = Fraction(frame.duration or 0) * Fraction(frame.time_base) / base
            if point.denominator != 1 or duration <= 0:
                raise ValueError("无法确定源视频帧的时间边界")
            point = int(point)
            if previous_point is not None and point <= previous_point:
                raise ValueError("源视频呈现时间戳没有严格递增")
            previous_point = point
            first_point = point if first_point is None else first_point
            terminal = point + -(-duration.numerator // duration.denominator)
            if (point - expected_start) * base > MAX_DURATION_SECONDS:
                raise ValueError("本阶段仅支持不超过 10 分钟的单个素材")
            elapsed = (point - expected_start) * base
            if elapsed < next_sample_time:
                continue
            histogram = _histogram(frame)
            difference = (float(np.abs(histogram - previous_histogram).sum()) / 6
                          if previous_histogram is not None else None)
            if not shots or difference > SCENE_THRESHOLD:
                if shots:
                    shots[-1]["end_pts"] = str(point)
                number = len(shots) + 1
                thumbnail = f"shot-{number:04d}.jpg"
                _thumbnail(frame, directory / thumbnail)
                shots.append({"id": f"shot-{number:04d}", "start_pts": str(point),
                              "end_pts": str(expected_end), "thumbnail": thumbnail,
                              "boundary_status": "candidate", "boundary_score": difference,
                              "description_status": "unknown", "description": None})
            previous_histogram = histogram
            # Anchor to the source start rather than the previous sampled
            # frame: fractional frame rates/VFR must not accumulate drift.
            next_sample_time = (elapsed // SAMPLE_INTERVAL_SECONDS + 1) * SAMPLE_INTERVAL_SECONDS
            progress(0.1 + 0.3 * min(1, (point - expected_start) / max(1, expected_end - expected_start)))
    if not shots or first_point != expected_start or terminal != expected_end:
        raise ValueError("源视频解码边界与素材元数据不一致，请重新导入素材")
    return shots


def _waveform(path: Path, check: Callable[[], None]) -> tuple[list[float], int]:
    peaks = []
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("音频预处理结果格式不正确")
        sample_count = audio.getnframes()
        if sample_count > MAX_DURATION_SECONDS * 16000 + 16000:
            raise ValueError("音频预处理结果超出时长限制")
        while data := audio.readframes(WAVEFORM_BIN_SAMPLES):
            check()
            values = np.frombuffer(data, dtype="<i2").astype(np.int32)
            peaks.append(round(float(np.max(np.abs(values))) / 32768, 5))
    return peaks, sample_count


def prepare_media(asset: dict, output_dir: Path, *,
                  cancelled: Callable[[], bool] = lambda: False,
                  progress: Callable[[float], None] = lambda _: None) -> dict:
    """Create proxy, candidate-shot thumbnails and 16 kHz audio derivatives.

    The original hash is checked before decoding. Each operation is bounded by
    a five-minute job deadline. Callers should give every attempt a private
    directory; existing derivatives are deliberately never overwritten.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS

    def check():
        if cancelled():
            raise PreprocessCancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("媒体预处理超时，请缩短素材后重试")

    check()
    progress(0)
    try:
        source = Path(asset["path"]).resolve(strict=True)
    except OSError:
        raise ValueError("原始媒体不存在或无法读取") from None
    if not source.is_file():
        raise ValueError("原始媒体不存在")
    metadata = asset["metadata"]
    streams = metadata["streams"]
    supported = [s for s in streams if s["type"] in {"video", "audio"}]
    if not supported:
        raise ValueError("素材中没有可解码的视频或音频")
    starts = [int(s["start_pts"]) * _fraction(s["time_base"]) for s in supported]
    ends = [int(s["end_pts"]) * _fraction(s["time_base"]) for s in supported]
    if any(end <= start for start, end in zip(starts, ends)):
        raise ValueError("素材时间边界不正确")
    if max(ends) - min(starts) > MAX_DURATION_SECONDS:
        raise ValueError("本阶段仅支持不超过 10 分钟的单个素材")
    digest = hashlib.sha256()
    with source.open("rb") as original:
        while chunk := original.read(1024 * 1024):
            check()
            digest.update(chunk)
    source_hash = digest.hexdigest()
    if asset.get("sha256") and asset["sha256"] != source_hash:
        raise ValueError("原始媒体完整性校验失败")
    video = next((s for s in streams if s["index"] == metadata.get("video_stream_index")
                  and s["type"] == "video"), None)
    audio = next((s for s in streams if s["index"] == metadata.get("audio_stream_index")
                  and s["type"] == "audio"), None)
    if not video and not audio:
        raise ValueError("素材主音视频流不存在")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    published: list[Path] = []
    manifest = {"schema_version": 1, "asset_id": asset["id"],
                "asset_version": asset["version"], "source_sha256": source_hash,
                "status": "ready", "method": "local-histogram-candidate-v1",
                "scene_detection": {"status": "candidate", "sample_interval_seconds": 0.5,
                                    "histogram_threshold": SCENE_THRESHOLD},
                "proxy": None, "video": None, "audio": None, "shots": [],
                "waveform": [], "waveform_bin_seconds": WAVEFORM_BIN_SAMPLES / 16000,
                "transcript": {"status": "not_configured", "segments": []}}
    ffmpeg = [ffmpeg_binary(), "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
              "-protocol_whitelist", "file,pipe", "-format_whitelist",
              _INPUT_OPTIONS["format_whitelist"], "-i", str(source)]
    try:
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=directory) as temporary:
            work = Path(temporary)
            progress(0.1)
            if video:
                base = _fraction(video["time_base"])
                manifest["video"] = {"stream_index": video["index"], "time_base": video["time_base"],
                                     "start_pts": video["start_pts"], "end_pts": video["end_pts"]}
                manifest["shots"] = _shots(source, video, work, check, progress)
                progress(0.4)
                command = ffmpeg + ["-map", f"0:{video['index']}", "-vf",
                    "setpts=PTS-STARTPTS,scale=w='min(640,trunc(iw/2)*2)':h=-2,fps=15",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p"]
                if audio:
                    offset = int(audio["start_pts"]) * _fraction(audio["time_base"]) - int(video["start_pts"]) * base
                    command += ["-map", f"0:{audio['index']}", "-af",
                                f"asetpts=PTS-STARTPTS+({offset.numerator}/{offset.denominator})/TB,aresample=async=1:first_pts=0",
                                "-c:a", "aac", "-b:a", "64k", "-ac", "1"]
                else:
                    command += ["-an"]
                duration = (int(video["end_pts"]) - int(video["start_pts"])) * base
                command += ["-t", f"{float(duration):.12f}", "-movflags", "+faststart", str(work / "proxy.mp4")]
                _run(command, check)
                with av.open(str(work / "proxy.mp4")) as proxy:
                    stream = proxy.streams.video[0]
                    manifest["proxy"] = {"path": "proxy.mp4", "width": stream.width,
                                         "height": stream.height, "source_start_pts": video["start_pts"],
                                         "source_time_base": video["time_base"]}
            progress(0.75)
            if audio:
                _run(ffmpeg + ["-map", f"0:{audio['index']}", "-vn", "-af", "asetpts=PTS-STARTPTS",
                               "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(work / "audio.wav")], check)
                waveform, sample_count = _waveform(work / "audio.wav", check)
                manifest["waveform"] = waveform
                manifest["audio"] = {"path": "audio.wav", "sample_rate": 16000, "channels": 1,
                                     "sample_count": sample_count, "source_stream_index": audio["index"],
                                     "time_base": audio["time_base"], "start_pts": audio["start_pts"],
                                     "end_pts": audio["end_pts"]}
            progress(0.95)
            check()
            for generated in sorted(work.iterdir()):
                check()
                destination = directory / generated.name
                # Hard-link publication is create-only, including against a
                # concurrent attempt or a symlink to an original source file.
                try:
                    os.link(generated, destination)
                except FileExistsError:
                    raise ValueError("预处理目录已存在产物，请使用独立任务目录") from None
                published.append(destination)
            check()
            progress(1)
        return manifest
    except Exception as failure:
        for path in published:
            path.unlink(missing_ok=True)
        if isinstance(failure, av.FFmpegError):
            raise ValueError("媒体预处理解码失败，请检查原始媒体") from None
        raise
