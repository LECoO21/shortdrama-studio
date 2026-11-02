"""Bounded SiWC connection smoke only: fixed text or two designated JPEG frames.

No tools, arbitrary prompt/URL/file upload, project mutation, automatic retry,
API-key fallback or Codex credential access. A completed mock is never account
entitlement evidence. Real calls require the user's plan-only confirmation.
"""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any

import httpx

from .chatgpt_auth import ChatGPTAuthManager

MODELS_URL = "https://api.openai.com/v1/models"
RESPONSES_URL = "https://api.openai.com/v1/responses"
CLIENT_NAME = "shortdrama_studio"
MAX_ATTEMPTS = 8
MAX_STREAM_BYTES = 512 * 1024
MAX_MODELS_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_FRAME_BYTES = 256 * 1024
SAMPLE_SHA256 = "9d48d2b21522e97a99651f909b003beec32f69360806ed5bfdb33b93f75a50bb"
SAMPLE_ASSET_ID = SAMPLE_SHA256[:24]
_PROJECT = Path(__file__).resolve().parents[2]
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_STATES = {"started", "completed", "failed", "unknown", "cancelled"}
_UNRESOLVED = {"started", "unknown", "cancelled"}
_MEDIA_TYPES = {"text/event-stream", "application/json", "text/html", "text/plain", "application/octet-stream"}
_PUBLIC_EVENTS = {"error", "response.created", "response.queued", "response.in_progress", "response.completed",
                  "response.failed", "response.incomplete", "response.output_item.added", "response.output_item.done",
                  "response.content_part.added", "response.content_part.done", "response.output_text.delta",
                  "response.output_text.done", "response.refusal.delta", "response.refusal.done",
                  "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                  "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                  "response.reasoning_text.delta", "response.reasoning_text.done"}
_TEXT = "这是一次连接测试。请仅用中文回复：连接成功。"
_FRAMES = "这是用户指定测试素材的抽帧。请按顺序简述每张画面中可见的内容，无法确定的内容写不确定。不要猜测人物身份，不要生成操作指令。用简短中文回答。"

# Provider requests carry bearer tokens; never let dependency DEBUG output expose
# headers or base64 frames through the application's root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ChatGPTProbeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str) -> ChatGPTProbeError:
    messages = {
        "plan_confirmation_required": "调用前需确认已关闭 ChatGPT 额外 credits；程序不能代替账号设置核验。",
        "model_not_selected": "请先读取当前账号模型目录，再显式选择一个目录中的模型。",
        "account_not_ready": "请先完成有 ChatGPT 计划权限的账号连接。",
        "request_conflict": "此请求编号已用于不同内容，请核对原请求记录。",
        "result_unknown": "相同请求仍在执行或结果未知；请核对原请求，禁止自动重提。",
        "limit_reached": "本轮最多 8 次服务请求的上限已用完。",
        "cancelled": "测试已取消；已开始的服务请求不会自动重试。",
        "timeout_unknown": "请求超时，服务端结果未知；不会自动重试。",
        "network_unknown": "连接中断，服务端结果未知；不会自动重试。",
        "stream_incomplete": "未收到完整的 response.completed，结果未知；不会自动重试。",
        "invalid_response": "服务响应结构无效或超过限制；不会自动重试。",
        "provider_failed": "模型返回失败或未完成；不会自动重试。",
        "provider_incomplete": "模型返回未完成状态；不会自动重试。",
        "unexpected_tool": "无工具测试中出现了工具调用，已停止且不会执行。",
        "invalid_frames": "抽帧未通过指定素材绑定或 JPEG 大小校验。",
        "sample_unavailable": "指定素材或已入库抽帧缺失、已变化，或路径校验失败。",
        "unsafe_storage": "测试记录必须保存在项目和云同步目录之外的本机私有目录。",
        "storage_invalid": "测试记录不可读取或校验失败，已停止以保留调用计数。",
        "invalid_request_id": "请求编号需为 8–64 位字母、数字、下划线或短横线。",
        "retry_not_allowed": "原请求不符合已确证本地缺失响应头提前拒绝的重试条件。",
        "retry_already_used": "原未知请求已登记过一次关联重试，不能再次提交。",
    }
    return ChatGPTProbeError(code, messages[code])


def _private_directory(supplied: Path) -> Path:
    path = supplied.expanduser().resolve()
    forbidden = (_PROJECT, (Path.home() / ".codex").resolve())
    if (any(path == root or root in path.parents for root in forbidden)
            or any(p in {"Mobile Documents", "com~apple~CloudDocs", "CloudStorage"} for p in path.parts)):
        raise _error("unsafe_storage")
    try:
        if supplied.expanduser().is_symlink():
            raise OSError
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.stat().st_uid != os.getuid() or not path.is_dir():
            raise OSError
        path.chmod(0o700)
    except OSError:
        raise _error("unsafe_storage") from None
    return path


def _read_file(path: Path, limit: int) -> bytes:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            fd = None
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError
            content = source.read(limit + 1)
            if len(content) > limit:
                raise ValueError
            return content
    except (OSError, ValueError):
        raise _error("sample_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Check bounded JPEG SOF dimensions before any remote upload or decode."""
    if not isinstance(data, bytes) or not 16 <= len(data) <= MAX_FRAME_BYTES or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise _error("invalid_frames")
    cursor = 2
    while cursor + 4 <= len(data):
        if data[cursor] != 255:
            break
        while cursor < len(data) and data[cursor] == 255:
            cursor += 1
        if cursor >= len(data):
            break
        marker = data[cursor]
        cursor += 1
        if marker in (0xDA, 0xD9):
            break
        length = int.from_bytes(data[cursor:cursor + 2], "big")
        if length < 2 or cursor + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if length < 8:
                break
            height = int.from_bytes(data[cursor + 3:cursor + 5], "big")
            width = int.from_bytes(data[cursor + 5:cursor + 7], "big")
            if 1 <= width <= 1280 and 1 <= height <= 1280:
                return width, height
            break
        cursor += length
    raise _error("invalid_frames")


@dataclass(frozen=True)
class BoundFrame:
    """Internal loader result; not a browser request or arbitrary path contract."""
    source_sha256: str
    asset_id: str
    asset_version: int
    shot_id: str
    start_pts: str
    jpeg: bytes = field(repr=False)

    def validate(self) -> None:
        if (self.source_sha256 != SAMPLE_SHA256 or self.asset_id != SAMPLE_ASSET_ID
                or self.asset_version != 1 or not re.fullmatch(r"shot-\d{4}", self.shot_id)
                or not re.fullmatch(r"\d{1,8}", self.start_pts) or int(self.start_pts) > 437866):
            raise _error("invalid_frames")
        _jpeg_dimensions(self.jpeg)


def load_designated_frames() -> tuple[BoundFrame, ...]:
    """Read only the fixed imported sample and manifest's first/last JPEG.

    No caller-controlled filesystem or URL input. Does not import storage.py,
    migrate, write the DB, re-extract frames, or access an alternate asset.
    """
    data = _PROJECT / "data"
    database = data / "studio.db"
    original = data / "originals" / f"{SAMPLE_ASSET_ID}.mp4"
    try:
        if database.is_symlink() or original.resolve() != original or not database.is_file():
            raise ValueError
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
            asset = conn.execute("SELECT version,sha256,path FROM assets WHERE id=?", (SAMPLE_ASSET_ID,)).fetchone()
            analysis = conn.execute("SELECT version,manifest_json FROM analyses WHERE asset_id=?", (SAMPLE_ASSET_ID,)).fetchone()
        if (asset is None or analysis is None or asset[0] != 1 or asset[1] != SAMPLE_SHA256
                or asset[2] != str(original) or analysis[0] != 1):
            raise ValueError
        source = _read_file(original, 2 * 1024 * 1024)
        if hashlib.sha256(source).hexdigest() != SAMPLE_SHA256:
            raise ValueError
        manifest = json.loads(analysis[1])
        if (manifest.get("asset_id") != SAMPLE_ASSET_ID or manifest.get("asset_version") != 1
                or manifest.get("source_sha256") != SAMPLE_SHA256 or manifest.get("schema_version") != 1
                or not re.fullmatch(r"[a-f0-9]{32}-a[1-3]", manifest.get("artifact_id", ""))):
            raise ValueError
        shots = manifest["shots"]
        if not isinstance(shots, list) or not 1 <= len(shots) <= 1000:
            raise ValueError
        selected = [shots[0]] if len(shots) == 1 else [shots[0], shots[-1]]
        frames = []
        for shot in selected:
            name = shot["thumbnail"]
            if not re.fullmatch(r"shot-\d{4}\.jpg", name):
                raise ValueError
            path = data / "analysis" / manifest["artifact_id"] / name
            if path.resolve() != path:
                raise ValueError
            frame = BoundFrame(SAMPLE_SHA256, SAMPLE_ASSET_ID, 1, shot["id"], shot["start_pts"],
                               _read_file(path, MAX_FRAME_BYTES))
            frame.validate()
            frames.append(frame)
        return tuple(frames)
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError):
        raise _error("sample_unavailable") from None


class ChatGPTProbe:
    def __init__(self, manager: ChatGPTAuthManager, *, state_dir: Path | None = None,
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self._auth, self._clock, self._monotonic = manager, clock, monotonic
        self._dir = _private_directory(state_dir or Path.home() / "Library/Application Support/Shortdrama Studio/chatgpt-probes")
        self._state_path, self._lock_path = self._dir / "probe-attempts.json", self._dir / "probes.lock"
        self._mutex = threading.RLock()
        self._http = httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                                  timeout=httpx.Timeout(30, connect=10, write=10, pool=5),
                                  headers={"User-Agent": CLIENT_NAME + "/0.1.0"})
        with self._locked():
            if not self._state_path.exists():
                self._write({"version": 1, "attempts": [], "catalog": None})

    @contextmanager
    def _locked(self):
        with self._mutex:
            fd = None
            try:
                fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            except OSError:
                raise _error("storage_invalid") from None
            finally:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def _read(self) -> dict:
        try:
            fd = os.open(self._state_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
                    raise ValueError
                state = json.load(source)
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("attempts"), list) or len(state["attempts"]) > MAX_ATTEMPTS):
                raise ValueError
            for attempt in state["attempts"]:
                if (not isinstance(attempt, dict) or not _REQUEST_ID.fullmatch(attempt.get("request_id", ""))
                        or attempt.get("state") not in _STATES or not _DIGEST.fullmatch(attempt.get("fingerprint", ""))):
                    raise ValueError
            catalog = state.get("catalog")
            if catalog is not None and (not isinstance(catalog, dict)
                    or not _DIGEST.fullmatch(catalog.get("account", ""))
                    or not isinstance(catalog.get("fetched_at"), (int, float))
                    or not math.isfinite(catalog["fetched_at"])
                    or not isinstance(catalog.get("models"), list) or not 1 <= len(catalog["models"]) <= 1024
                    or any(not isinstance(model, str) or not _MODEL.fullmatch(model) for model in catalog["models"])):
                raise ValueError
            return state
        except (OSError, ValueError, TypeError):
            raise _error("storage_invalid") from None

    def _write(self, state: dict) -> None:
        name = None
        try:
            fd, name = tempfile.mkstemp(prefix=".probes-", dir=self._dir)
            with os.fdopen(fd, "w") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(state, target, ensure_ascii=False, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self._state_path)
            directory_fd = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise _error("storage_invalid") from None
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)

    def status(self) -> dict:
        """Local only. Does not access/refresh a bearer or discover models."""
        with self._locked():
            state = self._read()
        catalog = state["catalog"]
        try:
            account = self._account()
            if not isinstance(catalog, dict) or catalog.get("account") != account:
                catalog = None
        except ChatGPTProbeError:
            catalog = None
        return {"request_limit": MAX_ATTEMPTS, "attempts_used": len(state["attempts"]),
                "attempts_remaining": MAX_ATTEMPTS - len(state["attempts"]),
                "attempts": state["attempts"], "catalog": catalog,
                "extra_credits_verified_by_app": False,
                "note": "计数覆盖 /models 与 /responses 的已登记尝试；未知结果不自动重提。"}

    def _account(self) -> str:
        status = self._auth.status()
        account_id = (status.get("account") or {}).get("id")
        if not status.get("connected") or not status.get("plan_usage_enabled") or not isinstance(account_id, str):
            raise _error("account_not_ready")
        return hashlib.sha256(account_id.encode()).hexdigest()

    @staticmethod
    def _check_cancel(cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise _error("cancelled")

    @staticmethod
    def _validate_retry(state: dict, source_id: str | None, identity: dict, fingerprint: str) -> None:
        if source_id is None:
            return
        source = next((a for a in state["attempts"] if a["request_id"] == source_id), None)
        diagnostics = source.get("diagnostics") if source else None
        if (source is None or source.get("retry_of") is not None
                or source.get("state") != "unknown" or source.get("error_code") != "invalid_response"
                or not isinstance(diagnostics, dict) or diagnostics.get("phase") != "sse_content_type"
                or diagnostics.get("content_type") != "absent" or diagnostics.get("http_status") != 200
                or diagnostics.get("received_bytes") != 0 or diagnostics.get("events_received") != 0
                or source.get("fingerprint") != fingerprint
                or any(source.get(key) != value for key, value in identity.items())):
            raise _error("retry_not_allowed")
        if any(a.get("retry_of") == source_id for a in state["attempts"]):
            raise _error("retry_already_used")

    def _begin(self, kind: str, model: str | None, frames: tuple[BoundFrame, ...], request_id: str,
               cancelled: Callable[[], bool], retry_unknown_from: str | None = None) -> tuple[dict, str | None]:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise _error("invalid_request_id")
        if retry_unknown_from is not None and (not isinstance(retry_unknown_from, str)
                or not _REQUEST_ID.fullmatch(retry_unknown_from) or retry_unknown_from == request_id):
            raise _error("retry_not_allowed")
        self._check_cancel(cancelled)
        account = self._account()
        frame_hashes = [hashlib.sha256(frame.jpeg).hexdigest() for frame in frames]
        identity = {"kind": kind, "account": account, "model": model, "frame_sha256": frame_hashes}
        fingerprint = hashlib.sha256(json.dumps({"kind": kind, "account": account, "model": model,
            "frames": frame_hashes}, sort_keys=True).encode()).hexdigest()
        with self._locked():
            state = self._read()
            for previous in state["attempts"]:
                if previous["request_id"] == request_id:
                    if previous["fingerprint"] != fingerprint or (retry_unknown_from is not None
                            and previous.get("retry_of") != retry_unknown_from):
                        raise _error("request_conflict")
                    if previous["state"] in _UNRESOLVED:
                        raise _error("result_unknown")
                    return previous, None
            self._validate_retry(state, retry_unknown_from, identity, fingerprint)
            if any(a["fingerprint"] == fingerprint and a["state"] in _UNRESOLVED
                   and a["request_id"] != retry_unknown_from for a in state["attempts"]):
                raise _error("result_unknown")
            if len(state["attempts"]) >= MAX_ATTEMPTS:
                raise _error("limit_reached")
            catalog = state.get("catalog")
            if kind != "models" and (not isinstance(catalog, dict) or catalog.get("account") != account
                    or not 0 <= self._clock() - catalog.get("fetched_at", 0) <= 1800
                    or model not in catalog.get("models", [])):
                raise _error("model_not_selected")
        token = self._auth.access_token()  # Internal SiWC accessor; never serialized.
        if self._account() != account:
            raise _error("account_not_ready")
        self._check_cancel(cancelled)
        with self._locked():
            # Token refresh may have overlapped another caller; recheck before reserving.
            state = self._read()
            for previous in state["attempts"]:
                if previous["request_id"] == request_id:
                    if previous["fingerprint"] != fingerprint or (retry_unknown_from is not None
                            and previous.get("retry_of") != retry_unknown_from):
                        raise _error("request_conflict")
                    if previous["state"] in _UNRESOLVED:
                        raise _error("result_unknown")
                    return previous, None
            self._validate_retry(state, retry_unknown_from, identity, fingerprint)
            if any(a["fingerprint"] == fingerprint and a["state"] in _UNRESOLVED
                   and a["request_id"] != retry_unknown_from for a in state["attempts"]):
                raise _error("result_unknown")
            if len(state["attempts"]) >= MAX_ATTEMPTS:
                raise _error("limit_reached")
            attempt = {"request_id": request_id, "kind": kind, "model": model, "account": account,
                       "fingerprint": fingerprint, "state": "started", "started_at": self._clock(),
                       "plan_only_confirmed": kind != "models",
                       "frame_sha256": frame_hashes}
            if retry_unknown_from is not None:
                # This links an explicitly authorized new attempt. It is not a
                # repair/reclassification of the original unknown receipt.
                attempt["retry_of"] = retry_unknown_from
            state["attempts"].append(attempt)
            self._write(state)  # Must be durable before any provider HTTP request.
        return attempt, token

    def _finish(self, attempt: dict, outcome: str, *, result: dict | None = None,
                error_code: str | None = None, diagnostics: dict | None = None) -> dict:
        with self._locked():
            state = self._read()
            stored = next(a for a in state["attempts"] if a["request_id"] == attempt["request_id"])
            stored.update({"state": outcome, "finished_at": self._clock()})
            if error_code:
                stored["error_code"] = error_code
            if result is not None:
                stored["result"] = result
            if diagnostics is not None:
                stored["diagnostics"] = dict(diagnostics)
            if outcome == "completed" and attempt["kind"] == "models":
                state["catalog"] = {"account": attempt["account"], "fetched_at": self._clock(),
                                    "models": result["models"], "choices": result["choices"]}
            self._write(state)
            return dict(stored)

    @staticmethod
    def _headers(token: str, request_id: str) -> dict:
        return {"Authorization": "Bearer " + token, "X-Client-Request-Id": request_id,
                "Accept": "text/event-stream"}

    @staticmethod
    def _check_http(response: httpx.Response) -> None:
        if response.status_code != 200:
            code = f"http_{response.status_code}"
            raise ChatGPTProbeError(code, f"官方服务返回 HTTP {response.status_code}；不会自动重试。")

    @staticmethod
    def _response_diagnostics(response: httpx.Response, diagnostics: dict) -> None:
        # Never copy raw headers, event strings, upstream messages or JSON body.
        media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        diagnostics.update({"phase": "http_headers", "http_status": response.status_code,
                            "content_type": media if media in _MEDIA_TYPES else "other" if media else "absent"})

    def models(self, *, request_id: str, cancelled: Callable[[], bool] = lambda: False) -> dict:
        attempt, token = self._begin("models", None, (), request_id, cancelled)
        if token is None:
            return attempt
        diagnostics = {"phase": "http_request", "received_bytes": 0, "chunks_received": 0}
        try:
            with self._http.stream("GET", MODELS_URL, headers=self._headers(token, request_id)) as response:
                self._response_diagnostics(response, diagnostics)
                self._check_http(response)
                diagnostics["phase"] = "catalog_body"
                payload = self._bounded_body(response, cancelled, diagnostics)
            diagnostics["phase"] = "catalog_json"
            data = json.loads(payload)
            diagnostics["phase"] = "catalog_schema"
            # SiWC uses models[].slug/visibility, unlike the API-key model-list
            # shape. Preserve official account-catalog ordering for the default.
            if not isinstance(data, dict) or not isinstance(data.get("models"), list) or len(data["models"]) > 1024:
                raise _error("invalid_response")
            choices, seen = [], set()
            for item in data["models"]:
                if not isinstance(item, dict) or item.get("visibility") != "list":
                    continue
                slug = item.get("slug")
                if not isinstance(slug, str) or not _MODEL.fullmatch(slug) or slug in seen:
                    continue
                seen.add(slug)
                display = item.get("display_name")
                choices.append({"slug": slug, "display_name": display[:100] if isinstance(display, str) else slug})
            models = [item["slug"] for item in choices]
            if not models:
                raise _error("invalid_response")
            diagnostics["phase"] = "local_ledger"
            return self._finish(attempt, "completed", result={"models": models, "choices": choices, "entitlement_verified": False}, diagnostics=diagnostics)
        except (httpx.HTTPError, ChatGPTProbeError, ValueError, TypeError) as exc:
            return self._record_failure(attempt, exc, diagnostics)

    def _bounded_body(self, response: httpx.Response, cancelled: Callable[[], bool], diagnostics: dict) -> bytes:
        body = bytearray()
        deadline = self._monotonic() + 60
        for chunk in response.iter_bytes():
            diagnostics["chunks_received"] += 1
            diagnostics["received_bytes"] += len(chunk)
            self._check_cancel(cancelled)
            if self._monotonic() > deadline:
                raise _error("timeout_unknown")
            body.extend(chunk)
            if len(body) > MAX_MODELS_BYTES:
                raise _error("invalid_response")
        return bytes(body)

    def selected_model(self, model: str | None = None) -> str:
        """Use the first visible account-catalog choice unless explicitly selected."""
        account = self._account()
        with self._locked():
            catalog = self._read().get("catalog")
        if (not isinstance(catalog, dict) or catalog.get("account") != account
                or not 0 <= self._clock() - catalog.get("fetched_at", 0) <= 1800
                or not catalog.get("models")):
            raise _error("model_not_selected")
        selected = catalog["models"][0] if model is None else model
        if not isinstance(selected, str) or not _MODEL.fullmatch(selected) or selected not in catalog["models"]:
            raise _error("model_not_selected")
        return selected

    def text(self, *, model: str | None = None, request_id: str, plan_only_confirmed: bool = False,
             cancelled: Callable[[], bool] = lambda: False, retry_unknown_from: str | None = None) -> dict:
        return self._response("text", model, (), request_id, plan_only_confirmed, cancelled, retry_unknown_from)

    def frames(self, *, model: str | None = None, request_id: str, plan_only_confirmed: bool = False,
               cancelled: Callable[[], bool] = lambda: False, retry_unknown_from: str | None = None) -> dict:
        if plan_only_confirmed is not True:
            raise _error("plan_confirmation_required")
        return self._response("frames", model, load_designated_frames(), request_id, plan_only_confirmed, cancelled, retry_unknown_from)

    def _response(self, kind: str, model: str | None, frames: tuple[BoundFrame, ...], request_id: str,
                  confirmed: bool, cancelled: Callable[[], bool], retry_unknown_from: str | None = None) -> dict:
        if confirmed is not True:
            raise _error("plan_confirmation_required")
        model = self.selected_model(model)
        if kind == "frames":
            if not 1 <= len(frames) <= 2 or len({f.shot_id for f in frames}) != len(frames):
                raise _error("invalid_frames")
            for frame in frames:
                frame.validate()
            if sum(len(f.jpeg) for f in frames) > 2 * MAX_FRAME_BYTES:
                raise _error("invalid_frames")
        content = [{"type": "input_text", "text": _TEXT if kind == "text" else _FRAMES}]
        for frame in frames:
            content.extend([{"type": "input_text", "text": f"{frame.shot_id}，原片 PTS={frame.start_pts}，时间基 1/16000。"},
                            {"type": "input_image", "image_url": "data:image/jpeg;base64," + base64.b64encode(frame.jpeg).decode(), "detail": "low"}])
        # SiWC preview accepts a narrower contract than the general Responses
        # API: do not send max_output_tokens, metadata or conversation fields.
        # The request-count and local stream bounds are not a billing/token cap.
        payload = {"model": model, "input": [{"role": "user", "content": content}],
                   "store": False, "stream": True}
        attempt, token = self._begin(kind, model, frames, request_id, cancelled, retry_unknown_from)
        if token is None:
            return attempt
        diagnostics = {"phase": "http_request", "received_bytes": 0, "chunks_received": 0,
                       "events_received": 0, "last_event_type": "none", "response_completed_seen": False}
        try:
            with self._http.stream("POST", RESPONSES_URL, headers=self._headers(token, request_id), json=payload) as response:
                self._response_diagnostics(response, diagnostics)
                self._check_http(response)
                result = self._completed_sse(response, cancelled, diagnostics)
            diagnostics["phase"] = "local_ledger"
            return self._finish(attempt, "completed", result=result, diagnostics=diagnostics)
        except (httpx.HTTPError, ChatGPTProbeError, ValueError, TypeError) as exc:
            return self._record_failure(attempt, exc, diagnostics)

    def _events(self, response: httpx.Response, cancelled: Callable[[], bool], diagnostics: dict) -> Iterator[dict]:
        diagnostics["phase"] = "sse_content_type"
        # Explicit stream requests in the official SDK decode SSE without a
        # mandatory response header. Permit an absent header, while retaining
        # all body/terminal checks and rejecting an explicit non-SSE media type.
        media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media and media != "text/event-stream":
            raise _error("invalid_response")
        buffer, event_lines, total, count = bytearray(), [], 0, 0
        deadline = self._monotonic() + 60
        diagnostics["phase"] = "stream_read"
        for chunk in response.iter_bytes():
            diagnostics["phase"] = "sse_bytes"
            diagnostics["chunks_received"] += 1
            diagnostics["received_bytes"] += len(chunk)
            self._check_cancel(cancelled)
            if self._monotonic() > deadline:
                raise _error("timeout_unknown")
            total += len(chunk)
            if total > MAX_STREAM_BYTES:
                raise _error("invalid_response")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                line = line.rstrip(b"\r")
                if len(line) > MAX_EVENT_BYTES:
                    raise _error("invalid_response")
                if not line:
                    if event_lines:
                        count += 1
                        if count > 512:
                            raise _error("invalid_response")
                        raw = b"\n".join(event_lines)
                        event_lines = []
                        if raw == b"[DONE]":
                            return
                        diagnostics["phase"] = "sse_json"
                        diagnostics["event_bytes"] = len(raw)
                        event = json.loads(raw)
                        diagnostics["phase"] = "sse_event_schema"
                        if not isinstance(event, dict):
                            diagnostics["schema_issue"] = "event_not_object"
                            raise _error("invalid_response")
                        diagnostics["events_received"] += 1
                        event_type = event.get("type")
                        diagnostics["last_event_type"] = event_type if isinstance(event_type, str) and event_type in _PUBLIC_EVENTS else "other"
                        yield event
                        diagnostics["phase"] = "sse_bytes"
                elif line.startswith(b"data:"):
                    event_lines.append(bytes(line[5:]).lstrip(b" "))
                    if sum(len(item) for item in event_lines) > MAX_EVENT_BYTES:
                        raise _error("invalid_response")
            if len(buffer) > MAX_EVENT_BYTES:
                raise _error("invalid_response")
            diagnostics["phase"] = "stream_read"

    def _completed_sse(self, response: httpx.Response, cancelled: Callable[[], bool], diagnostics: dict) -> dict:
        text, refused = "", False
        for event in self._events(response, cancelled, diagnostics):
            diagnostics["phase"] = "sse_event_schema"
            kind = event.get("type", "")
            if not isinstance(kind, str):
                diagnostics["schema_issue"] = "event_type_not_string"
                raise _error("invalid_response")
            if kind in {"error", "response.failed", "response.incomplete"}:
                diagnostics["phase"] = "provider_terminal"
                provider_error = (event.get("response") or {}).get("error", {}) if isinstance(event.get("response", {}), dict) else {}
                if kind == "error":
                    provider_error = event.get("error", event)
                code = provider_error.get("code") if isinstance(provider_error, dict) else None
                diagnostics["provider_error_present"] = bool(provider_error)
                if isinstance(code, str) and code in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}:
                    diagnostics["provider_code"] = code
                    raise ChatGPTProbeError(code, "ChatGPT 计划用量暂不可用；请在账号设置核对，不会自动重试或购买额外额度。")
                raise _error("provider_incomplete" if kind == "response.incomplete" else "provider_failed")
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call", "computer_call", "mcp_call", "local_shell_call"}:
                raise _error("unexpected_tool")
            if kind == "response.output_text.delta":
                diagnostics["phase"] = "response_delta"
                if not isinstance(event.get("delta"), str):
                    diagnostics["schema_issue"] = "delta_not_string"
                    raise _error("invalid_response")
                text += event["delta"]
                if len(text) > 8000:
                    raise _error("invalid_response")
            if kind in {"response.refusal.delta", "response.refusal.done"}:
                refused = True
            if kind == "response.completed":
                diagnostics["phase"] = "response_completed_schema"
                diagnostics["response_completed_seen"] = True
                completed = event.get("response")
                if not isinstance(completed, dict) or completed.get("status") != "completed":
                    diagnostics["schema_issue"] = "completed_status"
                    raise _error("invalid_response")
                diagnostics["phase"] = "response_output_schema"
                output = completed.get("output", [])
                if not isinstance(output, list) or len(output) > 32:
                    diagnostics["schema_issue"] = "completed_output_not_bounded_array"
                    raise _error("invalid_response")
                completed_text = []
                for item in output:
                    if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
                        raise _error("unexpected_tool")
                    if item.get("type") == "message":
                        parts = item.get("content", [])
                        if not isinstance(parts, list) or len(parts) > 128:
                            diagnostics["schema_issue"] = "message_content_not_bounded_array"
                            raise _error("invalid_response")
                        for part in parts:
                            if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                                diagnostics["schema_issue"] = "output_part_type"
                                raise _error("invalid_response")
                            if part.get("type") == "refusal":
                                refused = True
                            value = part.get("text", part.get("refusal", ""))
                            if not isinstance(value, str):
                                diagnostics["schema_issue"] = "output_text_not_string"
                                raise _error("invalid_response")
                            completed_text.append(value)
                if completed_text:
                    text = "".join(completed_text)
                if len(text) > 8000:
                    raise _error("invalid_response")
                usage = completed.get("usage") or {}
                diagnostics["phase"] = "response_usage_schema"
                if not isinstance(usage, dict):
                    diagnostics["schema_issue"] = "usage_not_object"
                    raise _error("invalid_response")
                safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                              if isinstance(usage.get(key), int) and not isinstance(usage[key], bool) and 0 <= usage[key] <= 100000000}
                smoke_passed = bool(text.strip()) and not refused
                return {"text": text, "usage": safe_usage, "response_completed": True,
                        "refused": refused, "smoke_passed": smoke_passed, "entitlement_verified": smoke_passed,
                        "scope": "仅证明当前账号本次所选模型和输入可完成；未验证 ASR、embedding 或剪辑能力。"}
        diagnostics["phase"] = "stream_terminal_missing"
        raise _error("stream_incomplete")

    def _record_failure(self, attempt: dict, exc: Exception, diagnostics: dict) -> dict:
        if isinstance(exc, ChatGPTProbeError):
            code = exc.code
        elif isinstance(exc, httpx.TimeoutException):
            code = "timeout_unknown"
        elif isinstance(exc, httpx.HTTPError):
            code = "network_unknown"
        else:
            code = "invalid_response"
        if isinstance(exc, json.JSONDecodeError):
            diagnostics.update({"exception_kind": "JSONDecodeError", "json_error_line": exc.lineno,
                                "json_error_column": exc.colno, "json_error_position": exc.pos})
        elif isinstance(exc, UnicodeError):
            diagnostics["exception_kind"] = "UnicodeError"
        elif isinstance(exc, TypeError):
            diagnostics["exception_kind"] = "TypeError"
        elif isinstance(exc, ValueError):
            diagnostics["exception_kind"] = "ValueError"
        elif isinstance(exc, ChatGPTProbeError):
            diagnostics["exception_kind"] = "ProbeError"
        else:
            diagnostics["exception_kind"] = "TransportError"
        diagnostics["failure_class"] = (
            "local_storage" if diagnostics.get("phase") == "local_ledger" else
            "http_rejection" if code.startswith("http_") else
            "provider_terminal" if diagnostics.get("phase") == "provider_terminal" else
            "missing_terminal" if code == "stream_incomplete" else
            "transport" if code in {"timeout_unknown", "network_unknown"} else
            "cancelled" if code == "cancelled" else
            "unexpected_output" if code == "unexpected_tool" else "parse_or_schema")
        outcome = "cancelled" if code == "cancelled" else "unknown" if code in {
            "timeout_unknown", "network_unknown", "stream_incomplete", "invalid_response"} else "failed"
        if diagnostics["failure_class"] == "local_storage":
            outcome = "unknown"
        return self._finish(attempt, outcome, error_code=code, diagnostics=diagnostics)

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
