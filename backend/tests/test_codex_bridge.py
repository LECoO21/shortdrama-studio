"""Security-boundary tests. No real account, model or personal Codex state."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.codex_bridge import (
    BridgeConfig, CodexBridge, CodexBridgeError, CodexGateFailed,
    ReadOnlyMediaTools, evaluate_native_surface,
)


ASSET = {"project_id": "project-1", "asset_id": "asset-1", "version": 1,
         "duration_ms": 1000, "width": 640, "height": 320}


def bridge(tmp_path):
    return CodexBridge(BridgeConfig(Path("unused"), tmp_path,
        provider_base_url="http://127.0.0.1:1234/v1", protocol_poc=True),
        ReadOnlyMediaTools([ASSET]), access_token="synthetic-test-access-token")


def test_media_tools_return_only_bound_immutable_metadata():
    original = ASSET.copy()
    tools = ReadOnlyMediaTools([original])
    original["width"] = 1
    result = tools.call("studio_media", "read_asset_manifest", {
        "project_id": "project-1", "asset_id": "asset-1"})
    assert result["success"]
    assert json.loads(result["contentItems"][0]["text"]) == ASSET
    assert "path" not in json.dumps(tools.specs)


@pytest.mark.parametrize("namespace,tool,args", [
    ("studio_media", "read_asset_manifest", {"project_id": "other", "asset_id": "asset-1"}),
    ("studio_media", "read_asset_manifest", {"project_id": "project-1", "asset_id": "../secret"}),
    ("studio_media", "read_asset_manifest", {"project_id": "project-1", "asset_id": "asset-1", "path": "/private/sentinel"}),
    ("studio_media", "read_asset_manifest", {"project_id": ["project-1"], "asset_id": "asset-1"}),
    ("studio_media", "read_asset_manifest", []),
    (None, "exec_command", {"cmd": "private-sentinel-command"}),
    ("studio_media", "enqueue_render", {}),
])
def test_dynamic_tools_reject_cross_project_paths_unknown_tools_and_shapes(namespace, tool, args):
    result = ReadOnlyMediaTools([ASSET]).call(namespace, tool, args)
    assert result["success"] is False
    assert "sentinel" not in json.dumps(result)


def test_snapshot_rejects_hidden_paths_and_bound_limits():
    with pytest.raises(CodexBridgeError):
        ReadOnlyMediaTools([ASSET | {"path": "/private/sentinel"}])
    with pytest.raises(CodexBridgeError):
        ReadOnlyMediaTools([ASSET, ASSET])
    with pytest.raises(CodexBridgeError):
        ReadOnlyMediaTools([ASSET] * 1001)


def test_child_environment_does_not_inherit_secrets_proxy_or_personal_home(tmp_path, monkeypatch):
    for name in ("CODEX_HOME", "HOME", "OPENAI_API_KEY", "DASHSCOPE_API_KEY", "HTTPS_PROXY", "DYLD_INSERT_LIBRARIES"):
        monkeypatch.setenv(name, "private-sentinel")
    env = CodexBridge.child_environment(tmp_path, "own-synthetic-token")
    assert env["ACCESS_TOKEN"] == "own-synthetic-token"
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert env["HOME"] == str(tmp_path / "home")
    assert "private-sentinel" not in json.dumps(env)
    assert set(env) == {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "ACCESS_TOKEN", "LANG", "RUST_LOG"}


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://localhost:12/v1",
    "http://127.0.0.1.evil:12/v1", "http://user@127.0.0.1:12/v1", "http://127.0.0.1:12/v1?secret=1"])
def test_poc_cannot_target_real_or_ambiguous_service(tmp_path, url):
    with pytest.raises(CodexBridgeError):
        BridgeConfig(Path("unused"), tmp_path, provider_base_url=url, protocol_poc=True).validate()


def test_live_bridge_fails_before_process_even_with_token(tmp_path):
    instance = CodexBridge(BridgeConfig(Path("unused"), tmp_path), ReadOnlyMediaTools([]),
                           access_token="synthetic-token")
    assert "synthetic-token" not in repr(instance)
    with pytest.raises(CodexGateFailed):
        asyncio.run(instance.start())
    assert not instance.running
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("method,params", [
    ("thread/shellCommand", {"threadId": "t", "command": "touch sentinel"}),
    ("command/exec", {"command": ["touch", "sentinel"]}),
    ("fs/readFile", {"path": "/private/sentinel"}),
    ("config/value/write", {"keyPath": "sandbox_mode", "value": "danger-full-access"}),
    ("account/login/start", {"type": "apiKey", "apiKey": "secret"}),
    ("turn/start", {"threadId": "t", "input": [], "config": {}}),
    ("turn/start", {"threadId": "t", "input": [], "environments": [{"cwd": "/private"}], "approvalPolicy": "never"}),
])
def test_rpc_proxy_and_privilege_escalation_are_rejected_before_transport(tmp_path, method, params):
    with pytest.raises(CodexBridgeError):
        asyncio.run(bridge(tmp_path)._request(method, params))


def test_approvals_refresh_and_unknown_server_requests_default_to_deny(tmp_path):
    async def scenario():
        instance = bridge(tmp_path)
        sent = []
        async def capture(message):
            sent.append(message)
        instance._send = capture
        for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                       "item/permissions/requestApproval", "account/chatgptAuthTokens/refresh", "unrecognized/request"):
            await instance._server_message({"id": 1, "method": method, "params": {}})
        assert sent[:2] == [{"id": 1, "result": {"decision": "decline"}}] * 2
        assert all(item["error"]["code"] == -32601 for item in sent[2:])
    asyncio.run(scenario())


def test_dynamic_tool_calls_are_bound_to_thread_turn_and_count_limit(tmp_path):
    async def scenario():
        instance = bridge(tmp_path)
        instance._thread_id, instance._turn_id = "thread-1", "turn-1"
        sent = []
        async def capture(message):
            sent.append(message)
        instance._send = capture
        params = {"threadId": "thread-1", "turnId": "turn-1", "namespace": "studio_media",
                  "tool": "read_asset_manifest", "arguments": {"project_id": "project-1", "asset_id": "asset-1"}}
        await instance._server_message({"id": 1, "method": "item/tool/call", "params": params | {"turnId": "other"}})
        assert sent[-1]["result"]["success"] is False
        instance._tool_calls = 0
        for _ in range(25):
            await instance._server_message({"id": 1, "method": "item/tool/call", "params": params})
        assert all(item["result"]["success"] for item in sent[1:25])
        assert sent[-1]["result"]["success"] is False
    asyncio.run(scenario())


def test_isolation_gate_requires_minimal_surface_and_both_negative_probes():
    gate = evaluate_native_surface({"read_asset_manifest", "apply_patch"},
        outside_reads_blocked=True, outside_writes_blocked=True)
    assert not gate.passed and len(gate.reasons) == 1
    assert not evaluate_native_surface({"read_asset_manifest"},
        outside_reads_blocked=False, outside_writes_blocked=True).passed
    assert evaluate_native_surface({"read_asset_manifest"},
        outside_reads_blocked=True, outside_writes_blocked=True).passed


class _FakeStdin:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        await asyncio.sleep(0)


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_timeout_stops_child_and_cancels_waiters(tmp_path):
    async def scenario():
        instance = bridge(tmp_path)
        instance.config = replace(instance.config, timeout_seconds=0.05)
        process = _FakeProcess()
        instance._process, instance._thread_id = process, "thread-1"
        with pytest.raises(CodexBridgeError, match="超时"):
            await instance.turn("synthetic input")
        assert not instance.running
        assert not instance._pending
        assert instance._turn_done.done()
        assert instance._access_token == ""
    asyncio.run(scenario())


def test_cancel_stops_child_without_refresh_or_retry(tmp_path):
    async def scenario():
        instance = bridge(tmp_path)
        process = _FakeProcess()
        instance._process, instance._thread_id = process, "thread-1"
        task = asyncio.create_task(instance.turn("synthetic input"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not instance.running
        assert len(process.stdin.sent) == 1  # No automatic repeat or auth fallback.
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [b"not-json private-sentinel\n", b"[]\n",
    b'{"method":"item/completed","params":[]}\n'])
def test_malformed_stdout_kills_child_and_sanitizes_error(tmp_path, payload):
    async def scenario():
        instance = bridge(tmp_path)
        process = _FakeProcess()
        process.stdout.feed_data(payload)
        process.stdout.feed_eof()
        instance._process = process
        pending = asyncio.get_running_loop().create_future()
        instance._pending[1] = pending
        await instance._read_loop()
        assert not instance.running
        with pytest.raises(CodexBridgeError) as failure:
            await pending
        assert "private-sentinel" not in str(failure.value)
        await instance.close()
    asyncio.run(scenario())


def test_wrong_or_alpha_binary_never_starts(tmp_path):
    binary = tmp_path / "fake-alpha-codex"
    binary.write_bytes(b"not-the-signed-release")
    instance = bridge(tmp_path)
    instance.config = replace(instance.config, binary=binary)
    with pytest.raises(CodexBridgeError, match="0.164.0"):
        asyncio.run(instance.start())
    assert not instance.running
    assert instance._temp is None
