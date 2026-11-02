"""Local browser connection control; never proxy Codex RPC or return credentials."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import logging
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .chatgpt_auth import ChatGPTAuthError, ChatGPTAuthManager
from .chatgpt_probe import ChatGPTProbe, ChatGPTProbeError

router = APIRouter()
FRONTEND = "http://127.0.0.1:3031/settings/ai"
API_ORIGIN = "http://127.0.0.1:8031"
COOKIE = "studio_chatgpt_attempt"
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class OAuthAccessLogFilter(logging.Filter):
    """Uvicorn otherwise logs authorization codes from the callback query."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            if path.startswith(("/auth/", "/api/v1/chatgpt")):
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(OAuthAccessLogFilter())


@lru_cache(maxsize=1)
def get_manager() -> ChatGPTAuthManager:
    return ChatGPTAuthManager()


def get_probe_status(manager=Depends(get_manager)) -> dict | None:
    """Read existing local evidence only; never create a ledger or call a model."""
    state_dir = Path.home() / "Library/Application Support/Shortdrama Studio/chatgpt-probes"
    path = state_dir / "probe-attempts.json"
    try:
        # ChatGPTProbe's constructor initializes absent state. Do not invoke it
        # when the page is simply checking a never-tested account.
        if not path.exists():
            return {"attempts": []}
        if path.is_symlink():
            return None
        with ChatGPTProbe(manager, state_dir=state_dir) as probe:
            return probe.status()
    except (ChatGPTProbeError, ChatGPTAuthError, OSError):
        return None


def smoke_test_summary(auth: dict, probe: dict | None) -> dict:
    """Project only verification outcomes for the active authorized account."""
    empty = {"state": "not_tested", "has_unknown": False}
    summary = {"text": dict(empty), "frames": dict(empty), "has_unknown": False,
               "record_available": probe is not None}
    account_id = (auth.get("account") or {}).get("id")
    if not auth.get("connected") or not auth.get("plan_usage_enabled") or not isinstance(account_id, str):
        return summary
    if probe is None or not isinstance(probe.get("attempts"), list):
        summary.update({"text": {"state": "unknown", "has_unknown": True},
                        "frames": {"state": "unknown", "has_unknown": True},
                        "has_unknown": True, "record_available": False})
        return summary
    account_hash = hashlib.sha256(account_id.encode()).hexdigest()
    attempts = [item for item in probe["attempts"] if isinstance(item, dict)
                and item.get("account") == account_hash]
    unresolved = {"started", "unknown", "cancelled"}
    summary["has_unknown"] = any(item.get("state") in unresolved for item in attempts)
    for kind in ("text", "frames"):
        matching = [item for item in attempts if item.get("kind") == kind]
        has_unknown = any(item.get("state") in unresolved for item in matching)
        passed = any(item.get("state") == "completed" and isinstance(item.get("result"), dict)
                     and item["result"].get("smoke_passed") is True for item in matching)
        state = ("passed" if passed else "unknown" if has_unknown else "failed" if matching
                 else "not_tested")
        summary[kind] = {"state": state, "has_unknown": has_unknown}
    return summary


class BrowserFlow:
    def __init__(self):
        self.lock = threading.Lock()
        self.tickets: dict[str, float] = {}
        self.last_error: str | None = None

    def issue(self):
        with self.lock:
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v > now}
            if len(self.tickets) >= 4:
                raise ChatGPTAuthError("LOGIN_BUSY", "登录窗口较多，请稍后再试。", 429)
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = now + 60
            self.last_error = None
            return ticket

    def consume(self, ticket):
        with self.lock:
            expiry = self.tickets.pop(ticket, 0)
            return expiry > time.monotonic()


flow = BrowserFlow()


def error_response(exc):
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}},
                        status_code=exc.status_code, headers=PRIVATE_HEADERS)


def require_local_write(request):
    if (request.headers.get("x-studio-client") != "1" or request.headers.get("origin") not in
            (None, "http://127.0.0.1:3031", "http://localhost:3031")):
        raise ChatGPTAuthError("LOCAL_ACCESS_ONLY", "请从本机工作台连接 ChatGPT。", 403)


@router.get("/api/v1/chatgpt/status")
def connection_status(manager=Depends(get_manager), probe_status=Depends(get_probe_status)):
    try:
        result = manager.status()
    except ChatGPTAuthError as exc:
        return error_response(exc)
    pending = result.get("pending_login", False)
    connected = result.get("connected", False)
    state = "connecting" if pending else "connected" if connected else "disconnected"
    if not pending and (result.get("needs_login") or result.get("reason") == "reauth_required"):
        state = "needs_login"
    if flow.last_error and not connected and not pending:
        state = "error"
    account = result.get("account") or {}
    label = account.get("label") or account.get("email")
    smoke = smoke_test_summary(result, probe_status)
    verified = any(smoke[kind]["state"] == "passed" for kind in ("text", "frames"))
    return JSONResponse({"status": state, "account_label": label,
        "plan_usage": bool(result.get("plan_usage_enabled", False)),
        "verified_inference": verified, "smoke_tests": smoke,
        "message": flow.last_error or ("连接已失效，请重新登录。" if state == "needs_login" else
                    "当前账号已有真实接入验证通过，完整素材分析与剪辑尚未开放。" if verified else
                    "账号已连接，AI 真实效果尚未验证。" if connected else
                    "请在官方页面登录；连接本身不会发送素材或开始分析。"),
        "runtime": {"ready": False, "reason": "文字和两帧验证仅检查模型接入，完整素材分析与自动粗剪尚未实现。"}},
        headers=PRIVATE_HEADERS)


@router.post("/api/v1/chatgpt/connect")
def connect(request: Request):
    try:
        require_local_write(request)
        ticket = flow.issue()
        return JSONResponse({"login_url": f"{API_ORIGIN}/auth/chatgpt/start?ticket={ticket}"},
                            headers=PRIVATE_HEADERS)
    except ChatGPTAuthError as exc:
        return error_response(exc)


@router.get("/auth/chatgpt/start")
def start_login(request: Request, manager=Depends(get_manager)):
    ticket = request.query_params.get("ticket", "")
    if request.url.hostname != "127.0.0.1" or not flow.consume(ticket):
        return JSONResponse({"error": {"code": "LOGIN_EXPIRED",
            "message": "登录入口已过期，请返回工作台重新点击连接。"}},
            status_code=400, headers=PRIVATE_HEADERS)
    try:
        login = manager.begin_login()
        url = login["authorization_url"]
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc, parts.path) != (
                "https", "auth.openai.com", "/api/accounts/authorize"):
            raise ChatGPTAuthError("INVALID_LOGIN_ENDPOINT", "无法安全启动登录，请重试。", 500)
        state = parse_qs(parts.query).get("state", [""])[0]
        if not state:
            raise ChatGPTAuthError("INVALID_LOGIN_STATE", "无法安全启动登录，请重试。", 500)
        response = RedirectResponse(url, status_code=303, headers=PRIVATE_HEADERS)
        response.set_cookie(COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/auth")
        return response
    except ChatGPTAuthError as exc:
        flow.last_error = exc.message
        return RedirectResponse(FRONTEND + "?connection=error", status_code=303, headers=PRIVATE_HEADERS)


@router.get("/auth/callback")
def callback(request: Request, manager=Depends(get_manager)):
    states = request.query_params.getlist("state")
    cookie = request.cookies.get(COOKIE, "")
    if (request.url.hostname != "127.0.0.1" or len(states) != 1 or not cookie or
            not secrets.compare_digest(cookie, states[0]) or
            any(len(request.query_params.getlist(k)) != 1 for k in request.query_params)):
        return JSONResponse({"error": {"code": "INVALID_LOGIN_STATE",
            "message": "登录校验未通过，请返回工作台重新连接。"}},
            status_code=400, headers=PRIVATE_HEADERS)
    result = "success"
    try:
        manager.complete_login(dict(request.query_params))
        flow.last_error = None
    except ChatGPTAuthError as exc:
        flow.last_error = exc.message
        result = "error"
    response = RedirectResponse(FRONTEND + f"?connection={result}", status_code=303, headers=PRIVATE_HEADERS)
    response.delete_cookie(COOKIE, path="/auth", httponly=True, samesite="lax")
    return response


@router.post("/api/v1/chatgpt/disconnect")
def disconnect(request: Request, manager=Depends(get_manager)):
    try:
        require_local_write(request)
        with flow.lock:
            flow.tickets.clear()
        result = manager.disconnect()
        flow.last_error = None
        return JSONResponse({"disconnected": True,
            "remote_revocation_confirmed": result.get("remote_revocation_confirmed", False),
            "message": result.get("warning") or "已断开连接。"}, headers=PRIVATE_HEADERS)
    except ChatGPTAuthError as exc:
        return error_response(exc)
