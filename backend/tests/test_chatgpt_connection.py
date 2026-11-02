"""Browser/OAuth boundary tests. No provider requests, credentials or product DB."""
import logging
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import chatgpt_connection as connection


class FakeManager:
    def __init__(self):
        self.begin_count = 0
        self.callbacks = []
        self.connected = False
        self.account_id = "local-account-one"
        self.probe_status = {"attempts": []}

    def status(self):
        return {"connected": self.connected, "plan_usage_enabled": self.connected,
            "account": {"id": self.account_id, "label": "u***@example.test"} if self.connected else None,
            "pending_login": self.begin_count > 0 and not self.connected,
            "access_token": "must-never-leave-backend"}

    def begin_login(self):
        self.begin_count += 1
        return {"authorization_url": "https://auth.openai.com/api/accounts/authorize?state=browser-state"}

    def complete_login(self, params):
        self.callbacks.append(params)
        self.connected = True
        return self.status()

    def disconnect(self):
        self.connected = False
        return {"remote_revocation_confirmed": True}


@pytest.fixture
def web():
    manager = FakeManager()
    app = FastAPI()
    app.include_router(connection.router)
    app.dependency_overrides[connection.get_manager] = lambda: manager
    app.dependency_overrides[connection.get_probe_status] = lambda: manager.probe_status
    connection.flow.tickets.clear()
    connection.flow.last_error = None
    with TestClient(app, base_url="http://127.0.0.1:8031", follow_redirects=False) as client:
        yield client, manager


def start(client):
    result = client.post("/api/v1/chatgpt/connect", headers={"X-Studio-Client": "1"})
    assert result.status_code == 200
    url = result.json()["login_url"]
    assert url.startswith("http://127.0.0.1:8031/auth/chatgpt/start?ticket=")
    return url


def test_connect_requires_workbench_write_boundary(web):
    client, manager = web
    assert client.post("/api/v1/chatgpt/connect").status_code == 403
    assert client.post("/api/v1/chatgpt/connect", headers={
        "X-Studio-Client": "1", "Origin": "https://untrusted.example"}).status_code == 403
    assert manager.begin_count == 0


def test_ticket_is_one_time_and_browser_cookie_binds_callback(web):
    client, manager = web
    url = start(client)
    assert manager.begin_count == 0  # no OAuth/network work until real navigation
    response = client.get(url)
    assert response.status_code == 303
    assert urlsplit(response.headers["location"]).netloc == "auth.openai.com"
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=lax" in response.headers["set-cookie"].lower()
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert client.get(url).status_code == 400
    assert manager.begin_count == 1
    assert client.get("/auth/callback?state=wrong&code=private-code").status_code == 400
    assert not manager.callbacks
    response = client.get("/auth/callback?state=browser-state&code=private-code&client_id=oaiapp_test")
    assert response.status_code == 303
    assert response.headers["location"] == connection.FRONTEND + "?connection=success"
    assert "private-code" not in response.text + response.headers["location"]
    assert manager.connected and len(manager.callbacks) == 1
    assert client.get("/auth/callback?state=browser-state&code=private-code").status_code == 400


def test_callback_without_browser_or_with_duplicate_fields_is_rejected(web):
    client, manager = web
    assert client.get("/auth/callback?state=browser-state&code=stolen").status_code == 400
    client.get(start(client))
    assert client.get("/auth/callback?state=browser-state&code=one&code=two").status_code == 400
    assert not manager.callbacks


def test_ticket_expiry_never_starts_login(web):
    client, manager = web
    url = start(client)
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]
    connection.flow.tickets[ticket] = 0
    assert client.get(url).status_code == 400
    assert manager.begin_count == 0


def test_status_never_returns_credentials_and_login_does_not_equal_inference(web):
    client, manager = web
    manager.connected = True
    response = client.get("/api/v1/chatgpt/status")
    result = response.json()
    assert result["status"] == "connected" and result["plan_usage"]
    assert not result["verified_inference"] and not result["runtime"]["ready"]
    assert "must-never-leave-backend" not in response.text
    assert manager.begin_count == 0
    assert response.headers["cache-control"] == "no-store"


def smoke_attempt(manager, kind="text", state="completed", passed=True, **extra):
    return {"account": hashlib.sha256(manager.account_id.encode()).hexdigest(), "kind": kind,
            "state": state, "result": {"smoke_passed": passed, "text": "private-model-output"},
            "request_id": "private-request-id", "access_token": "private-probe-token", **extra}


def test_completed_current_account_smokes_are_projected_without_ledger_or_outputs(web):
    client, manager = web
    manager.connected = True
    manager.probe_status = {"attempts": [smoke_attempt(manager), smoke_attempt(manager, "frames")]}
    response = client.get("/api/v1/chatgpt/status")
    result = response.json()
    assert result["verified_inference"] and not result["runtime"]["ready"]
    assert result["smoke_tests"]["text"]["state"] == "passed"
    assert result["smoke_tests"]["frames"]["state"] == "passed"
    for private in ("private-model-output", "private-request-id", "private-probe-token",
                    hashlib.sha256(manager.account_id.encode()).hexdigest(), "attempts"):
        assert private not in response.text
    assert manager.begin_count == 0


@pytest.mark.parametrize("attempt,expected", [
    ({"state": "unknown"}, "unknown"), ({"state": "started"}, "unknown"),
    ({"state": "cancelled"}, "unknown"), ({"state": "failed"}, "failed"),
    ({"result": {"smoke_passed": False}}, "failed"),
    ({"result": {"smoke_passed": "true"}}, "failed"),
])
def test_incomplete_failed_or_false_smoke_cannot_be_verified(web, attempt, expected):
    client, manager = web
    manager.connected = True
    manager.probe_status = {"attempts": [smoke_attempt(manager, **attempt)]}
    result = client.get("/api/v1/chatgpt/status").json()
    assert not result["verified_inference"]
    assert result["smoke_tests"]["text"]["state"] == expected
    assert result["smoke_tests"]["frames"]["state"] == "not_tested"


def test_old_unknown_is_visible_even_when_another_attempt_passed(web):
    client, manager = web
    manager.connected = True
    manager.probe_status = {"attempts": [smoke_attempt(manager, state="unknown"),
        smoke_attempt(manager), smoke_attempt(manager, "frames", state="unknown")]}
    result = client.get("/api/v1/chatgpt/status").json()
    assert result["verified_inference"] and result["smoke_tests"]["has_unknown"]
    assert result["smoke_tests"]["text"] == {"state": "passed", "has_unknown": True}
    assert result["smoke_tests"]["frames"] == {"state": "unknown", "has_unknown": True}


def test_models_catalog_is_not_inference_evidence_and_its_unknown_is_visible(web):
    client, manager = web
    manager.connected = True
    manager.probe_status = {"attempts": [smoke_attempt(manager, "models"),
        smoke_attempt(manager, "models", state="unknown")]}
    result = client.get("/api/v1/chatgpt/status").json()
    assert not result["verified_inference"] and result["smoke_tests"]["has_unknown"]
    assert result["smoke_tests"]["text"]["state"] == "not_tested"


def test_switching_accounts_or_disconnect_does_not_reuse_previous_evidence(web):
    client, manager = web
    manager.connected = True
    manager.probe_status = {"attempts": [smoke_attempt(manager),
                                       smoke_attempt(manager, "frames", state="unknown")]}
    assert client.get("/api/v1/chatgpt/status").json()["verified_inference"]
    manager.account_id = "different-local-account"
    result = client.get("/api/v1/chatgpt/status").json()
    assert not result["verified_inference"] and not result["smoke_tests"]["has_unknown"]
    assert result["smoke_tests"]["text"]["state"] == "not_tested"
    manager.account_id = "local-account-one"
    manager.connected = False
    result = client.get("/api/v1/chatgpt/status").json()
    assert not result["verified_inference"] and not result["smoke_tests"]["has_unknown"]


def test_unreadable_local_evidence_is_unknown_and_never_verified(web):
    client, manager = web
    manager.connected = True
    manager.probe_status = None
    result = client.get("/api/v1/chatgpt/status").json()
    assert not result["verified_inference"]
    assert result["smoke_tests"]["text"]["state"] == "unknown"
    assert not result["smoke_tests"]["record_available"]


def test_missing_probe_ledger_status_does_not_initialize_files_or_network(tmp_path, monkeypatch):
    monkeypatch.setattr(connection.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(connection, "ChatGPTProbe", lambda *_a, **_k: pytest.fail("must not initialize probe"))
    assert connection.get_probe_status(FakeManager()) == {"attempts": []}
    assert list(tmp_path.iterdir()) == []


def test_invalid_authorization_endpoint_is_not_followed(web):
    client, manager = web
    manager.begin_login = lambda: {"authorization_url": "https://evil.example/?state=private"}
    response = client.get(start(client))
    assert response.headers["location"] == connection.FRONTEND + "?connection=error"
    assert "evil.example" not in response.text
    assert client.get("/api/v1/chatgpt/status").json()["status"] == "error"


def test_disconnect_is_explicit_and_returns_no_credentials(web):
    client, manager = web
    manager.connected = True
    assert client.post("/api/v1/chatgpt/disconnect").status_code == 403
    assert manager.connected
    response = client.post("/api/v1/chatgpt/disconnect", headers={"X-Studio-Client": "1"})
    assert response.json()["disconnected"] and not manager.connected
    assert "must-never-leave-backend" not in response.text


def test_cancel_invalidates_unconsumed_browser_login_tickets(web):
    client, manager = web
    url = start(client)
    assert client.post("/api/v1/chatgpt/disconnect", headers={"X-Studio-Client": "1"}).status_code == 200
    assert client.get(url).status_code == 400
    assert manager.begin_count == 0


def test_known_expired_credentials_are_shown_as_needing_login(web):
    client, manager = web
    manager.status = lambda: {"connected": False, "plan_usage_enabled": False,
                              "reason": "reauth_required", "needs_login": True}
    result = client.get("/api/v1/chatgpt/status").json()
    assert result["status"] == "needs_login" and not result["plan_usage"]


def test_failed_reauthorization_does_not_hide_existing_valid_connection(web):
    client, manager = web
    manager.connected = True
    connection.flow.last_error = "新的授权未完成；旧连接仍保留。"
    result = client.get("/api/v1/chatgpt/status").json()
    assert result["status"] == "connected" and result["plan_usage"]
    assert result["account_label"] and result["message"] == connection.flow.last_error


def test_auth_access_logs_drop_callback_codes_and_ticket_urls():
    guard = connection.OAuthAccessLogFilter()
    for url in ("/auth/callback?code=private&state=private", "/auth/chatgpt/start?ticket=private",
                "/api/v1/chatgpt/status"):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s",
                                  ("127.0.0.1", "GET", url, "1.1", 200), None)
        assert not guard.filter(record)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s",
                              ("127.0.0.1", "GET", "/api/v1/health", "1.1", 200), None)
    assert guard.filter(record)
