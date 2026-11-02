#!/usr/bin/env python3
"""Real pinned binary + synthetic loopback Responses POC. No real model or key.

Run with backend/.venv/bin/python scripts/check_codex_bridge.py --binary PATH.
Only a compact evidence JSON is saved; no private media, tokens or raw logs.
"""
import argparse
import asyncio
import base64
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.codex_bridge import (  # noqa: E402
    BridgeConfig, CodexBridge, CodexBridgeError, ReadOnlyMediaTools,
    RUNTIME_VERSION, evaluate_native_surface,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jjuQAAAAASUVORK5CYII=")
ASSET = {"project_id": "poc-project", "asset_id": "poc-asset", "version": 1,
         "duration_ms": 1000, "width": 640, "height": 320}


def response_events(item, response_id):
    return [{"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": response_id, "usage": {
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}]


class MockResponses:
    def __init__(self, first_item):
        self.first_item, self.requests, self.authorized = first_item, [], []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/responses" or not 0 < length < 2 * 1024 * 1024:
                    self.send_error(400)
                    return
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    self.send_error(400)
                    return
                outer.requests.append(data)
                outer.authorized.append(self.headers.get("Authorization") == "Bearer synthetic-poc-access-token")
                item = outer.first_item if len(outer.requests) == 1 else {
                    "type": "message", "role": "assistant", "id": "poc-message",
                    "content": [{"type": "output_text", "text": "synthetic-protocol-completed"}]}
                events = response_events(item, f"poc-response-{len(outer.requests)}")
                payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.send_error(404)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"


def tool_names(requests):
    names = set()
    def add(tools):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace":
                add(tool.get("tools", []))
            else:
                names.add(tool.get("name", tool.get("type", "unknown")))
    for request in requests:
        add(request.get("tools", []))
        # 0.164 may send schemas as incremental input declarations rather than
        # a top-level tools field. An empty top-level field is not isolation.
        for item in request.get("input", []):
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                add(item.get("tools", []))
    return names


def tool_outputs(requests):
    return [item.get("output") for request in requests for item in request.get("input", [])
            if isinstance(item, dict) and item.get("call_id") == "poc-call"
            and item.get("type") in ("function_call_output", "custom_tool_call_output")]


async def run_case(binary, root, first_item):
    with MockResponses(first_item) as mock:
        bridge = CodexBridge(BridgeConfig(binary, root, provider_base_url=mock.origin,
            protocol_poc=True, timeout_seconds=20), ReadOnlyMediaTools([ASSET]),
            access_token="synthetic-poc-access-token")
        try:
            await bridge.start()
            await bridge.start_thread()
            turn = await bridge.turn("Synthetic protocol POC. Use the supplied read-only media tool.")
            summary = {"completed": turn["status"] == "completed", "tool_calls": turn["tool_calls"],
                       "http_requests": len(mock.requests), "bearer_header_supplied": all(mock.authorized),
                       "event_methods": sorted(set(bridge.events)),
                       "tool_names": sorted(tool_names(mock.requests))}
            return summary, tool_outputs(mock.requests)
        except CodexBridgeError as exc:
            return {"completed": False, "safe_error": str(exc), "http_requests": len(mock.requests),
                    "event_methods": sorted(set(bridge.events)), "tool_names": sorted(tool_names(mock.requests))}, tool_outputs(mock.requests)
        finally:
            await bridge.close()


async def main(binary, output):
    evidence = {"runtime_version": RUNTIME_VERSION, "account_entitlement_tested": False,
                "real_model_called": False, "transport": "stdio -> 127.0.0.1 synthetic Responses SSE"}
    with tempfile.TemporaryDirectory(prefix="studio-codex-probe-") as temp:
        root = Path(temp)
        secret_png = root / "outside-workspace.png"
        secret_png.write_bytes(PNG)
        write_target = root / "outside-workspace-write.txt"
        shell_target = root / "outside-shell-write.txt"
        cases = {
            "dynamic_read": {"type": "function_call", "call_id": "poc-call", "namespace": "studio_media",
                "name": "read_asset_manifest", "arguments": json.dumps({"project_id": "poc-project", "asset_id": "poc-asset"})},
            "native_read": {"type": "function_call", "call_id": "poc-call", "name": "view_image",
                "arguments": json.dumps({"path": str(secret_png)})},
            "native_write": {"type": "custom_tool_call", "call_id": "poc-call", "name": "apply_patch",
                "input": f"*** Begin Patch\n*** Add File: {write_target}\n+SYNTHETIC-PROBE\n*** End Patch"},
            "native_shell": {"type": "function_call", "call_id": "poc-call", "name": "exec_command",
                "arguments": json.dumps({"cmd": f"/usr/bin/touch '{shell_target}'"})},
        }
        outputs = {}
        for name, first in cases.items():
            summary, returned = await run_case(binary, root / "sessions", first)
            evidence[name] = summary
            outputs[name] = json.dumps(returned)
        denied = re.compile(r"unsupported|not available|not found|no environment|unknown|not allowed|disabled|rejected|no turn environment|no usable execution environment", re.I)
        outside_read_blocked = bool(denied.search(outputs["native_read"])) and "data:image" not in outputs["native_read"]
        outside_write_blocked = not write_target.exists() and bool(denied.search(outputs["native_write"]))
        evidence["outside_read_blocked"] = outside_read_blocked
        evidence["outside_write_blocked"] = outside_write_blocked
        evidence["shell_marker_created"] = shell_target.exists()
        evidence["dynamic_result_matches_snapshot"] = 'poc-asset' in outputs["dynamic_read"] and 'duration_ms' in outputs["dynamic_read"]
        evidence["isolation_gate"] = asdict(evaluate_native_surface(set(evidence["dynamic_read"]["tool_names"]),
            outside_reads_blocked=outside_read_blocked, outside_writes_blocked=outside_write_blocked))
        evidence["recommended_route"] = "app-server candidate" if evidence["isolation_gate"]["passed"] else "direct SiWC Responses + business tool gateway"
        # Outputs are synthetic; retain only bounded denial excerpts, never full requests.
        evidence["denial_evidence"] = {name: outputs[name][:300] for name in ("native_read", "native_write", "native_shell")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"evidence_file": str(output), "dynamic_round_trip": evidence["dynamic_result_matches_snapshot"],
        "gate": evidence["isolation_gate"], "recommended_route": evidence["recommended_route"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".runtime/poc20261027/codex-protocol-evidence.json"))
    args = parser.parse_args()
    asyncio.run(main(args.binary.resolve(), args.output))
