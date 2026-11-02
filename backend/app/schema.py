from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Ratio(StrictModel):
    num: int = Field(gt=0, le=1_000_000_000)
    den: int = Field(gt=0, le=1_000_000_000)


class Canvas(StrictModel):
    width: int = Field(ge=64, le=1920, multiple_of=2)
    height: int = Field(ge=64, le=1920, multiple_of=2)


class Source(StrictModel):
    asset_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    stream_index: int = Field(ge=0)
    time_base: Ratio
    in_pts: str = Field(pattern=r"^-?\d{1,20}$")
    out_pts: str = Field(pattern=r"^-?\d{1,20}$")

    @model_validator(mode="after")
    def bounds(self):
        if int(self.out_pts) <= int(self.in_pts):
            raise ValueError("源出点必须大于入点")
        return self


class Track(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["video", "audio", "subtitle"]
    enabled: bool = True


class Clip(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    track_id: str
    source: Source
    timeline_start_frame: int = Field(ge=0)
    duration_frames: int = Field(gt=0)
    gain_db: float = Field(default=0.0, ge=-60, le=12)
    muted: bool = False
    fade_in_frames: int = Field(default=0, ge=0)
    fade_out_frames: int = Field(default=0, ge=0)
    fit: Literal["contain", "cover"] = "contain"
    include_source_audio: bool = True

    @model_validator(mode="after")
    def fades(self):
        if self.fade_in_frames + self.fade_out_frames > self.duration_frames:
            raise ValueError("淡入淡出不能超过片段时长")
        return self


class Subtitle(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    track_id: str
    text: str = Field(min_length=1, max_length=1000)
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    font_size: int = Field(default=28, ge=10, le=120)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    bottom_percent: float = Field(default=8.0, ge=0, le=30)


class Document(StrictModel):
    schema_version: Literal[1] = 1
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    canvas: Canvas
    fps: Ratio
    sample_rate: Literal[48000] = 48000
    duration_frames: int = Field(gt=0, le=108_000)
    tracks: list[Track] = Field(min_length=1, max_length=3)
    clips: list[Clip] = Field(max_length=200)
    subtitles: list[Subtitle] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def structure(self):
        fps = Fraction(self.fps.num, self.fps.den)
        if not 1 <= fps <= 120:
            raise ValueError("当前阶段工程帧率支持 1–120 fps")
        if Fraction(self.duration_frames, 1) / fps > 600:
            raise ValueError("本地验证工程最长 10 分钟")
        for rows in [self.tracks, self.clips, self.subtitles]:
            if len({r.id for r in rows}) != len(rows):
                raise ValueError("同类对象 ID 不可重复")
        if len({t.kind for t in self.tracks}) != len(self.tracks):
            raise ValueError("当前阶段每类轨道限一条")
        tracks = {t.id: t.kind for t in self.tracks}
        for c in self.clips:
            if tracks.get(c.track_id) not in ("video", "audio"):
                raise ValueError("片段必须引用视频或音频轨道")
            if c.timeline_start_frame + c.duration_frames > self.duration_frames:
                raise ValueError("片段超出工程范围")
        for sub in self.subtitles:
            if tracks.get(sub.track_id) != "subtitle":
                raise ValueError("字幕必须引用字幕轨道")
            if not sub.start_frame < sub.end_frame <= self.duration_frames:
                raise ValueError("字幕超出工程范围")
        return self


class SaveRequest(StrictModel):
    document: Document
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100)


class CreateProjectRequest(StrictModel):
    name: str = Field(default="未命名剪辑", min_length=1, max_length=200)
    canvas: Canvas = Field(default_factory=lambda: Canvas(width=640, height=360))
    fps: Ratio = Field(default_factory=lambda: Ratio(num=25, den=1))
    request_id: str = Field(min_length=8, max_length=100)

    @model_validator(mode="after")
    def frame_rate(self):
        if not 1 <= Fraction(self.fps.num, self.fps.den) <= 120:
            raise ValueError("当前阶段工程帧率支持 1–120 fps")
        return self


class RevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=100)


class RestoreRequest(RevisionRequest):
    revision: int = Field(ge=1)
