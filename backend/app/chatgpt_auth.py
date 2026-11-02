"""Application-owned local SiWC OAuth. Never reads Codex's credential cache.

Only the four methods returning public dictionaries may be exposed to a browser.
``access_token`` is an internal credential accessor, never a product API response.
Official contract: developers.openai.com/siwc/token-sharing-open-source/sign-in.
"""
from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlencode
import uuid

import httpx
import jwt


ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
REVOCATION_URL = ISSUER + "/api/accounts/oauth/revoke"
REDIRECT_URI = "http://127.0.0.1:8031/auth/callback"
RESOURCE = "https://api.openai.com/v1"
USAGE_URL = "https://chatgpt.com/settings/usage"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPES = {"resource.invoke", "chatgpt.tokens.use.direct"}
PENDING_SECONDS = 600
_REVOKE_REQUEST_SECONDS = 4
_REVOKE_TOTAL_SECONDS = 8
_MAX_RESPONSE_BYTES = 1024 * 1024
_CREDENTIAL_KEYS = ("access_token", "refresh_token", "id_token", "expires_at", "scopes", "nonce")


class ChatGPTAuthError(Exception):
    """Safe error: no upstream body, credentials, authorization code or callback."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ChatGPTAuthManager:
    """One local host, separately persisted account/client registrations.

    Pending PKCE attempts deliberately live only in memory and expire after ten
    minutes. A restart requires a fresh login. Filesystem locks serialize token
    rotation across processes; a thread lock also protects pending callbacks.
    """

    def __init__(
        self,
        state_dir: Path | str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, Any] | None = None
        self._unverified_client_id: str | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_until = 0.0
        supplied = Path(state_dir) if state_dir is not None else (
            Path.home() / "Library/Application Support/Shortdrama Studio/chatgpt"
        )
        self._state_dir = self._private_state_dir(supplied)
        self._state_path = self._state_dir / "accounts.json"
        self._lock_path = self._state_dir / "credentials.lock"
        self._http = httpx.Client(
            transport=transport, timeout=15, trust_env=False, follow_redirects=False,
        )
        with self._locked_state():
            if not self._state_path.exists():
                self._write_state({"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()),
                                   "active_account_id": None, "accounts": {}})

    @staticmethod
    def _private_state_dir(supplied: Path) -> Path:
        path = supplied.expanduser().resolve()
        project = Path(__file__).resolve().parents[2]
        codex_home = (Path.home() / ".codex").resolve()
        if (path == project or project in path.parents or
                path == codex_home or codex_home in path.parents or
                any(part in {"Mobile Documents", "com~apple~CloudDocs", "CloudStorage"}
                    for part in path.parts)):
            raise ChatGPTAuthError("unsafe_storage", "ChatGPT 凭据必须保存在项目和云同步目录之外。")
        try:
            if supplied.expanduser().is_symlink():
                raise OSError("symlink")
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
            if path.stat().st_uid != os.getuid() or not path.is_dir():
                raise OSError("ownership")
            path.chmod(0o700)
        except OSError:
            raise ChatGPTAuthError("unsafe_storage", "无法创建安全的本机凭据目录。", 503) from None
        return path

    @contextmanager
    def _locked_state(self):
        with self._lock:
            fd = None
            try:
                fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("unsafe lock")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            except OSError:
                raise ChatGPTAuthError("credential_storage_unavailable", "本机凭据存储不可用。", 503) from None
            finally:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    def _read_state(self) -> dict[str, Any]:
        try:
            fd = os.open(self._state_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                        info.st_mode & 0o077 or info.st_size > _MAX_RESPONSE_BYTES):
                    raise ValueError("unsafe file")
                data = json.load(source)
            if (not isinstance(data, dict) or data.get("version") != 1 or
                    not isinstance(data.get("accounts"), dict) or
                    not isinstance(data.get("host_id"), str) or
                    not data["host_id"].startswith("urn:uuid:")):
                raise ValueError("invalid state")
            return data
        except (OSError, ValueError, TypeError):
            raise ChatGPTAuthError("credential_storage_invalid", "本机凭据记录无效，请检查其权限或恢复备份。", 503) from None

    def _write_state(self, data: dict[str, Any]) -> None:
        temp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".accounts-", dir=self._state_dir)
            temp_path = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(data, target, ensure_ascii=False, separators=(",", ":"))
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, self._state_path)
            directory_fd = os.open(self._state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _public_status(self, data: dict[str, Any]) -> dict[str, Any]:
        accounts = []
        active_id = data.get("active_account_id")
        for account_id, record in data["accounts"].items():
            signed_in = bool(record.get("access_token")) and not self._needs_login(record)
            accounts.append({"id": account_id, "label": record.get("email") or
                             "ChatGPT 账号 " + account_id[:8], "signed_in": signed_in})
        record = data["accounts"].get(active_id, {})
        needs_login = self._needs_login(record)
        connected = bool(record.get("access_token")) and not needs_login
        plan = connected and PLAN_SCOPES.issubset(set(record.get("scopes", [])))
        active = next((item for item in accounts if item["id"] == active_id), None)
        pending = self._pending is not None and self._pending["expires_at"] > self._clock()
        return {"connected": connected, "plan_usage_enabled": plan, "needs_login": needs_login,
                "account": active, "accounts": accounts, "pending_login": pending,
                "expires_at": record.get("expires_at") if record.get("access_token") else None,
                "reason": ("reauth_required" if needs_login else None if plan else
                           "plan_usage_not_authorized" if connected else "not_connected"),
                "usage_url": USAGE_URL}

    def _needs_login(self, record: dict[str, Any]) -> bool:
        return bool(record.get("access_token") and not record.get("refresh_token") and
                    record.get("expires_at", 0) <= self._clock())

    def status(self) -> dict[str, Any]:
        """Read local, redacted state only; never triggers refresh or network IO."""
        with self._locked_state():
            return self._public_status(self._read_state())

    def begin_login(self, *, account_id: str | None = None,
                    new_account: bool = False) -> dict[str, Any]:
        with self._locked_state():
            data = self._read_state()
            selected = None if new_account else (account_id or data.get("active_account_id"))
            if account_id and (new_account or account_id not in data["accounts"]):
                raise ChatGPTAuthError("unknown_account", "未找到所选 ChatGPT 账号。")
            record = data["accounts"].get(selected, {})
            client_id = ("dynamic_agent_client" if new_account else
                         record.get("client_id") or self._unverified_client_id or "dynamic_agent_client")
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            pending = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                       "verifier": verifier, "client_id": client_id, "account_id": selected,
                       "subject": record.get("subject"), "expires_at": self._clock() + PENDING_SECONDS,
                       "login_id": secrets.token_urlsafe(16)}
            self._pending = pending  # A new explicit attempt invalidates the older one.
            params = {"client_id": client_id, "ext_agent_host_id": data["host_id"],
                      "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": SCOPES,
                      "resource": RESOURCE, "state": pending["state"], "nonce": pending["nonce"],
                      "code_challenge_method": "S256", "code_challenge": challenge}
            if client_id == "dynamic_agent_client":
                params["agent_name_hint"] = "shortdrama_studio"
            elif record.get("email"):
                params["login_hint"] = record["email"]
            # Optional id_token_hint is intentionally omitted: this URL reaches
            # the product browser, which must never receive a token.
            return {"authorization_url": AUTHORIZE_URL + "?" + urlencode(params),
                    "expires_in": PENDING_SECONDS, "login_id": pending["login_id"]}

    @staticmethod
    def _query_value(query: Mapping[str, Any], key: str, max_length: int = 16384) -> str:
        values = query.getlist(key) if hasattr(query, "getlist") else [query.get(key, "")]
        values = values or [""]
        if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > max_length:
            raise ChatGPTAuthError("invalid_callback", "登录回调参数无效，请重新连接。")
        return values[0]

    def complete_login(self, query_params: Mapping[str, Any]) -> dict[str, Any]:
        with self._locked_state():
            pending = self._pending
            state = self._query_value(query_params, "state", 256)
            if pending is None or not hmac.compare_digest(state, pending["state"]):
                raise ChatGPTAuthError("invalid_state", "登录请求不匹配或已失效，请重新连接。")
            self._pending = None  # Consume before HTTP, including denied/error outcomes.
            if pending["expires_at"] <= self._clock():
                raise ChatGPTAuthError("login_expired", "登录请求超过十分钟，请重新连接。")
            if self._query_value(query_params, "error", 256):
                raise ChatGPTAuthError("login_denied", "未完成 ChatGPT 授权，当前连接未更改。")
            code = self._query_value(query_params, "code")
            returned_client = self._query_value(query_params, "client_id", 256)
            client_id = pending["client_id"]
            if client_id == "dynamic_agent_client":
                if not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,240}", returned_client):
                    raise ChatGPTAuthError("registration_incomplete", "未取得有效的应用注册，请重新连接。")
                client_id = returned_client
            elif returned_client and returned_client != client_id:
                raise ChatGPTAuthError("client_mismatch", "回调应用注册不匹配，当前连接未更改。")
            if not code:
                raise ChatGPTAuthError("invalid_callback", "登录回调缺少授权结果，请重新连接。")
            # Keep an issued registration available for reauthorization after
            # a consumed/failed code, without assigning it to an unverified user.
            self._unverified_client_id = client_id
            payload = self._request_json("POST", TOKEN_URL, {
                "grant_type": "authorization_code", "client_id": client_id, "code": code,
                "code_verifier": pending["verifier"], "redirect_uri": REDIRECT_URI,
                "resource": RESOURCE,
            })
            claims = self._validate_id_token(payload.get("id_token"), client_id,
                                             nonce=pending["nonce"])
            if pending["subject"] and claims["sub"] != pending["subject"]:
                raise ChatGPTAuthError("account_mismatch", "返回账号与所选注册不匹配，当前连接未更改。")
            record = self._token_record(payload, scopes_required=True)
            account_id = hashlib.sha256((ISSUER + "\0" + claims["sub"] + "\0" + client_id).encode()).hexdigest()[:24]
            data = self._read_state()
            if pending["account_id"] and account_id != pending["account_id"]:
                raise ChatGPTAuthError("account_mismatch", "返回账号注册不匹配，当前连接未更改。")
            record.update({"issuer": ISSUER, "subject": claims["sub"], "client_id": client_id,
                           "email": claims.get("email") if isinstance(claims.get("email"), str) else None,
                           "nonce": pending["nonce"], "id_token": payload["id_token"]})
            data["accounts"][account_id] = record
            data["active_account_id"] = account_id
            self._write_state(data)
            self._unverified_client_id = None
            return self._public_status(data)

    def _request_json(self, method: str, url: str, data: dict[str, str] | None = None,
                      *, allow_empty: bool = False, timeout: float = 15,
                      deadline: float | None = None) -> dict[str, Any]:
        if url not in {TOKEN_URL, JWKS_URL, DISCOVERY_URL, REVOCATION_URL}:
            raise ChatGPTAuthError("invalid_auth_endpoint", "认证服务地址无效。", 503)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ChatGPTAuthError("auth_network_unavailable", "认证操作已超过等待时间。", 503)
            timeout = min(timeout, remaining)
        try:
            with self._http.stream(method, url, data=data, timeout=timeout) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise ChatGPTAuthError("auth_network_unavailable", "认证操作已超过等待时间。", 503)
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ChatGPTAuthError("invalid_auth_response", "认证服务返回的数据超限。", 502)
                if response.status_code >= 400:
                    error = None
                    try:
                        parsed = json.loads(body)
                        error = parsed.get("error") if isinstance(parsed, dict) else None
                    except (ValueError, TypeError):
                        pass
                    if error == "invalid_grant":
                        raise ChatGPTAuthError("reauth_required", "授权已过期或被撤销，请重新连接。", 401)
                    raise ChatGPTAuthError("auth_service_error", "ChatGPT 认证服务暂不可用，请稍后重试。", 503)
                if response.status_code != 200:
                    raise ChatGPTAuthError("invalid_auth_response", "认证服务返回了不支持的响应。", 502)
                if allow_empty and not body:
                    return {}
                parsed = json.loads(body)
                if not isinstance(parsed, dict):
                    raise ValueError("object required")
                return parsed
        except (httpx.HTTPError, OSError):
            raise ChatGPTAuthError("auth_network_unavailable", "无法连接官方认证服务，请检查网络后重试。", 503) from None
        except (ValueError, TypeError):
            raise ChatGPTAuthError("invalid_auth_response", "认证服务返回的数据无效。", 502) from None

    def _validate_id_token(self, token: Any, client_id: str, *, nonce: str | None = None,
                           expected_subject: str | None = None) -> dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 65536:
            raise ChatGPTAuthError("invalid_identity", "无法验证 ChatGPT 账号身份。", 401)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid or len(kid) > 256:
                raise ValueError("invalid signing header")
            if self._jwks is None or self._jwks_until <= self._clock():
                self._jwks = self._request_json("GET", JWKS_URL)
                self._jwks_until = self._clock() + 300
            keys = self._jwks.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
                raise ValueError("invalid keys")
            matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
            if not matching:
                self._jwks = self._request_json("GET", JWKS_URL)
                self._jwks_until = self._clock() + 300
                keys = self._jwks.get("keys", [])
                if not isinstance(keys, list) or not 1 <= len(keys) <= 32:
                    raise ValueError("invalid keys")
                matching = [key for key in keys if isinstance(key, dict) and key.get("kid") == kid]
            if len(matching) != 1 or matching[0].get("kty") != "RSA" or matching[0].get("use", "sig") != "sig":
                raise ValueError("unknown key")
            key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256")
            required = ["exp", "iat", "iss", "aud", "sub"] + (["nonce"] if nonce is not None else [])
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
                                issuer=ISSUER, options={"require": required}, leeway=15)
            if (not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 512 or
                    (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id) or
                    (nonce is not None and (not isinstance(claims.get("nonce"), str) or
                     not hmac.compare_digest(claims["nonce"], nonce))) or
                    (expected_subject is not None and claims["sub"] != expected_subject)):
                raise ValueError("identity mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise ChatGPTAuthError("invalid_identity", "无法验证 ChatGPT 账号身份，请重新连接。", 401) from None

    def _token_record(self, payload: dict[str, Any], *, scopes_required: bool,
                      previous: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = previous or {}
        access = payload.get("access_token")
        refresh = payload.get("refresh_token", previous.get("refresh_token"))
        expires = payload.get("expires_in")
        scope = payload.get("scope")
        if (not isinstance(access, str) or not 1 <= len(access) <= 32768 or
                any(ord(character) < 33 or ord(character) > 126 for character in access) or
                (refresh is not None and (not isinstance(refresh, str) or not 1 <= len(refresh) <= 32768)) or
                not isinstance(payload.get("token_type"), str) or
                payload["token_type"].lower() != "bearer" or
                isinstance(expires, bool) or not isinstance(expires, (int, float)) or
                not math.isfinite(expires) or not 0 < expires <= 86400 or
                (scopes_required and not isinstance(scope, str)) or
                ("scope" in payload and (not isinstance(scope, str) or len(scope) > 8192))):
            raise ChatGPTAuthError("invalid_token_response", "认证凭据响应无效，请重新连接。", 502)
        scopes = scope.split() if scope is not None else previous.get("scopes", [])
        if len(scopes) > 64 or any(len(item) > 256 for item in scopes):
            raise ChatGPTAuthError("invalid_token_response", "认证权限响应无效。", 502)
        return {"access_token": access, "refresh_token": refresh,
                "expires_at": self._clock() + expires, "scopes": sorted(set(scopes))}

    def access_token(self) -> str:
        """Internal only. Serializes refresh, including across manager processes."""
        with self._locked_state():
            data = self._read_state()
            record = data["accounts"].get(data.get("active_account_id"))
            if not record or not record.get("access_token"):
                raise ChatGPTAuthError("not_connected", "请先连接你的 ChatGPT 账号。", 401)
            if not PLAN_SCOPES.issubset(set(record.get("scopes", []))):
                raise ChatGPTAuthError("plan_usage_not_authorized", "此连接尚未授权使用 ChatGPT 计划。", 403)
            if record.get("expires_at", 0) > self._clock() + 60:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("reauth_required", "ChatGPT 连接已到续期时间，请重新连接。", 401)
            try:
                payload = self._request_json("POST", TOKEN_URL, {
                    "grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": RESOURCE,
                })
                replacement = self._token_record(payload, scopes_required=False, previous=record)
                if payload.get("id_token") is not None:
                    claims = self._validate_id_token(payload["id_token"], record["client_id"],
                                                     expected_subject=record["subject"])
                    if claims.get("nonce") is not None and claims["nonce"] != record.get("nonce"):
                        raise ChatGPTAuthError("invalid_identity", "续期账号验证失败，请重新连接。", 401)
                    replacement["id_token"] = payload["id_token"]
                record.update(replacement)
                self._write_state(data)
            except ChatGPTAuthError as exc:
                if exc.code in {"reauth_required", "invalid_identity", "invalid_token_response"}:
                    for key in _CREDENTIAL_KEYS:
                        record.pop(key, None)
                    self._write_state(data)
                raise
            if not PLAN_SCOPES.issubset(set(record.get("scopes", []))):
                raise ChatGPTAuthError("plan_usage_not_authorized", "续期未授予 ChatGPT 计划使用权限。", 403)
            return record["access_token"]

    def disconnect(self) -> dict[str, Any]:
        with self._locked_state():
            self._pending = None
            self._unverified_client_id = None
            data = self._read_state()
            record = data["accounts"].get(data.get("active_account_id"), {})
            had_session = bool(record.get("access_token") or record.get("refresh_token"))
            refresh_token = record.get("refresh_token")
            client_id = record.get("client_id")
            for key in _CREDENTIAL_KEYS:
                record.pop(key, None)
            # Stop local usage before touching the network. No later write from
            # this disconnect may erase credentials installed by a fresh login.
            self._write_state(data)
            result = self._public_status(data)

        confirmed = not had_session
        if refresh_token and client_id:
            completed = threading.Event()
            cancelled = threading.Event()
            outcome = [False]
            deadline = time.monotonic() + _REVOKE_TOTAL_SECONDS

            def revoke_old_session() -> None:
                try:
                    if cancelled.is_set():
                        return
                    discovery = self._request_json("GET", DISCOVERY_URL,
                                                   timeout=_REVOKE_REQUEST_SECONDS, deadline=deadline)
                    if discovery.get("issuer") != ISSUER or discovery.get("revocation_endpoint") != REVOCATION_URL:
                        raise ChatGPTAuthError("invalid_auth_endpoint", "官方撤销地址验证失败。", 502)
                    if cancelled.is_set():
                        return
                    self._request_json("POST", REVOCATION_URL, {
                        "token": refresh_token, "token_type_hint": "refresh_token", "client_id": client_id,
                    }, allow_empty=True, timeout=_REVOKE_REQUEST_SECONDS, deadline=deadline)
                    outcome[0] = True
                except (ChatGPTAuthError, RuntimeError):
                    pass
                finally:
                    completed.set()

            # HTTP timeouts apply per IO operation. A separate bounded wait
            # also caps the caller's total wait if a server trickles bytes.
            # The daemon can only revoke the captured old session, never write
            # local state or alter a concurrently established new connection.
            threading.Thread(target=revoke_old_session, daemon=True,
                             name="chatgpt-session-revoke").start()
            if completed.wait(_REVOKE_TOTAL_SECONDS):
                confirmed = outcome[0]
            else:
                cancelled.set()
        # Return this operation's local-clear receipt. Reacquiring the account
        # lock here could wait behind a new login and defeat the total budget;
        # callers can independently read the latest status after this receipt.
        result.update({"remote_revocation_confirmed": confirmed,
                       "warning": None if confirmed else
                       "已在本机断开；未能确认远端撤销，请在 ChatGPT 设置中断开此应用。"})
        return result

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ChatGPTAuthManager:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
