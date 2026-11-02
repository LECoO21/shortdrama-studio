#!/usr/bin/env python3
"""Independent local-ASR POC; default reads preparation status only.

--prepare downloads only public pinned source/build tool/model files. No user
media is uploaded. --run-sample uses only the already-imported designated sample
after the real backup gate, and never writes its transcript into a live project.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import urllib.error
import urllib.request
import wave
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.local_asr import (  # noqa: E402
    BoundAudio, DEFAULT_ROOT, LocalASRError, WHISPER_COMMIT, WHISPER_VERSION,
    load_designated_audio, local_model_spec, private_root, run_local_asr, sha256,
)

SOURCE_URL = "https://github.com/ggml-org/whisper.cpp.git"
CMAKE_URL = "https://files.pythonhosted.org/packages/3f/02/86b484e16c91de4bdf90dfab37a33c3e5512bc30d3a494759976df0943b9/cmake-4.4.4-py3-none-macosx_10_10_universal2.whl"
CMAKE_SHA256 = "883962a72b1b3a16ff445427f24c8caca9797dd9e563bbe208cbcd89e877f9d3"
CMAKE_BYTES = 54404900


def _download(url: str, target: Path, *, expected_bytes: int, expected_sha256: str):
    """Resume only verified byte ranges; publish only a complete hash match."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        if target.stat().st_size == expected_bytes and sha256(target) == expected_sha256:
            return
        raise LocalASRError("download_mismatch", "已有下载文件校验不符，未覆盖")
    partial = target.with_name(target.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset >= expected_bytes:
        if offset == expected_bytes and sha256(partial) == expected_sha256:
            partial.chmod(0o600)
            partial.replace(target)
            return
        raise LocalASRError("download_mismatch", "断点文件校验不符，需保留诊断后重新准备")
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        if offset and response.status == 206:
            if not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                raise LocalASRError("invalid_range", "模型源返回的断点范围不一致")
            mode = "ab"
        elif response.status == 200:
            mode = "wb"
            offset = 0
        else:
            raise LocalASRError("download_failed", "模型源没有返回可校验的文件内容")
        with partial.open(mode) as output:
            os.fchmod(output.fileno(), 0o600)
            for block in iter(lambda: response.read(1024 * 1024), b""):
                offset += len(block)
                if offset > expected_bytes:
                    raise LocalASRError("download_mismatch", "下载大小超出固定模型范围")
                output.write(block)
    if partial.stat().st_size != expected_bytes or sha256(partial) != expected_sha256:
        raise LocalASRError("download_mismatch", "下载未完成或校验不符，未启用该文件")
    partial.replace(target)


def _local_command(arguments, log: Path):
    with log.open("ab") as output:
        os.fchmod(output.fileno(), 0o600)
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            env={key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}})
    if result.returncode != 0:
        raise LocalASRError("prepare_failed", f"本地准备失败，诊断保留在 {log}")


def prepare(root: Path, model_size: str = "small") -> dict:
    specification = local_model_spec(model_size)
    root = private_root(root)
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise LocalASRError("unsupported_host", "当前准备流程已验证范围是 Apple Silicon macOS；不会自动安装其他平台依赖")
    source = root / f"whisper.cpp-v{WHISPER_VERSION}"
    build = root / f"build-v{WHISPER_VERSION}"
    log = root / "preparation.log"
    provenance = {"whisper_source": {"repository": SOURCE_URL, "tag": f"v{WHISPER_VERSION}",
            "commit": WHISPER_COMMIT, "license": "MIT"},
        "model": {**specification, "license_reference": "https://github.com/openai/whisper/blob/main/LICENSE"},
        "build_tool": {"name": "cmake", "version": "4.4.4", "url": CMAKE_URL,
            "bytes": CMAKE_BYTES, "sha256": CMAKE_SHA256}, "network_purpose": "public_downloads_only"}
    # Preserve earlier small provenance and each model's independent record.
    provenance_path = root / f"poc-sources-{model_size}.json"
    provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2))
    provenance_path.chmod(0o600)
    if not source.exists():
        _local_command(["git", "clone", "--depth", "1", "--branch", f"v{WHISPER_VERSION}", SOURCE_URL, str(source)], log)
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != WHISPER_COMMIT:
        raise LocalASRError("source_mismatch", "whisper.cpp 源码不匹配固定提交，未构建")
    cmake = root / "build-tools/bin/cmake"
    if not cmake.is_file():
        wheel = root / "tools/cmake-4.4.4.whl"
        _download(CMAKE_URL, wheel, expected_bytes=CMAKE_BYTES, expected_sha256=CMAKE_SHA256)
        extracted = root / "tools/cmake-4.4.4"
        with zipfile.ZipFile(wheel) as archive:
            if any(Path(info.filename).is_absolute() or ".." in Path(info.filename).parts for info in archive.infolist()):
                raise LocalASRError("invalid_archive", "构建工具归档路径无效")
            archive.extractall(extracted)
        cmake = extracted / "cmake/data/bin/cmake"
        cmake.chmod(0o700)
    _local_command([str(cmake), "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DWHISPER_BUILD_IS_DEV=OFF", "-DWHISPER_BUILD_TESTS=OFF",
        "-DWHISPER_BUILD_SERVER=OFF", "-DWHISPER_CURL=OFF", "-DBUILD_SHARED_LIBS=OFF",
        "-DGGML_METAL=ON", "-DGGML_METAL_EMBED_LIBRARY=ON"], log)
    _local_command([str(cmake), "--build", str(build), "--target", "whisper-cli", "--parallel", "4"], log)
    _download(specification["url"], root / "models" / specification["filename"],
        expected_bytes=specification["bytes"], expected_sha256=specification["sha256"])
    return status(root, model_size)


def status(root: Path, model_size: str = "small") -> dict:
    specification = local_model_spec(model_size)
    binary = root / f"build-v{WHISPER_VERSION}/bin/whisper-cli"
    model = root / "models" / specification["filename"]
    return {"engine": "whisper.cpp", "version": WHISPER_VERSION, "commit": WHISPER_COMMIT,
        "binary_exists": binary.is_file(), "model_exists": model.is_file(),
        "model_bytes": model.stat().st_size if model.is_file() else 0,
        "selected_model": model_size, "expected_model_bytes": specification["bytes"],
        "expected_model_sha256": specification["sha256"], "private_root": str(root),
        "message": "仅本机候选验证；未接入工作台、未写入工程；模型就绪不代表对白质量通过。"}


def silence_audio(root: Path) -> BoundAudio:
    root = private_root(root)
    fixture = root / "synthetic-silence.wav"
    with wave.open(str(fixture), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 32000)
    fixture.chmod(0o600)
    digest = sha256(fixture)
    return BoundAudio(fixture, digest, digest, "synthetic-silence", 1, 0, 1, 16000, 16000, 32000)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--prepare", action="store_true", help="下载公开固定依赖并仅在私有目录构建")
    actions.add_argument("--run-silence", action="store_true", help="仅合成静音冒烟，不读取用户素材")
    actions.add_argument("--run-sample", action="store_true", help="仅使用指定样本已入库且绑定的音轨")
    parser.add_argument("--backup-verified", action="store_true", help="真实备份及读回已完成的操作记录；此参数本身不产生授权")
    parser.add_argument("--language", choices=("zh", "en", "auto"), default="zh")
    parser.add_argument("--model", choices=("small", "medium"), default="small",
        help="显式选择固定多语权重；默认small；不自动下载或切换模型")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if args.run_sample and not args.backup_verified:
        parser.error("先完成工程与素材引用备份并读回验证，再使用 --backup-verified；不会跳过备份")
    try:
        if args.prepare:
            result = prepare(DEFAULT_ROOT, args.model)
        elif args.run_silence or args.run_sample:
            audio = silence_audio(DEFAULT_ROOT) if args.run_silence else load_designated_audio()
            result = run_local_asr(audio, root=DEFAULT_ROOT, language=args.language,
                threads=args.threads, timeout_seconds=args.timeout, model_size=args.model)
        else:
            result = status(DEFAULT_ROOT, args.model)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except LocalASRError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False))
        return 1
    except (OSError, urllib.error.URLError, subprocess.SubprocessError, ValueError):
        print(json.dumps({"error": {"code": "local_failure", "message": "本地准备或运行未完成，已保留缓存；未上传用户素材。"}}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "interrupted", "message": "已中断，本地状态保留；未改动工程。"}}, ensure_ascii=False))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
