"""SiWC contracts with real local RSA signatures; every HTTP call is mocked."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import stat
import threading
import time
from urllib.parse import parse_qs, urlencode

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest
from starlette.datastructures import QueryParams

from app.chatgpt_auth import (
    AUTHORIZE_URL, DISCOVERY_URL, ISSUER, JWKS_URL, REDIRECT_URI, RESOURCE,
    REVOCATION_URL, SCOPES, TOKEN_URL, ChatGPTAuthError, ChatGPTAuthManager,
)
import app.chatgpt_auth as auth_module


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class OAuthFake:
    def __init__(self, key):
        self.key = key
        self.claims = {}
        self.nonce = "not-started"
        self.client_id = "oaiapp_localtest"
        self.subject = "person-one"
        self.scope = SCOPES
        self.requests = []
        self.refreshes = 0
        self.revocation_status = 200
        self.revocation_endpoint = REVOCATION_URL
        self.token_status = 200
        self.token_override = {}
        self.token_factory = None
        self.refresh_error = None
        self.discovery_hook = None

    def id_token(self):
        claims = {"iss": ISSUER, "sub": self.subject, "aud": self.client_id,
                  "iat": int(time.time()), "exp": int(time.time()) + 3600,
                  "nonce": self.nonce, "email": "local@example.test"}
        claims.update(self.claims)
        return (self.token_factory(claims) if self.token_factory else
                jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"}))

    def handle(self, request):
        self.requests.append(request)
        assert str(request.url) in {TOKEN_URL, JWKS_URL, DISCOVERY_URL, REVOCATION_URL}
        if str(request.url) == JWKS_URL:
            public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            return httpx.Response(200, json={"keys": [{**public, "kid": "test-key", "use": "sig"}]})
        if str(request.url) == DISCOVERY_URL:
            if self.discovery_hook:
                self.discovery_hook(request)
            return httpx.Response(200, json={"issuer": ISSUER,
                                           "revocation_endpoint": self.revocation_endpoint})
        if str(request.url) == REVOCATION_URL:
            if self.revocation_status == 200:
                return httpx.Response(200, content=b"")
            return httpx.Response(self.revocation_status, json={"private": "never-return-upstream"})
        form = parse_qs(request.content.decode())
        if form["grant_type"] == ["refresh_token"]:
            self.refreshes += 1
            time.sleep(.015)  # Let a second manager contend for the file lock.
            if self.refresh_error:
                return httpx.Response(400, json={"error": self.refresh_error,
                                               "message": "private-refresh-error"})
            assert form["client_id"] == [self.client_id]
            assert form["resource"] == [RESOURCE]
            assert "scope" not in form
            return httpx.Response(200, json={"access_token": "replacement-access-secret",
                                           "refresh_token": "rotated-refresh-secret",
                                           "token_type": "Bearer", "expires_in": 3600})
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "private-error",
                                                          "message": "authorization-code-secret"})
        return httpx.Response(200, json={"access_token": "initial-access-secret",
                                        "refresh_token": "initial-refresh-secret",
                                        "id_token": self.id_token(), "token_type": "Bearer",
                                        "expires_in": 3600, "scope": self.scope,
                                        **self.token_override})


@pytest.fixture
def auth(tmp_path, signing_key):
    fake = OAuthFake(signing_key)
    now = [time.time()]
    manager = ChatGPTAuthManager(tmp_path / "private-auth", transport=httpx.MockTransport(fake.handle),
                                 clock=lambda: now[0])
    yield manager, fake, now
    manager.close()


def begin(manager, fake, **kwargs):
    result = manager.begin_login(**kwargs)
    query = parse_qs(result["authorization_url"].split("?", 1)[1])
    fake.nonce = query["nonce"][0]
    return result, query


def finish(manager, fake, query, **overrides):
    return manager.complete_login(QueryParams(urlencode({
        "state": query["state"][0], "code": "authorization-code-secret",
        "client_id": fake.client_id, **overrides,
    })))


def connect(manager, fake, **kwargs):
    _, query = begin(manager, fake, **kwargs)
    return finish(manager, fake, query)


def assert_redacted(value):
    serialized = json.dumps(value)
    for secret in ("initial-access-secret", "initial-refresh-secret", "replacement-access-secret",
                   "rotated-refresh-secret", "authorization-code-secret", "eyJ"):
        assert secret not in serialized


def test_local_status_never_networks_and_stable_host_uses_private_permissions(auth):
    manager, fake, _ = auth
    assert not manager.status()["connected"]
    assert fake.requests == []
    first = manager.begin_login()
    second = manager.begin_login()
    q1, q2 = [parse_qs(item["authorization_url"].split("?", 1)[1]) for item in (first, second)]
    assert q1["ext_agent_host_id"] == q2["ext_agent_host_id"]
    assert q1["state"] != q2["state"]
    assert q1["nonce"] != q2["nonce"]
    for path in manager._state_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manager._state_dir.stat().st_mode) == 0o700
    with ChatGPTAuthManager(manager._state_dir, transport=httpx.MockTransport(fake.handle)) as other:
        host = parse_qs(other.begin_login()["authorization_url"].split("?", 1)[1])["ext_agent_host_id"]
        assert host == q1["ext_agent_host_id"]


def test_default_storage_is_app_owned_and_project_cloud_or_codex_storage_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    with ChatGPTAuthManager(transport=httpx.MockTransport(lambda _: pytest.fail("network"))) as manager:
        assert manager._state_dir == tmp_path / "Library/Application Support/Shortdrama Studio/chatgpt"
    for path in (tmp_path / "Library/Mobile Documents/com~apple~CloudDocs/auth",
                 tmp_path / ".codex/auth", Path(__file__).resolve().parents[2] / ".runtime/auth"):
        with pytest.raises(ChatGPTAuthError, match="云同步目录"):
            ChatGPTAuthManager(path)


def test_pkce_nonce_signature_and_exact_callback_contract(auth):
    manager, fake, _ = auth
    result, query = begin(manager, fake)
    assert result["authorization_url"].startswith(AUTHORIZE_URL + "?")
    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["agent_name_hint"] == ["shortdrama_studio"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["scope"] == [SCOPES]
    assert query["resource"] == [RESOURCE]
    assert query["code_challenge_method"] == ["S256"]
    status = finish(manager, fake, query)
    assert status["connected"] and status["plan_usage_enabled"]
    assert not status["pending_login"]
    assert manager.access_token() == "initial-access-secret"
    token_request = next(item for item in fake.requests if str(item.url) == TOKEN_URL)
    form = parse_qs(token_request.content.decode())
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
    assert form["client_id"] == [fake.client_id]
    assert form["redirect_uri"] == [REDIRECT_URI]
    assert form["resource"] == [RESOURCE]
    assert query["code_challenge"] == [challenge]
    assert "client_secret" not in form
    assert_redacted(status)
    with pytest.raises(ChatGPTAuthError, match="不匹配或已失效"):
        finish(manager, fake, query)
    assert len([item for item in fake.requests if str(item.url) == TOKEN_URL]) == 1


def test_invalid_state_cannot_consume_valid_pending_and_duplicate_state_is_rejected(auth):
    manager, fake, _ = auth
    _, query = begin(manager, fake)
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.complete_login({"state": "attacker", "code": "secret"})
    assert failure.value.code == "invalid_state"
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.complete_login(QueryParams("state=x&state=y&code=secret"))
    assert failure.value.code == "invalid_callback"
    assert fake.requests == []
    assert finish(manager, fake, query)["connected"]


def test_expired_and_declined_callbacks_are_consumed_without_token_exchange(auth):
    manager, fake, now = auth
    _, query = begin(manager, fake)
    now[0] += 601
    with pytest.raises(ChatGPTAuthError) as failure:
        finish(manager, fake, query)
    assert failure.value.code == "login_expired"
    _, query = begin(manager, fake)
    with pytest.raises(ChatGPTAuthError) as failure:
        finish(manager, fake, query, error="access_denied")
    assert failure.value.code == "login_denied"
    assert not manager.status()["connected"]
    assert fake.requests == []


@pytest.mark.parametrize("client_id", ["", "dynamic_agent_client", "https://evil.example/steal"])
def test_unissued_or_missing_client_id_cannot_exchange(auth, client_id):
    manager, fake, _ = auth
    _, query = begin(manager, fake)
    with pytest.raises(ChatGPTAuthError) as failure:
        finish(manager, fake, query, client_id=client_id)
    assert failure.value.code == "registration_incomplete"
    assert fake.requests == []


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.example"}, {"aud": "wrong-client"}, {"nonce": "wrong-nonce"},
    {"exp": 1}, {"sub": ""}, {"aud": ["oaiapp_localtest", "other-client"]},
])
def test_invalid_signed_identity_never_persists_credentials(auth, claims):
    manager, fake, _ = auth
    fake.claims = claims
    with pytest.raises(ChatGPTAuthError) as failure:
        connect(manager, fake)
    assert failure.value.code == "invalid_identity"
    assert not manager.status()["connected"]
    assert_redacted(manager._read_state())


@pytest.mark.parametrize("attack", ["hmac", "wrong-key", "missing-exp", "unknown-kid"])
def test_jwt_algorithm_confusion_and_invalid_signature_rejected(auth, signing_key, attack):
    manager, fake, _ = auth
    def token(claims):
        if attack == "hmac":
            return jwt.encode(claims, "untrusted-key" * 4, algorithm="HS256", headers={"kid": "test-key"})
        if attack == "missing-exp":
            claims.pop("exp")
        key = (rsa.generate_private_key(public_exponent=65537, key_size=2048)
               if attack == "wrong-key" else signing_key)
        return jwt.encode(claims, key, algorithm="RS256",
                          headers={"kid": "unknown-key" if attack == "unknown-kid" else "test-key"})
    fake.token_factory = token
    with pytest.raises(ChatGPTAuthError) as failure:
        connect(manager, fake)
    assert failure.value.code == "invalid_identity"
    assert not manager.status()["connected"]


def test_granted_scopes_from_token_not_callback_control_plan_access(auth):
    manager, fake, _ = auth
    fake.scope = "openid profile email"
    _, query = begin(manager, fake)
    status = finish(manager, fake, query, scope=SCOPES)
    assert status["connected"] and not status["plan_usage_enabled"]
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.access_token()
    assert failure.value.code == "plan_usage_not_authorized"
    assert_redacted(status)


@pytest.mark.parametrize("override", [{"access_token": None}, {"token_type": {}},
                                      {"expires_in": -1}, {"scope": None}])
def test_malformed_token_response_does_not_partially_connect(auth, override):
    manager, fake, _ = auth
    fake.token_override = override
    with pytest.raises(ChatGPTAuthError) as failure:
        connect(manager, fake)
    assert failure.value.code == "invalid_token_response"
    assert not manager.status()["connected"]


def test_account_reauthorization_preserves_active_identity_and_separate_registrations(auth):
    manager, fake, _ = auth
    first = connect(manager, fake)["account"]["id"]
    _, query = begin(manager, fake)
    assert query["client_id"] == ["oaiapp_localtest"]
    assert "agent_name_hint" not in query and "id_token_hint" not in query
    fake.subject = "different-person"
    with pytest.raises(ChatGPTAuthError) as failure:
        finish(manager, fake, query)
    assert failure.value.code == "account_mismatch"
    assert manager.status()["account"]["id"] == first
    fake.client_id = "oaiapp_otherworkspace"
    second = connect(manager, fake, new_account=True)["account"]["id"]
    assert first != second
    assert len(manager.status()["accounts"]) == 2  # Same email must not merge registrations.
    _, query = begin(manager, fake, account_id=first)
    assert query["client_id"] == ["oaiapp_localtest"]
    with pytest.raises(ChatGPTAuthError) as failure:
        finish(manager, fake, query, client_id="oaiapp_otherworkspace")
    assert failure.value.code == "client_mismatch"
    assert manager.status()["account"]["id"] == second


def test_refresh_rotation_is_serialized_across_managers_and_persisted_together(auth):
    manager, fake, now = auth
    connect(manager, fake)
    now[0] += 3550
    with ChatGPTAuthManager(manager._state_dir, transport=httpx.MockTransport(fake.handle),
                            clock=lambda: now[0]) as other:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda current: current.access_token(), (manager, other)))
        assert results == ["replacement-access-secret"] * 2
        assert fake.refreshes == 1
    data = manager._read_state()
    record = data["accounts"][data["active_account_id"]]
    assert record["access_token"] == "replacement-access-secret"
    assert record["refresh_token"] == "rotated-refresh-secret"
    assert record["expires_at"] == now[0] + 3600
    assert stat.S_IMODE(manager._state_path.stat().st_mode) == 0o600
    assert_redacted(manager.status())


def test_revoked_refresh_clears_tokens_but_keeps_registration(auth):
    manager, fake, now = auth
    connect(manager, fake)
    now[0] += 3550
    fake.refresh_error = "invalid_grant"
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.access_token()
    assert failure.value.code == "reauth_required"
    assert "private-refresh-error" not in str(failure.value)
    assert not manager.status()["connected"]
    assert_redacted(manager._read_state())
    _, query = begin(manager, fake)
    assert query["client_id"] == [fake.client_id]


def test_expired_nonrenewable_token_reports_needs_login_without_network(auth):
    manager, fake, now = auth
    fake.token_override = {"refresh_token": None}
    assert connect(manager, fake)["connected"]
    request_count = len(fake.requests)
    now[0] += 3601
    status = manager.status()
    assert not status["connected"] and not status["plan_usage_enabled"]
    assert status["needs_login"] and status["reason"] == "reauth_required"
    assert not status["account"]["signed_in"]
    assert len(fake.requests) == request_count
    assert_redacted(status)


def test_slow_revocation_does_not_block_status_or_erase_concurrent_new_login(auth):
    manager, fake, _ = auth
    connect(manager, fake)
    entered = threading.Event()
    release = threading.Event()

    def pause_discovery(_request):
        entered.set()
        assert release.wait(5), "test did not release discovery"

    fake.discovery_hook = pause_discovery
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_disconnect = pool.submit(manager.disconnect)
        try:
            assert entered.wait(1)
            status = pool.submit(manager.status).result(timeout=1)
            assert not status["connected"] and not status["plan_usage_enabled"]
            assert_redacted(manager._read_state())
            fake.token_override = {"access_token": "new-login-access-secret",
                                   "refresh_token": "new-login-refresh-secret"}
            new_account = connect(manager, fake)["account"]["id"]
            assert manager.access_token() == "new-login-access-secret"
        finally:
            release.set()
        result = old_disconnect.result(timeout=1)
    assert result["remote_revocation_confirmed"]
    assert not result["connected"]  # Receipt describes this disconnect, not a newer login.
    assert manager.status()["account"]["id"] == new_account
    assert manager.access_token() == "new-login-access-secret"
    revocation = next(item for item in fake.requests if str(item.url) == REVOCATION_URL)
    assert parse_qs(revocation.content.decode())["token"] == ["initial-refresh-secret"]
    assert revocation.extensions["timeout"]["read"] == 4


def test_disconnect_total_wait_is_bounded_and_timeout_does_not_reenable_local_tokens(auth, monkeypatch):
    manager, fake, _ = auth
    connect(manager, fake)
    monkeypatch.setattr(auth_module, "_REVOKE_TOTAL_SECONDS", .05)
    entered = threading.Event()
    release = threading.Event()
    finished_discovery = threading.Event()

    def pause_discovery(_request):
        entered.set()
        release.wait(5)
        finished_discovery.set()

    fake.discovery_hook = pause_discovery
    try:
        started = time.monotonic()
        result = manager.disconnect()
        assert time.monotonic() - started < 1
        assert entered.is_set()
        assert not result["connected"]
        assert not result["remote_revocation_confirmed"] and result["warning"]
        assert not manager.status()["connected"]
        assert_redacted(manager._read_state())
    finally:
        release.set()
        assert finished_discovery.wait(1)
    # The expired budget must not start the revocation POST after discovery.
    assert not any(str(item.url) == REVOCATION_URL for item in fake.requests)


@pytest.mark.parametrize("mode", ["success", "server-failure", "evil-discovery"])
def test_disconnect_revokes_or_reports_unconfirmed_and_retains_mapping(auth, mode):
    manager, fake, _ = auth
    connect(manager, fake)
    if mode == "server-failure":
        fake.revocation_status = 503
    if mode == "evil-discovery":
        fake.revocation_endpoint = "https://evil.example/steal"
    status = manager.disconnect()
    assert not status["connected"]
    assert status["remote_revocation_confirmed"] == (mode == "success")
    assert bool(status["warning"]) == (mode != "success")
    assert_redacted(status)
    assert_redacted(manager._read_state())
    _, query = begin(manager, fake)
    assert query["client_id"] == [fake.client_id]
    assert "id_token_hint" not in query
    if mode == "evil-discovery":
        assert not any(str(item.url) == REVOCATION_URL for item in fake.requests)


def test_upstream_errors_and_redirects_do_not_leak_or_follow_arbitrary_urls(auth):
    manager, fake, _ = auth
    fake.token_status = 503
    with pytest.raises(ChatGPTAuthError) as failure:
        connect(manager, fake)
    assert "authorization-code-secret" not in str(failure.value)
    assert "private-error" not in str(failure.value)
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    with ChatGPTAuthManager(manager._state_dir, transport=httpx.MockTransport(redirect)) as other:
        with pytest.raises(ChatGPTAuthError) as failure:
            connect(other, fake)
        assert failure.value.code == "invalid_auth_response"
    assert len(requests) == 1 and str(requests[0].url) == TOKEN_URL


def test_symlink_or_world_readable_credentials_fail_closed(auth, tmp_path):
    manager, _, _ = auth
    original = manager._state_path.read_text()
    manager._state_path.chmod(0o644)
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.status()
    assert failure.value.code == "credential_storage_invalid"
    manager._state_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(original)
    manager._state_path.symlink_to(outside)
    with pytest.raises(ChatGPTAuthError) as failure:
        manager.status()
    assert failure.value.code == "credential_storage_invalid"
