"""All provider calls use MockTransport; no real account/model/credit is used."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import stat
import threading

import httpx
import pytest

import app.chatgpt_probe as module
from app.chatgpt_probe import BoundFrame, ChatGPTProbe, ChatGPTProbeError, SAMPLE_ASSET_ID, SAMPLE_SHA256


class FakeAuth:
    account = "verified-account-a"
    token_calls = 0

    def status(self):
        return {"connected": True, "plan_usage_enabled": True, "account": {"id": self.account}}

    def access_token(self):
        self.token_calls += 1
        return "synthetic-siwc-bearer"


def catalog():
    return httpx.Response(200, json={"models": [
        {"slug": "model-z", "display_name": "First from service", "visibility": "list"},
        {"slug": "hidden-model", "visibility": "hide"},
        {"slug": "model-a", "display_name": "Second", "visibility": "list"},
        {"slug": "https://bad/model", "visibility": "list"}]})


def sse(*events):
    content = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events).encode()
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)


def complete():
    return sse({"type": "response.output_text.delta", "delta": "连接"},
               {"type": "response.completed", "response": {"status": "completed", "usage": {
                   "input_tokens": 5, "output_tokens": 3, "total_tokens": 8}, "output": [
                   {"type": "message", "content": [{"type": "output_text", "text": "连接成功"}]}]}})


def probe(tmp_path, handler=None, auth=None):
    return ChatGPTProbe(auth or FakeAuth(), state_dir=tmp_path / "private-probe",
        transport=httpx.MockTransport(handler or (lambda request: catalog() if request.method == "GET" else complete())))


def discover(instance):
    assert instance.models(request_id="catalog-0001")["state"] == "completed"


def test_status_does_not_get_bearer_or_send_network(tmp_path):
    auth = FakeAuth()
    with probe(tmp_path, lambda _: pytest.fail("status used network"), auth) as instance:
        assert instance.status()["attempts_used"] == 0
        assert auth.token_calls == 0


def test_official_models_shape_order_default_and_account_isolation(tmp_path):
    auth = FakeAuth()
    with probe(tmp_path, auth=auth) as instance:
        discover(instance)
        assert instance.status()["catalog"]["models"] == ["model-z", "model-a"]
        assert instance.selected_model() == "model-z"
        assert instance.selected_model("model-a") == "model-a"
        with pytest.raises(ChatGPTProbeError):
            instance.selected_model("hidden-model")
        auth.account = "verified-account-b"
        assert instance.status()["catalog"] is None
        with pytest.raises(ChatGPTProbeError, match="模型"):
            instance.text(request_id="text-0001", plan_only_confirmed=True)


def test_text_request_is_fixed_no_tools_no_store_and_completed_only(tmp_path, caplog):
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return catalog()
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["Authorization"] == "Bearer synthetic-siwc-bearer"
        data = json.loads(request.content)
        assert data["model"] == "model-z"
        assert data["stream"] is True and data["store"] is False
        assert set(data) == {"model", "input", "store", "stream"}
        assert not {"tools", "max_output_tokens", "metadata", "background", "conversation",
                    "previous_response_id", "prompt", "tool_choice", "reasoning"} & data.keys()
        assert request.headers["User-Agent"] == "shortdrama_studio/0.1.0"
        assert data["input"][0]["content"] == [{"type": "input_text", "text": module._TEXT}]
        return complete()
    with caplog.at_level("INFO"), probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "completed"
        assert result["result"]["text"] == "连接成功"
        assert result["result"]["usage"]["total_tokens"] == 8
        assert instance.status()["attempts_used"] == 2
        assert instance._http.follow_redirects is False
        assert instance._http._trust_env is False
        assert "synthetic-siwc-bearer" not in instance._state_path.read_text()
    assert "synthetic-siwc-bearer" not in caplog.text
    assert len(requests) == 2


@pytest.mark.parametrize("confirmed", [False, None, "true", 1])
def test_credit_confirmation_required_before_any_bearer_or_network(tmp_path, confirmed):
    auth = FakeAuth()
    with probe(tmp_path, lambda _: pytest.fail("unconfirmed call"), auth) as instance:
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="text-0001", plan_only_confirmed=confirmed)
        assert failure.value.code == "plan_confirmation_required"
        assert auth.token_calls == 0


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_http_failure_and_redirect_never_retry(tmp_path, status):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return catalog() if request.method == "GET" else httpx.Response(status,
            headers={"Location": "https://malicious.invalid"}, json={"message": "private-error-sentinel"})
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "failed" and result["error_code"] == f"http_{status}"
        assert "private-error-sentinel" not in json.dumps(result)
        assert instance.text(request_id="text-0001", plan_only_confirmed=True) == result
    assert len(calls) == 2


@pytest.mark.parametrize("reply,error,state", [
    (lambda: sse({"type": "response.output_text.delta", "delta": "partial"}), "stream_incomplete", "unknown"),
    (lambda: sse({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "private"}}}), "subscription_sharing_usage_limit_exceeded", "failed"),
    (lambda: sse({"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_unavailable"}}}), "subscription_sharing_usage_unavailable", "failed"),
    (lambda: sse({"type": "response.incomplete"}), "provider_incomplete", "failed"),
    (lambda: sse({"type": "response.output_item.done", "item": {"type": "function_call", "name": "exec_command"}}), "unexpected_tool", "failed"),
    (lambda: httpx.Response(200, content=b"data: not-json-private\n\n", headers={"Content-Type": "text/event-stream"}), "invalid_response", "unknown"),
    (lambda: httpx.Response(200, content=b"data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}), "stream_incomplete", "unknown"),
])
def test_stream_terminal_failures_are_not_success_or_retried(tmp_path, reply, error, state):
    calls = []
    def handler(request):
        calls.append(request)
        return catalog() if request.method == "GET" else reply()
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == state and result["error_code"] == error
        assert "private" not in json.dumps(result)
        if state == "unknown":
            for request_id in ("text-0001", "text-0002"):
                with pytest.raises(ChatGPTProbeError) as failure:
                    instance.text(request_id=request_id, plan_only_confirmed=True)
                assert failure.value.code == "result_unknown"
    assert len(calls) == 2


@pytest.mark.parametrize("exception,error", [(httpx.ReadTimeout("private-token"), "timeout_unknown"),
                                          (httpx.ConnectError("private-token"), "network_unknown")])
def test_network_unknown_remains_durable_and_prevents_new_request_id_retry(tmp_path, exception, error):
    def handler(request):
        if request.method == "GET":
            return catalog()
        raise exception
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == error
    with probe(tmp_path) as restarted:
        with pytest.raises(ChatGPTProbeError) as failure:
            restarted.text(request_id="text-0002", plan_only_confirmed=True)
        assert failure.value.code == "result_unknown"
        assert "private-token" not in restarted._state_path.read_text()


def test_attempt_is_durable_before_send_and_same_request_is_not_double_sent(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return catalog()
        ledger = json.loads((tmp_path / "private-probe/probe-attempts.json").read_text())
        assert ledger["attempts"][-1]["state"] == "started"
        entered.set()
        assert release.wait(3)
        return complete()
    with probe(tmp_path, handler) as instance, ThreadPoolExecutor(max_workers=2) as pool:
        discover(instance)
        first = pool.submit(instance.text, request_id="text-0001", plan_only_confirmed=True)
        assert entered.wait(3)
        try:
            with probe(tmp_path, handler) as other:
                with pytest.raises(ChatGPTProbeError) as failure:
                    other.text(request_id="text-0001", plan_only_confirmed=True)
                assert failure.value.code == "result_unknown"
        finally:
            release.set()
        assert first.result()["state"] == "completed"
        assert instance.text(request_id="text-0001", plan_only_confirmed=True)["state"] == "completed"
    assert len(calls) == 2


def test_eight_request_cap_is_shared_by_catalog_and_responses(tmp_path):
    with probe(tmp_path) as instance:
        discover(instance)
        for index in range(7):
            assert instance.text(request_id=f"text-{index:04}", plan_only_confirmed=True)["state"] == "completed"
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.models(request_id="catalog-0002")
        assert failure.value.code == "limit_reached"
        assert instance.status()["attempts_remaining"] == 0


def test_request_id_cannot_be_reused_with_another_selected_model(tmp_path):
    with probe(tmp_path) as instance:
        discover(instance)
        instance.text(request_id="text-0001", plan_only_confirmed=True)
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(model="model-a", request_id="text-0001", plan_only_confirmed=True)
        assert failure.value.code == "request_conflict"
        assert instance.status()["attempts_used"] == 2


def test_oversized_sse_event_fails_unknown_without_persisting_provider_body(tmp_path):
    def handler(request):
        if request.method == "GET":
            return catalog()
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=b"data: " + b"x" * (module.MAX_EVENT_BYTES + 1) + b"\n\n")
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == "invalid_response"
        assert instance._state_path.stat().st_size < 10000


def test_cancelled_stream_closes_without_automatic_retry(tmp_path):
    stop = threading.Event()
    def handler(request):
        if request.method == "GET":
            return catalog()
        stop.set()
        return complete()
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True, cancelled=stop.is_set)
        assert result["state"] == "cancelled"
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="text-0002", plan_only_confirmed=True)
        assert failure.value.code == "result_unknown"


def jpeg(width=320, height=160):
    return b"\xff\xd8\xff\xc0\x00\x0b\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00\xff\xd9"


def frame(shot="shot-0001"):
    return BoundFrame(SAMPLE_SHA256, SAMPLE_ASSET_ID, 1, shot, "0", jpeg())


def test_frames_are_only_fixed_bound_sample_and_at_most_two_jpegs(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "load_designated_frames", lambda: (frame(), frame("shot-0005")))
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return catalog()
        data = json.loads(request.content)
        images = [part for part in data["input"][0]["content"] if part["type"] == "input_image"]
        assert len(images) == 2 and all(p["image_url"].startswith("data:image/jpeg;base64,") for p in images)
        assert set(data) == {"model", "input", "store", "stream"}
        return complete()
    with probe(tmp_path, handler) as instance:
        discover(instance)
        assert instance.frames(request_id="frames-0001", plan_only_confirmed=True)["state"] == "completed"
        monkeypatch.setattr(module, "load_designated_frames", lambda: (frame(), frame("shot-0002"), frame("shot-0003")))
        with pytest.raises(ChatGPTProbeError):
            instance.frames(request_id="frames-0002", plan_only_confirmed=True)
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [b"not-jpeg", jpeg(65535, 65535), b"\xff\xd8" + b"x" * 300000 + b"\xff\xd9"])
def test_frame_contract_rejects_unbounded_or_invalid_jpeg(bad):
    with pytest.raises(ChatGPTProbeError):
        BoundFrame(SAMPLE_SHA256, SAMPLE_ASSET_ID, 1, "shot-0001", "0", bad).validate()
    with pytest.raises(ChatGPTProbeError):
        BoundFrame("a" * 64, SAMPLE_ASSET_ID, 1, "shot-0001", "0", jpeg()).validate()


def test_storage_is_private_and_corruption_does_not_reset_counter(tmp_path):
    with probe(tmp_path) as instance:
        discover(instance)
        assert stat.S_IMODE(instance._dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(instance._state_path.stat().st_mode) == 0o600
        instance._state_path.write_text("corrupt")
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.models(request_id="catalog-0002")
        assert failure.value.code == "storage_invalid"
    with pytest.raises(ChatGPTProbeError):
        ChatGPTProbe(FakeAuth(), state_dir=module._PROJECT / ".runtime/probe-unsafe")


def test_cli_defaults_to_status_and_requires_explicit_credits_confirmation():
    path = Path(__file__).resolve().parents[2] / "scripts/check_chatgpt_connection.py"
    spec = importlib.util.spec_from_file_location("probe_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    args = cli.parse_args([])
    assert not args.run_text and not args.run_frames and not args.list_models
    with pytest.raises(SystemExit):
        cli.parse_args(["--run-text", "--request-id", "text-0001"])
    assert cli.parse_args(["--run-text", "--request-id", "text-0001", "--plan-only-confirmed"]).model is None


def test_completed_refusal_is_not_successful_text_or_image_smoke(tmp_path):
    def handler(request):
        if request.method == "GET":
            return catalog()
        return sse({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "不能回答"}]}]}})
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "completed"
        assert result["result"]["response_completed"] is True
        assert result["result"]["refused"] is True
        assert result["result"]["smoke_passed"] is False
        assert result["result"]["entitlement_verified"] is False


@pytest.mark.parametrize("content_type,expected", [
    ("application/json; charset=utf-8", "application/json"),
    ("private-header-sentinel/token", "other")])
def test_non_sse_http200_is_unknown_with_only_allowlisted_header_diagnostics(tmp_path, content_type, expected):
    def handler(request):
        return catalog() if request.method == "GET" else httpx.Response(200,
            headers={"Content-Type": content_type}, content=b'{"secret":"private-body-sentinel"}')
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == "invalid_response"
        diagnostic = result["diagnostics"]
        assert diagnostic["http_status"] == 200 and diagnostic["content_type"] == expected
        assert diagnostic["phase"] == "sse_content_type"
        assert diagnostic["failure_class"] == "parse_or_schema"
        assert "private" not in json.dumps(result)


def test_json_parse_diagnostics_record_positions_not_input_or_exception_message(tmp_path):
    def handler(request):
        return catalog() if request.method == "GET" else httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, content=b'data: private-body-sentinel\n\n')
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        diagnostic = result["diagnostics"]
        assert diagnostic["phase"] == "sse_json" and diagnostic["exception_kind"] == "JSONDecodeError"
        assert diagnostic["json_error_line"] == 1 and diagnostic["json_error_column"] == 1
        assert diagnostic["received_bytes"] > 0 and diagnostic["event_bytes"] > 0
        assert result["state"] == "unknown" and "private" not in instance._state_path.read_text()


def test_explicit_model_terminal_error_is_distinct_from_parser_failure(tmp_path):
    def handler(request):
        return catalog() if request.method == "GET" else sse({"type": "response.failed", "response": {
            "error": {"code": "private-error-code-sentinel", "message": "private-body-sentinel"}}})
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        diagnostic = result["diagnostics"]
        assert result["state"] == "failed" and result["error_code"] == "provider_failed"
        assert diagnostic["failure_class"] == "provider_terminal"
        assert diagnostic["last_event_type"] == "response.failed"
        assert diagnostic["provider_error_present"] is True
        assert "provider_code" not in diagnostic and "private" not in json.dumps(result)


def test_completed_bad_content_is_schema_unknown_not_model_failure(tmp_path):
    def handler(request):
        return catalog() if request.method == "GET" else sse({"type": "response.completed", "response": {
            "status": "completed", "output": [{"type": "message", "content": None}]}})
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        diagnostic = result["diagnostics"]
        assert result["state"] == "unknown" and result["error_code"] == "invalid_response"
        assert diagnostic["response_completed_seen"] is True
        assert diagnostic["phase"] == "response_output_schema"
        assert diagnostic["schema_issue"] == "message_content_not_bounded_array"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_fragmented_unicode_named_sse_events_work_and_diagnostics_do_not_store_text(tmp_path, newline):
    events = [{"type": "response.output_text.delta", "delta": "连接成功"},
              {"type": "response.completed", "response": {"status": "completed", "output": [], "usage": None}}]
    data = "".join("event: " + event["type"] + newline + "data: " + json.dumps(event, ensure_ascii=False)
                   + newline + newline for event in events).encode()
    class Fragmented(httpx.SyncByteStream):
        def __iter__(self):
            for byte in data:
                yield bytes([byte])
    def handler(request):
        return catalog() if request.method == "GET" else httpx.Response(200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=Fragmented())
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "completed" and result["result"]["text"] == "连接成功"
        diagnostic = result["diagnostics"]
        assert diagnostic["events_received"] == 2
        assert diagnostic["last_event_type"] == "response.completed"
        assert diagnostic["received_bytes"] == len(data)
        assert "连接" not in json.dumps(diagnostic, ensure_ascii=False)


def test_unknown_event_type_never_enters_diagnostics_and_old_unknown_is_not_backfilled(tmp_path):
    def handler(request):
        return catalog() if request.method == "GET" else sse({"type": "private-event-body-sentinel"})
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == "stream_incomplete"
        assert result["diagnostics"]["last_event_type"] == "other"
        assert result["diagnostics"]["failure_class"] == "missing_terminal"
        assert "private" not in instance._state_path.read_text()
        # Simulate an existing pre-diagnostics attempt; reading and rejecting a
        # duplicate must not migrate, annotate or alter its historical receipt.
        ledger = json.loads(instance._state_path.read_text())
        ledger["attempts"][-1].pop("diagnostics")
        instance._state_path.write_text(json.dumps(ledger))
        before = instance._state_path.read_bytes()
        instance.status()
        with pytest.raises(ChatGPTProbeError):
            instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert instance._state_path.read_bytes() == before


def test_absent_content_type_allows_only_valid_completed_sse(tmp_path):
    def handler(request):
        if request.method == "GET":
            return catalog()
        response = complete()
        response.headers.pop("Content-Type")
        return response
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "completed" and result["result"]["smoke_passed"] is True
        diagnostic = result["diagnostics"]
        assert diagnostic["content_type"] == "absent"
        assert diagnostic["received_bytes"] > 0 and diagnostic["events_received"] == 2
        assert diagnostic["response_completed_seen"] is True


@pytest.mark.parametrize("body,code", [
    (b"", "stream_incomplete"),
    (b'{"status":"completed","text":"private-json-body-sentinel"}', "stream_incomplete"),
    (b"<html>private-html-body-sentinel</html>", "stream_incomplete"),
    (b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n', "stream_incomplete"),
    (b"data: [DONE]\n\n", "stream_incomplete"),
    (b"data: private-invalid-json-sentinel\n\n", "invalid_response"),
])
def test_absent_header_does_not_make_eof_json_html_or_partial_output_successful(tmp_path, body, code):
    def handler(request):
        return catalog() if request.method == "GET" else httpx.Response(200, content=body)
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == code
        assert result["diagnostics"]["content_type"] == "absent"
        assert result["diagnostics"]["response_completed_seen"] is False
        assert "private" not in instance._state_path.read_text()
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="text-0002", plan_only_confirmed=True)
        assert failure.value.code == "result_unknown"


def test_absent_header_still_rejects_unexpected_tool_without_execution(tmp_path):
    def handler(request):
        if request.method == "GET":
            return catalog()
        response = sse({"type": "response.output_item.done", "item": {
            "type": "function_call", "name": "private-tool-sentinel"}})
        response.headers.pop("Content-Type")
        return response
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "failed" and result["error_code"] == "unexpected_tool"
        assert result["diagnostics"]["failure_class"] == "unexpected_output"
        assert result["diagnostics"]["content_type"] == "absent"
        assert "private" not in instance._state_path.read_text()


@pytest.mark.parametrize("body", [b"data: " + b"x" * (module.MAX_EVENT_BYTES + 1) + b"\n\n",
                                  b"x" * (module.MAX_STREAM_BYTES + 1)])
def test_absent_header_cannot_bypass_event_or_stream_limits(tmp_path, body):
    def handler(request):
        return catalog() if request.method == "GET" else httpx.Response(200, content=body)
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == "invalid_response"
        assert result["diagnostics"]["content_type"] == "absent"
        assert result["diagnostics"]["phase"] == "sse_bytes"


def test_explicit_json_content_type_is_still_rejected_even_if_body_is_completed_sse(tmp_path):
    def handler(request):
        if request.method == "GET":
            return catalog()
        response = complete()
        response.headers["Content-Type"] = "application/json"
        return response
    with probe(tmp_path, handler) as instance:
        discover(instance)
        result = instance.text(request_id="text-0001", plan_only_confirmed=True)
        assert result["state"] == "unknown" and result["error_code"] == "invalid_response"
        assert result["diagnostics"]["phase"] == "sse_content_type"
        assert result["diagnostics"]["received_bytes"] == 0


def legacy_missing_header_unknown(instance, *, kind="text", frames=(), request_id="legacy-0001"):
    """Synthetic historical receipt from the pre-fix parser; no provider POST."""
    attempt, _ = instance._begin(kind, "model-z", frames, request_id, lambda: False)
    return instance._finish(attempt, "unknown", error_code="invalid_response", diagnostics={
        "phase": "sse_content_type", "content_type": "absent", "http_status": 200,
        "received_bytes": 0, "events_received": 0})


def test_explicit_single_retry_preserves_original_receipt_and_replays_new_terminal_id(tmp_path):
    posts = []
    def handler(request):
        if request.method == "GET":
            return catalog()
        posts.append(request)
        return complete()
    with probe(tmp_path, handler) as instance:
        discover(instance)
        original = legacy_missing_header_unknown(instance)
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="retry-0001", plan_only_confirmed=True)
        assert failure.value.code == "result_unknown"
        result = instance.text(request_id="retry-0001", plan_only_confirmed=True,
                               retry_unknown_from="legacy-0001")
        assert result["state"] == "completed" and result["retry_of"] == "legacy-0001"
        assert instance.status()["attempts"][1] == original
        assert instance.text(request_id="retry-0001", plan_only_confirmed=True,
            retry_unknown_from="legacy-0001") == result
        assert instance.text(request_id="retry-0001", plan_only_confirmed=True) == result
        with pytest.raises(ChatGPTProbeError) as used:
            instance.text(request_id="retry-0002", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert used.value.code == "retry_already_used"
        assert instance.status()["attempts_used"] == 3
    assert len(posts) == 1


@pytest.mark.parametrize("change", [
    {"state": "failed"}, {"state": "started"}, {"error_code": "stream_incomplete"},
    {"diagnostics": None}, {"diagnostics": {"phase": "sse_json", "content_type": "absent"}},
    {"diagnostics": {"phase": "sse_content_type", "content_type": "application/json"}},
    {"diagnostics": {"phase": "sse_content_type", "content_type": "absent", "http_status": 200, "received_bytes": 1, "events_received": 0}},
    {"diagnostics": {"phase": "sse_content_type", "content_type": "absent", "http_status": 403, "received_bytes": 0, "events_received": 0}},
    {"retry_of": "earlier-0001"},
])
def test_retry_is_not_a_general_unknown_override(tmp_path, change):
    posts = []
    def handler(request):
        if request.method == "GET":
            return catalog()
        posts.append(request)
        return complete()
    with probe(tmp_path, handler) as instance:
        discover(instance)
        legacy_missing_header_unknown(instance)
        ledger = json.loads(instance._state_path.read_text())
        ledger["attempts"][-1].update(change)
        instance._state_path.write_text(json.dumps(ledger))
        before = instance._state_path.read_bytes()
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="retry-0001", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert failure.value.code == "retry_not_allowed"
        assert instance._state_path.read_bytes() == before
    assert not posts


def test_retry_requires_same_account_kind_model_and_frame_hashes(tmp_path, monkeypatch):
    auth = FakeAuth()
    posts = []
    def handler(request):
        if request.method == "GET":
            return catalog()
        posts.append(request)
        return complete()
    with probe(tmp_path, handler, auth) as instance:
        discover(instance)
        legacy_missing_header_unknown(instance)
        with pytest.raises(ChatGPTProbeError) as model:
            instance.text(model="model-a", request_id="retry-0001", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert model.value.code == "retry_not_allowed"
        monkeypatch.setattr(module, "load_designated_frames", lambda: (frame(),))
        with pytest.raises(ChatGPTProbeError) as kind:
            instance.frames(request_id="retry-0001", plan_only_confirmed=True,
                            retry_unknown_from="legacy-0001")
        assert kind.value.code == "retry_not_allowed"
        original_frames = (frame(),)
        legacy_missing_header_unknown(instance, kind="frames", frames=original_frames, request_id="legacy-frames-0001")
        changed = BoundFrame(SAMPLE_SHA256, SAMPLE_ASSET_ID, 1, "shot-0001", "0", jpeg(width=322))
        monkeypatch.setattr(module, "load_designated_frames", lambda: (changed,))
        with pytest.raises(ChatGPTProbeError) as images:
            instance.frames(request_id="retry-frames-0001", plan_only_confirmed=True,
                            retry_unknown_from="legacy-frames-0001")
        assert images.value.code == "retry_not_allowed"
        auth.account = "verified-account-b"
        assert instance.models(request_id="catalog-0002")["state"] == "completed"
        with pytest.raises(ChatGPTProbeError) as account:
            instance.text(request_id="retry-0001", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert account.value.code == "retry_not_allowed"
    assert not posts


def test_successful_same_frame_retry_uses_new_id_only_and_still_requires_plan_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "load_designated_frames", lambda: (frame(),))
    with probe(tmp_path) as instance:
        discover(instance)
        original = legacy_missing_header_unknown(instance, kind="frames", frames=(frame(),))
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.frames(request_id="retry-0001", retry_unknown_from="legacy-0001")
        assert failure.value.code == "plan_confirmation_required"
        result = instance.frames(request_id="retry-0001", plan_only_confirmed=True,
                                 retry_unknown_from="legacy-0001")
        assert result["state"] == "completed" and result["retry_of"] == original["request_id"]
        assert instance.status()["attempts"][1] == original


def test_concurrent_retry_reserves_only_one_linked_attempt(tmp_path):
    entered, release = threading.Event(), threading.Event()
    posts = []
    def handler(request):
        if request.method == "GET":
            return catalog()
        posts.append(request)
        entered.set()
        assert release.wait(3)
        return complete()
    with probe(tmp_path, handler) as instance, ThreadPoolExecutor(max_workers=2) as pool:
        discover(instance)
        legacy_missing_header_unknown(instance)
        first = pool.submit(instance.text, request_id="retry-0001", plan_only_confirmed=True,
                            retry_unknown_from="legacy-0001")
        assert entered.wait(3)
        try:
            with probe(tmp_path, handler) as other:
                with pytest.raises(ChatGPTProbeError) as failure:
                    other.text(request_id="retry-0002", plan_only_confirmed=True,
                               retry_unknown_from="legacy-0001")
                assert failure.value.code == "retry_already_used"
        finally:
            release.set()
        assert first.result()["state"] == "completed"
        assert len([a for a in instance.status()["attempts"] if a.get("retry_of") == "legacy-0001"]) == 1
    assert len(posts) == 1


def test_retry_counts_against_eight_attempts_and_unknown_retry_cannot_chain(tmp_path):
    with probe(tmp_path) as instance:
        discover(instance)
        legacy_missing_header_unknown(instance)
        for index in range(6):
            instance.models(request_id=f"catalog-{index + 2:04}")
        with pytest.raises(ChatGPTProbeError) as failure:
            instance.text(request_id="retry-0001", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert failure.value.code == "limit_reached"
        assert instance.status()["attempts_used"] == 8

    def handler(request):
        return catalog() if request.method == "GET" else sse({"type": "response.output_text.delta", "delta": "partial"})
    with ChatGPTProbe(FakeAuth(), state_dir=tmp_path / "second-private-probe",
                     transport=httpx.MockTransport(handler)) as instance:
        discover(instance)
        legacy_missing_header_unknown(instance)
        result = instance.text(request_id="retry-0001", plan_only_confirmed=True,
                               retry_unknown_from="legacy-0001")
        assert result["state"] == "unknown"
        with pytest.raises(ChatGPTProbeError) as same:
            instance.text(request_id="retry-0001", plan_only_confirmed=True,
                          retry_unknown_from="legacy-0001")
        assert same.value.code == "result_unknown"
        with pytest.raises(ChatGPTProbeError) as chained:
            instance.text(request_id="retry-0002", plan_only_confirmed=True,
                          retry_unknown_from="retry-0001")
        assert chained.value.code == "retry_not_allowed"


def test_cli_retry_flag_is_optional_and_restricted_to_explicit_run_with_new_id():
    path = Path(__file__).resolve().parents[2] / "scripts/check_chatgpt_connection.py"
    spec = importlib.util.spec_from_file_location("probe_retry_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.parse_args([]).retry_unknown_from is None
    for args in (["--retry-unknown-from", "legacy-0001"],
                 ["--run-text", "--request-id", "legacy-0001", "--plan-only-confirmed", "--retry-unknown-from", "legacy-0001"],
                 ["--run-text", "--request-id", "retry-0001", "--retry-unknown-from", "legacy-0001"]):
        with pytest.raises(SystemExit):
            cli.parse_args(args)
    assert cli.parse_args(["--run-frames", "--request-id", "retry-0001", "--plan-only-confirmed",
                           "--retry-unknown-from", "legacy-0001"]).retry_unknown_from == "legacy-0001"
