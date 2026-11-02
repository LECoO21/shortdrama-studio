#!/usr/bin/env python3
"""Manage only this checkout's local development processes (Python stdlib)."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
STATE = RUNTIME / "processes.json"
SCRIPT = Path(__file__).resolve()
PYTHON = ROOT / "backend/.venv/bin/python"
SERVICES = {
    "backend": {"cwd": ROOT / "backend", "port": 8031,
                "url": "http://127.0.0.1:8031/api/v1/health",
                "command": [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8031"]},
    "worker": {"cwd": ROOT / "backend", "port": None, "url": None,
               "command": [str(PYTHON), "-m", "app.worker"]},
    "frontend": {"cwd": ROOT / "frontend", "port": 3031,
                 "url": "http://127.0.0.1:3031",
                 "command": ["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "3031"]},
}


def read_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        value = json.loads(STATE.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError) as exc:
        raise RuntimeError("进程记录损坏；请人工检查 .runtime/processes.json，脚本不会猜测或终止进程。") from exc


def save_state(state: dict) -> None:
    temp = RUNTIME / "processes.json.tmp"
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(STATE)


def owns_process(name: str, record: dict | None) -> bool:
    """A PID alone is insufficient: verify the unique supervisor command and PGID."""
    if not isinstance(record, dict):
        return False
    pid, token = record.get("pid"), record.get("token")
    if not isinstance(pid, int) or pid <= 1 or not isinstance(token, str):
        return False
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
        result = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                capture_output=True, text=True, timeout=2, check=False)
        expected_tail = f"{SCRIPT} _run {name} {token}"
        return result.returncode == 0 and result.stdout.strip().endswith(expected_tail)
    except (OSError, subprocess.SubprocessError):
        return False


def healthy(name: str) -> bool:
    url = SERVICES[name]["url"]
    if url is None:
        return True  # Only process liveness is reported for the worker.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            if response.status != 200:
                return False
            return name != "backend" or json.load(response).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match uvicorn/Node restart behavior: TIME_WAIT is not a live listener.
        # SO_REUSEADDR still refuses an actively listening service (no REUSEPORT).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def environment() -> dict[str, str]:
    env = os.environ.copy()
    config = ROOT / ".env"
    if config.exists():
        for number, raw in enumerate(config.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
                raise RuntimeError(f".env 第 {number} 行不是 KEY=value 格式。未输出其内容。")
            if value[:1] in ("'", '"'):
                if len(value) < 2 or value[-1] != value[0]:
                    raise RuntimeError(f".env 第 {number} 行引号未配对。未输出其内容。")
                value = value[1:-1]
            env.setdefault(key, value)
    data = Path(env.get("STUDIO_DATA_DIR", str(ROOT / "data"))).expanduser()
    if not data.is_absolute():
        data = ROOT / data
    env["STUDIO_DATA_DIR"] = str(data.resolve())
    env.setdefault("FFMPEG_BIN", "ffmpeg")
    env.setdefault("NEXT_PUBLIC_API_BASE", "http://127.0.0.1:8031")
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    env["PYTHONUNBUFFERED"] = "1"
    return env


def preflight() -> None:
    if not PYTHON.is_file():
        raise RuntimeError("缺少 backend/.venv/bin/python；先按 README 创建虚拟环境并安装依赖。")
    if not (ROOT / "frontend/node_modules/next").is_dir():
        raise RuntimeError("缺少前端依赖；先在 frontend 执行 npm ci。")
    import shutil
    for command in ("node", "npm"):
        if not shutil.which(command):
            raise RuntimeError(f"PATH 中没有 {command}；按 README 配置 Node.js 22，不自动安装全局工具。")
    env = environment()
    if not shutil.which(env["FFMPEG_BIN"], path=env.get("PATH")):
        raise RuntimeError("找不到 FFmpeg；请在 .env 配置已有工具的 FFMPEG_BIN 路径。")


def stop_one(name: str, state: dict) -> bool:
    record = state.get(name)
    if not owns_process(name, record):
        if record:
            print(f"{name}: 记录已过期或身份不符，未终止任何进程。")
            state.pop(name, None)
        return True
    pid = record["pid"]
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and owns_process(name, record):
        time.sleep(0.2)
    if owns_process(name, record):
        os.killpg(pid, signal.SIGKILL)
        time.sleep(0.2)
    if owns_process(name, record):
        print(f"{name}: 停止未完成；保留进程记录，请检查日志。")
        return False
    state.pop(name, None)
    print(f"{name}: 已停止。")
    return True


def start(state: dict) -> None:
    preflight()
    for name, spec in SERVICES.items():
        if not owns_process(name, state.get(name)) and spec["port"] and not port_free(spec["port"]):
            raise RuntimeError(f"端口 {spec['port']} 已被其他进程占用；未启动或终止任何服务。")
    launched = []
    try:
        for name, spec in SERVICES.items():
            if owns_process(name, state.get(name)):
                if not healthy(name):
                    raise RuntimeError(f"{name} 已运行但健康检查未通过；请先查看该服务日志。")
                print(f"{name}: 本项目进程已运行。")
                continue
            token = uuid.uuid4().hex
            log = RUNTIME / f"{name}.log"
            with log.open("ab") as stream:
                log.chmod(0o600)
                stream.write(f"\n--- start {dt.datetime.now(dt.timezone.utc).isoformat()} ---\n".encode())
                process = subprocess.Popen([sys.executable, str(SCRIPT), "_run", name, token],
                                           cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True,
                                           env=environment())
            state[name] = {"pid": process.pid, "token": token,
                           "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            launched.append(name)
            save_state(state)
            deadline = time.monotonic() + (60 if name == "frontend" else 25)
            time.sleep(0.8)
            while time.monotonic() < deadline:
                if not owns_process(name, state[name]):
                    raise RuntimeError(f"{name} 提前退出；查看 .runtime/{name}.log（脚本不会回显日志内容）。")
                if healthy(name):
                    break
                time.sleep(0.4)
            else:
                raise RuntimeError(f"{name} 健康检查超时；查看 .runtime/{name}.log。")
            print(f"{name}: 已启动" + ("（进程存活；任务执行请在界面验证）。" if name == "worker" else "，HTTP 健康检查通过。"))
        print("工作台：http://127.0.0.1:3031  API：http://127.0.0.1:8031/docs")
    except (Exception, KeyboardInterrupt):
        for name in reversed(launched):
            stop_one(name, state)
        save_state(state)
        raise


def supervise(name: str) -> int:
    """Keep a unique, verifiable group leader until the service has exited."""
    spec = SERVICES[name]
    # The manager sends TERM to the entire process group. Keep the supervisor
    # alive while the child cleans up, so any necessary KILL can verify identity.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    try:
        child = subprocess.Popen(spec["command"], cwd=spec["cwd"], env=environment(),
                                 stdin=subprocess.DEVNULL)
        return child.wait()
    except OSError:
        print(f"{name}: 无法执行启动命令；检查依赖和运行路径。", file=sys.stderr)
        return 1


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "_run":
        if sys.argv[2] not in SERVICES or not re.fullmatch(r"[0-9a-f]{32}", sys.argv[3]):
            return 2
        return supervise(sys.argv[2])
    parser = argparse.ArgumentParser(description="管理本项目本机开发进程；不安装工具或操作系统服务。")
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    RUNTIME.chmod(0o700)
    with (RUNTIME / "manage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state()
        if args.action == "start":
            start(state)
        elif args.action == "stop":
            succeeded = [stop_one(name, state) for name in reversed(SERVICES)]
            save_state(state)
            return 0 if all(succeeded) else 1
        else:
            all_ready = True
            for name in SERVICES:
                running = owns_process(name, state.get(name))
                ready = running and healthy(name)
                all_ready = all_ready and ready
                label = "运行中（仅进程检查）" if name == "worker" and running else "健康" if ready else "运行但未就绪" if running else "未运行"
                print(f"{name}: {label}")
            return 0 if all_ready else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("操作已中断。", file=sys.stderr)
        raise SystemExit(130)
