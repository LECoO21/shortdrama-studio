"""Opt-in Beijing Bailian adapters; no upload, storage provisioning or DB side effects.

Only a trusted worker may supply an ASR URL, after binding it to a known asset.
Never expose submit_asr as a browser-controlled URL-fetching endpoint.
References (checked 2026-10-23):
https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api
https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api
https://help.aliyun.com/zh/model-studio/vision
https://help.aliyun.com/zh/model-studio/qwen-structured-output
"""
import base64
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger("studio.models")
# The worker enables root INFO logging. httpx logs complete request URLs at
# INFO, including short-lived ASR result signatures; httpcore can also expose
# transport details at DEBUG. Keep dependency warnings/errors, and keep our
# sanitized business errors/usage on their independent logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
PROMPT_VERSION = "shot_description_v1"
_RESULT_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ModelServiceError(RuntimeError):
    """Sanitized error safe to show without including provider input or response."""


class ModelsUnavailable(ModelServiceError):
    pass


class ModelCallCancelled(ModelServiceError):
    pass


class ASRSubmissionUnknown(ModelServiceError):
    """Submission may have been accepted; must not automatically submit again."""


@dataclass(frozen=True)
class ModelServiceConfig:
    enabled: bool = False
    api_key: str = field(default="", repr=False)
    workspace_id: str = ""
    vision_model: str = "qwen3-vl-plus"
    embedding_model: str = "text-embedding-v4"
    asr_model: str = "paraformer-v2"
    audio_hosts: tuple[str, ...] = ()
    timeout_seconds: float = 60

    @classmethod
    def from_env(cls):
        try:
            timeout = float(os.getenv("STUDIO_MODEL_TIMEOUT_SECONDS", "60"))
        except (TypeError, ValueError):
            raise ModelsUnavailable("模型请求超时配置无效，需为 1–120 秒") from None
        if not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise ModelsUnavailable("模型请求超时配置无效，需为 1–120 秒")
        return cls(
            enabled=(os.getenv("STUDIO_MODEL_PROVIDER", "chatgpt") == "bailian" and
                     os.getenv("STUDIO_MODELS_ENABLED", "").lower() in ("1", "true")),
            api_key=os.getenv("DASHSCOPE_API_KEY", ""),
            workspace_id=os.getenv("STUDIO_BAILIAN_WORKSPACE_ID", ""),
            vision_model=os.getenv("STUDIO_VISION_MODEL", "qwen3-vl-plus"),
            embedding_model=os.getenv("STUDIO_EMBEDDING_MODEL", "text-embedding-v4"),
            asr_model=os.getenv("STUDIO_ASR_MODEL", "paraformer-v2"),
            audio_hosts=tuple(x.strip().lower() for x in
                              os.getenv("STUDIO_ASR_AUDIO_HOSTS", "").split(",") if x.strip()),
            timeout_seconds=timeout,
        )

    def require_enabled(self):
        if not self.enabled:
            raise ModelsUnavailable("云模型未启用，未向第三方发送素材")
        if not self.api_key:
            raise ModelsUnavailable("尚未配置百炼 API Key")
        if self.workspace_id and not re.fullmatch(r"[A-Za-z0-9-]{1,100}", self.workspace_id):
            raise ModelsUnavailable("百炼北京业务空间配置无效")
        if any(not _IDENTIFIER.fullmatch(x) for x in
               (self.vision_model, self.embedding_model, self.asr_model)):
            raise ModelsUnavailable("模型版本配置无效")
        if not 1 <= self.timeout_seconds <= 120:
            raise ModelsUnavailable("模型请求超时配置无效")

    @property
    def api_origin(self):
        host = (f"{self.workspace_id}.cn-beijing.maas.aliyuncs.com"
                if self.workspace_id else "dashscope.aliyuncs.com")
        return f"https://{host}"

    def public_status(self):
        """No credential, signed URL or user text is exposed by configuration status."""
        return {"enabled": self.enabled, "configured": self.enabled and bool(self.api_key),
                "provider": "阿里云百炼（北京接入）", "api_origin": self.api_origin,
                "models": {"vision": self.vision_model, "embedding": self.embedding_model,
                           "asr": self.asr_model}, "embedding_dimensions": 1024,
                "asr_audio_host_configured": bool(self.audio_hosts)}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PersonCandidate(_StrictModel):
    description: str = Field(min_length=1, max_length=300)
    confidence: float = Field(ge=0, le=1)


class ShotDescription(_StrictModel):
    scene: str = Field(min_length=1, max_length=1000)
    people_candidates: list[PersonCandidate] = Field(max_length=20)
    actions: list[str] = Field(max_length=30)
    tags: list[str] = Field(max_length=30)
    unknowns: list[str] = Field(max_length=30)

    @model_validator(mode="after")
    def text_limits(self):
        if any(not x.strip() or len(x) > 300 for x in self.actions + self.tags + self.unknowns):
            raise ValueError("镜头描述条目长度无效")
        return self


class ASRWord(_StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(max_length=1000)
    punctuation: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def bounds(self):
        if self.end_ms < self.start_ms:
            raise ValueError("词时间范围无效")
        return self


class ASRSegment(_StrictModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=10000)
    speaker_id: int | None = Field(default=None, ge=0)
    channel_id: int = Field(default=0, ge=0)
    words: list[ASRWord] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def bounds(self):
        if self.end_ms <= self.start_ms or any(
                w.start_ms < self.start_ms or w.end_ms > self.end_ms for w in self.words):
            raise ValueError("句词时间范围无效")
        return self


class CallMetadata(_StrictModel):
    provider: str
    requested_model: str
    returned_model: str | None
    elapsed_ms: int
    usage: dict[str, int | float]
    prompt_version: str | None = None


class VisionResult(_StrictModel):
    description: ShotDescription
    metadata: CallMetadata


class EmbeddingResult(_StrictModel):
    vectors: list[list[float]]
    metadata: CallMetadata


class ASRSubmission(_StrictModel):
    task_id: str
    metadata: CallMetadata


class ASRResult(_StrictModel):
    segments: list[ASRSegment]
    metadata: CallMetadata


def _https_url(url: str, hosts: tuple[str, ...]):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in hosts or parts.port not in (None, 443)
                or parts.username or parts.password or parts.fragment or not parts.path
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError()
        return url
    except (ValueError, TypeError):
        raise ModelServiceError("远程文件地址不在已配置的 HTTPS 白名单内") from None


def _check_cancel(cancelled):
    if cancelled():
        raise ModelCallCancelled("模型任务已停止等待；已受理的云端任务可能仍会计费")


def _pause(seconds, cancelled):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _check_cancel(cancelled)
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


class BailianModelServices:
    def __init__(self, config: ModelServiceConfig | None = None, *, transport=None):
        self.config = config or ModelServiceConfig.from_env()
        self._client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False,
                                    timeout=httpx.Timeout(self.config.timeout_seconds, connect=10))

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._client.close()

    def _request(self, method, path, *, payload=None, authenticated=True, retry=False,
                 cancelled=lambda: False, deadline=None):
        self.config.require_enabled()
        url = self.config.api_origin + path if authenticated else path
        headers = {"Authorization": f"Bearer {self.config.api_key}"} if authenticated else {}
        if path.endswith("/transcription"):
            headers["X-DashScope-Async"] = "enable"
        for attempt in range(3 if retry else 1):
            _check_cancel(cancelled)
            remaining = (deadline - time.monotonic()) if deadline is not None else self.config.timeout_seconds
            if remaining <= 0:
                raise ModelServiceError("ASR 等待超时；保留云任务 ID 后可继续查询，无需重新提交")
            request_deadline = time.monotonic() + min(remaining, self.config.timeout_seconds)
            try:
                with self._client.stream(method, url, json=payload, headers=headers,
                                         timeout=min(remaining, self.config.timeout_seconds)) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        if retry and attempt < 2:
                            _pause(.25 * 2 ** attempt, cancelled)
                            continue
                    if not 200 <= response.status_code < 300:
                        raise ModelServiceError(f"百炼服务请求失败（HTTP {response.status_code}）")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        _check_cancel(cancelled)
                        if time.monotonic() > request_deadline:
                            raise ModelServiceError("百炼服务响应超时")
                        body.extend(chunk)
                        if len(body) > 8 * 1024 * 1024:
                            raise ModelServiceError("百炼返回数据超过当前处理上限")
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise ValueError()
                    return data
            except httpx.TransportError:
                if retry and attempt < 2:
                    _pause(.25 * 2 ** attempt, cancelled)
                    continue
                raise ModelServiceError("百炼服务连接失败或超时") from None
            except (ValueError, UnicodeError):
                raise ModelServiceError("百炼返回了无效 JSON 数据") from None
        raise ModelServiceError("百炼请求重试次数已用完")

    def _metadata(self, model, started, data, *, prompt=None):
        raw_usage = data.get("usage", {})
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        usage = {str(k): v for k, v in raw_usage.items()
                 if isinstance(k, str) and re.fullmatch(r"[a-z_]{1,50}", k)
                 and type(v) in (int, float) and math.isfinite(v) and v >= 0}
        returned = data.get("model")
        returned = returned if isinstance(returned, str) and _IDENTIFIER.fullmatch(returned) else None
        meta = CallMetadata(provider="bailian-beijing", requested_model=model, returned_model=returned,
                            elapsed_ms=round((time.monotonic() - started) * 1000), usage=usage,
                            prompt_version=prompt)
        logger.info("model_done model=%s returned_model=%s elapsed_ms=%s usage=%s",
                    meta.requested_model, meta.returned_model, meta.elapsed_ms, meta.usage)
        return meta

    def describe_shot(self, jpeg: bytes, *, cancelled=lambda: False) -> VisionResult:
        if not isinstance(jpeg, bytes) or not jpeg.startswith(b"\xff\xd8\xff") or len(jpeg) > 4 * 1024 * 1024:
            raise ModelServiceError("视觉输入必须是大小不超过 4 MiB 的 JPEG 抽帧")
        prompt = (Path(__file__).parent / "prompts" / "shot_description_v1.txt").read_text()
        started = time.monotonic()
        data = self._request("POST", "/compatible-mode/v1/chat/completions", payload={
            "model": self.config.vision_model, "enable_thinking": False,
            "max_tokens": 2000, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," +
                                                        base64.b64encode(jpeg).decode("ascii")}},
                {"type": "text", "text": "仅描述本张采样帧可见的证据，返回 JSON。"}]}],
        }, cancelled=cancelled)
        try:
            choice = data["choices"][0]
            if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                raise ValueError()
            description = ShotDescription.model_validate_json(choice["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise ModelServiceError("视觉描述未通过结构校验，未保存为有效证据") from None
        return VisionResult(description=description, metadata=self._metadata(
            self.config.vision_model, started, data, prompt=PROMPT_VERSION))

    def embed_texts(self, texts: list[str], *, cancelled=lambda: False) -> EmbeddingResult:
        if not isinstance(texts, list) or not 1 <= len(texts) <= 10 or any(
                not isinstance(x, str) or not x.strip() or len(x) > 8000 for x in texts):
            raise ModelServiceError("向量输入需为 1–10 条非空文本，每条不超过 8000 字符")
        started = time.monotonic()
        data = self._request("POST", "/compatible-mode/v1/embeddings", payload={
            "model": self.config.embedding_model, "input": texts,
            "dimensions": 1024, "encoding_format": "float"}, cancelled=cancelled)
        try:
            rows = data["data"]
            if len(rows) != len(texts) or sorted(row["index"] for row in rows) != list(range(len(texts))):
                raise ValueError()
            vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
            if any(not isinstance(v, list) or len(v) != 1024 or any(
                    type(x) not in (float, int) or not math.isfinite(x) for x in v) for v in vectors):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ModelServiceError("向量返回维度或顺序无效，需要 1024 维有限数值") from None
        return EmbeddingResult(vectors=vectors, metadata=self._metadata(self.config.embedding_model, started, data))

    def submit_asr(self, audio_url: str, *, cancelled=lambda: False) -> ASRSubmission:
        self.config.require_enabled()
        if not self.config.audio_hosts:
            raise ModelsUnavailable("ASR 需要已授权、可公网访问的音频 URL 及域名白名单；不会自动上传原片")
        _https_url(audio_url, self.config.audio_hosts)
        started = time.monotonic()
        # Never retry this paid, non-idempotent submission. Worker must durably
        # mark submitting BEFORE calling and persist task_id BEFORE polling.
        try:
            data = self._request("POST", "/api/v1/services/audio/asr/transcription", payload={
                "model": self.config.asr_model, "input": {"file_urls": [audio_url]},
                "parameters": {"channel_id": [0], "language_hints": ["zh", "en"],
                               "timestamp_alignment_enabled": True, "diarization_enabled": True},
            }, cancelled=cancelled)
            task_id = data["output"]["task_id"]
            if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
                raise ValueError()
        except ModelCallCancelled:
            raise
        except (ModelServiceError, KeyError, TypeError, ValueError):
            raise ASRSubmissionUnknown("ASR 提交结果未确认；请核查云端任务，禁止自动重复提交") from None
        return ASRSubmission(task_id=task_id, metadata=self._metadata(self.config.asr_model, started, data))

    def poll_asr(self, task_id: str, *, cancelled: Callable[[], bool] = lambda: False,
                 timeout_seconds: float = 600, interval_seconds: float = 2) -> ASRResult:
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
            raise ModelServiceError("ASR 任务 ID 无效")
        if not 0 < timeout_seconds <= 3600 or not 0 <= interval_seconds <= 30:
            raise ModelServiceError("ASR 轮询时限无效")
        started = time.monotonic()
        deadline = started + timeout_seconds
        while time.monotonic() - started < timeout_seconds:
            _check_cancel(cancelled)
            # Current HTTP reference/table and Python example both use POST.
            # Query is read-only and can be retried without re-submitting audio.
            data = self._request("POST", "/api/v1/tasks/" + task_id, retry=True,
                                 cancelled=cancelled, deadline=deadline)
            try:
                output = data["output"]
                status = output["task_status"]
                if status in ("PENDING", "RUNNING"):
                    _pause(min(interval_seconds, max(0, timeout_seconds - (time.monotonic() - started))), cancelled)
                    continue
                if status != "SUCCEEDED":
                    raise ModelServiceError("云端 ASR 任务失败或已取消")
                results = output["results"]
                if len(results) != 1 or results[0]["subtask_status"] != "SUCCEEDED":
                    raise ModelServiceError("云端 ASR 子任务未成功")
                result_url = _https_url(results[0]["transcription_url"], (_RESULT_HOST,))
                result = self._request("GET", result_url, authenticated=False,
                                       retry=True, cancelled=cancelled, deadline=deadline)
                segments = []
                for transcript in result["transcripts"]:
                    for sentence in transcript["sentences"]:
                        segments.append(ASRSegment.model_validate({
                            "start_ms": sentence["begin_time"], "end_ms": sentence["end_time"],
                            "text": sentence["text"], "speaker_id": sentence.get("speaker_id"),
                            "channel_id": transcript["channel_id"], "words": [
                                {"start_ms": w["begin_time"], "end_ms": w["end_time"],
                                 "text": w["text"], "punctuation": w.get("punctuation", "")}
                                for w in sentence.get("words", [])]}))
                return ASRResult(segments=segments, metadata=self._metadata(self.config.asr_model, started, data))
            except (KeyError, IndexError, TypeError, ValueError, ValidationError):
                raise ModelServiceError("ASR 返回数据未通过句词时间范围校验") from None
        raise ModelServiceError("ASR 等待超时；保留云任务 ID 后可继续查询，无需重新提交")
