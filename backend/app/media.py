"""Local media discovery and synthetic fixtures; no provider or network calls."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path

import av


def ffmpeg_binary() -> str:
    configured = os.environ.get("FFMPEG_BIN") or os.environ.get("STUDIO_FFMPEG")
    candidate = configured or shutil.which("ffmpeg")
    if not candidate:
        local = Path.home() / ".local/bin/ffmpeg"
        candidate = str(local) if local.is_file() else None
    if not candidate:
        raise RuntimeError("FFmpeg 未安装；请设置 FFMPEG_BIN 指向可执行文件")
    return candidate


def _rational(value: Fraction) -> dict:
    return {"num": value.numerator, "den": value.denominator}


def probe_media(path: Path) -> dict:
    """Decode all audio/video frames, retaining integer presentation timestamps.

    A duration is taken from decoded frames, never estimated from average fps.
    Unsupported zero-duration video frames are rejected instead of fabricating
    their terminal boundary. This intentionally favors correctness over speed
    in the first, size-limited local slice.
    """
    path = Path(path)
    streams = []
    video_index = audio_index = None
    width = height = 0
    first_video_pts: list[str] = []
    starts: list[Fraction] = []
    ends: list[Fraction] = []
    options = {"protocol_whitelist": "file,pipe", "format_whitelist": "mov,matroska,webm,avi,wav,mp3,flac,ogg,mpegts"}
    with av.open(str(path), options=options) as container:
        indices = [s.index for s in container.streams if s.type in {"video", "audio"}]
    for index in indices:
        with av.open(str(path), options=options) as container:
            stream = container.streams[index]
            time_base = Fraction(stream.time_base)
            points: list[int] = []
            terminal = None
            for frame in container.decode(stream):
                if frame.pts is None:
                    raise ValueError("媒体缺少可追溯的呈现时间戳")
                frame_base = Fraction(frame.time_base)
                point = Fraction(frame.pts) * frame_base / time_base
                if point.denominator != 1:
                    raise ValueError("解码时间基与源时间基不相容")
                point = int(point)
                if points and point <= points[-1]:
                    raise ValueError("媒体呈现时间戳没有严格递增")
                points.append(point)
                if stream.type == "audio":
                    frame_duration = Fraction(frame.samples, frame.sample_rate) / time_base
                else:
                    frame_duration = Fraction(frame.duration or 0) * frame_base / time_base
                if frame_duration <= 0:
                    raise ValueError("无法从解码帧确定媒体结束边界")
                # Some containers use millisecond audio timestamps although
                # one decoded audio block is a fractional millisecond. Round
                # the enclosing boundary outward, not the sample clock.
                terminal = point + -(-frame_duration.numerator // frame_duration.denominator)
            if not points or terminal is None:
                continue
            info = {
                "index": index,
                "type": stream.type,
                "codec": stream.codec_context.name,
                "time_base": _rational(time_base),
                "start_pts": str(points[0]),
                "end_pts": str(terminal),
                "frame_count": len(points),
            }
            starts.append(points[0] * time_base)
            ends.append(terminal * time_base)
            if stream.type == "video":
                info.update(width=stream.width, height=stream.height,
                            frame_pts=[str(p) for p in points],
                            is_vfr=len(set(b - a for a, b in zip(points, points[1:]))) > 1)
                if video_index is None:
                    video_index, width, height = index, stream.width, stream.height
                    first_video_pts = info["frame_pts"]
            else:
                info.update(sample_rate=stream.rate, channels=stream.codec_context.channels)
                if audio_index is None:
                    audio_index = index
            streams.append(info)
    if not streams:
        raise ValueError("文件中没有可解码的视频或音频")
    start = min(starts)
    end = max(ends)
    return {
        "probe": f"pyav-{av.__version__}",
        "streams": streams,
        "video_stream_index": video_index,
        "audio_stream_index": audio_index,
        "width": width,
        "height": height,
        "duration_seconds": float(end - start),
        "duration": float(end - start),
        "start_seconds": float(start),
        "end_seconds": float(end),
        "frame_pts": first_video_pts,
    }


def generate_demo_media(directory: Path) -> list[Path]:
    """Create two immutable, three-second local examples (no real footage)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    result = []
    for name, color, tone in [("demo-red.mp4", "0xB54638", 440),
                               ("demo-blue.mp4", "0x2864A4", 660)]:
        output = directory / name
        if not output.exists():
            temporary = directory / (name + f".{uuid.uuid4().hex}.partial.mp4")
            try:
                subprocess.run([
                    ffmpeg_binary(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=25:d=3",
                    "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-movflags", "+faststart", str(temporary),
                ], check=True, capture_output=True, timeout=60)
                probe_media(temporary)
                try:
                    os.link(temporary, output)
                except FileExistsError:
                    # Another caller won publication; never replace an
                    # already-existing original, even for demo assets.
                    pass
            finally:
                temporary.unlink(missing_ok=True)
        result.append(output)
    return result
