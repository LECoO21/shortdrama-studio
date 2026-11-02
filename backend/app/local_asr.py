"""Local-only whisper.cpp POC. No HTTP, product DB writes, or cloud adapters.

Whisper token timestamps are estimates, not forced alignment or Chinese word
boundaries. ``p`` is the decoder's token probability (merged UTF-8 tokens retain
the first token's p in upstream JSON), not calibrated transcript correctness.
Every result remains a candidate requiring a listen-through before acceptance.
"""
from dataclasses import asdict, dataclass
import fcntl
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Callable
import wave


WHISPER_VERSION = "1.9.5"
WHISPER_COMMIT = "d1be6fde11ac6e0407606b4e42fe72d34add8037"
SMALL_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
SMALL_BYTES = 487601967
MEDIUM_SHA256 = "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208"
MEDIUM_BYTES = 1533763059
MODEL_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
SAMPLE_SHA256 = "9d48d2b21522e97a99651f909b003beec32f69360806ed5bfdb33b93f75a50bb"
SAMPLE_ASSET_ID = SAMPLE_SHA256[:24]
PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = Path.home() / "Library/Application Support/Shortdrama Studio/local-asr"
_SPECIAL = re.compile(r"^\[_.*\]$|^\[<.*>\]$|^<\|.*\|>$")
_NON_SPEECH_MARKERS = {"[音乐]", "[音樂]", "[Music]", "[music]", "[MUSIC]", "[掌声]", "[笑声]", "[噪音]", "[静音]"}
_MAX_RESULT_BYTES = 16 * 1024 * 1024


class LocalASRError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def local_model_spec(model_size: str = "small") -> dict:
    """Only these pinned multilingual weights; never a user-controlled URL."""
    if model_size == "small":
        size, digest = SMALL_BYTES, SMALL_SHA256
    elif model_size == "medium":
        size, digest = MEDIUM_BYTES, MEDIUM_SHA256
    else:
        raise LocalASRError("invalid_model", "本地候选仅支持显式 small 或 medium 多语权重")
    filename = f"ggml-{model_size}.bin"
    return {"name": f"{model_size}-multilingual", "size": model_size,
        "filename": filename, "bytes": size, "sha256": digest, "revision": MODEL_REVISION,
        "url": f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{MODEL_REVISION}/{filename}"}


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def private_root(path: Path = DEFAULT_ROOT) -> Path:
    supplied = path.expanduser()
    resolved = supplied.resolve()
    if (supplied.is_symlink() or PROJECT == resolved or PROJECT in resolved.parents
            or any(p in {"Mobile Documents", "com~apple~CloudDocs", "CloudStorage"}
                   for p in resolved.parts)):
        raise LocalASRError("unsafe_storage", "转写缓存须位于工程和云同步目录之外")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    if resolved.stat().st_uid != os.getuid() or not resolved.is_dir():
        raise LocalASRError("unsafe_storage", "无法使用本机私有转写目录")
    resolved.chmod(0o700)
    return resolved


def _atomic_json(path: Path, value: dict):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as target:
        os.fchmod(target.fileno(), 0o600)
        json.dump(value, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def _validate_pcm16(path: Path, expected_sample_count: int | None = None) -> bool:
    """Read every declared PCM frame; return whether complete audio is all zero.

    wave.getnframes() reports the WAV data-chunk header, even when the physical
    PCM data is truncated. A header-only file must never become confirmed silence.
    """
    try:
        with wave.open(str(path), "rb") as audio:
            sample_count = audio.getnframes()
            if ((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(),
                    audio.getcomptype()) != (1, 2, 16000, "NONE")
                    or sample_count <= 0
                    or expected_sample_count is not None and sample_count != expected_sample_count):
                raise ValueError
            expected_bytes = sample_count * 2
            actual_bytes = 0
            all_zero = True
            while block := audio.readframes(16000):
                actual_bytes += len(block)
                if len(block) % 2 or actual_bytes > expected_bytes:
                    raise ValueError
                if block.strip(b"\0"):
                    all_zero = False
            if actual_bytes != expected_bytes:
                raise ValueError
            return all_zero
    except (wave.Error, EOFError, ValueError):
        raise LocalASRError("invalid_audio_format", "需要完整且已绑定的 16kHz 单声道 PCM16 WAV，音频数据不得截断") from None


@dataclass(frozen=True)
class BoundAudio:
    path: Path
    audio_sha256: str
    source_sha256: str
    asset_id: str
    asset_version: int
    start_pts: int
    time_base_num: int
    time_base_den: int
    sample_rate: int
    sample_count: int

    @property
    def duration(self) -> Fraction:
        return Fraction(self.sample_count, self.sample_rate)

    @property
    def origin(self) -> Fraction:
        return Fraction(self.start_pts * self.time_base_num, self.time_base_den)

    def binding(self) -> dict:
        result = asdict(self)
        result.pop("path")
        return result

    def validate(self):
        if (not all(re.fullmatch(r"[a-f0-9]{64}", x) for x in
                    (self.audio_sha256, self.source_sha256))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.asset_id)
                or type(self.asset_version) is not int or self.asset_version < 1
                or any(type(x) is not int for x in (self.start_pts, self.time_base_num,
                    self.time_base_den, self.sample_rate, self.sample_count))
                or min(self.time_base_num, self.time_base_den, self.sample_count) <= 0
                or self.sample_rate != 16000 or self.duration > 4 * 3600):
            raise LocalASRError("invalid_audio_binding", "音轨绑定或时基无效")
        if self.path.is_symlink() or not self.path.is_file() or sha256(self.path) != self.audio_sha256:
            raise LocalASRError("audio_changed", "音轨内容与绑定校验不一致")
        _validate_pcm16(self.path, self.sample_count)


def load_designated_audio() -> BoundAudio:
    """Read the previously imported fixed sample; never import storage.py.

    No migration, re-extraction, arbitrary path, URL, or desktop original write.
    The caller must have completed the user-data backup gate before invoking.
    """
    data = PROJECT / "data"
    database = data / "studio.db"
    original = data / "originals" / f"{SAMPLE_ASSET_ID}.mp4"
    try:
        if database.is_symlink() or original.resolve() != original:
            raise ValueError
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            asset = connection.execute("SELECT version,sha256,path FROM assets WHERE id=?", (SAMPLE_ASSET_ID,)).fetchone()
            analysis = connection.execute("SELECT version,manifest_json FROM analyses WHERE asset_id=?", (SAMPLE_ASSET_ID,)).fetchone()
        if (asset is None or analysis is None or asset[0] != 1 or asset[1] != SAMPLE_SHA256
                or asset[2] != str(original) or analysis[0] != 1 or sha256(original) != SAMPLE_SHA256):
            raise ValueError
        manifest = json.loads(analysis[1])
        artifact_id = manifest.get("artifact_id", "")
        if (manifest.get("asset_id") != SAMPLE_ASSET_ID or manifest.get("asset_version") != 1
                or manifest.get("source_sha256") != SAMPLE_SHA256
                or manifest.get("schema_version") != 1
                or not re.fullmatch(r"[a-f0-9]{32}-a[1-3]", artifact_id)):
            raise ValueError
        audio = manifest["audio"]
        path = data / "analysis" / artifact_id / "audio.wav"
        if path.resolve() != path:
            raise ValueError
        bound = BoundAudio(path=path, audio_sha256=audio["sha256"], source_sha256=SAMPLE_SHA256,
            asset_id=SAMPLE_ASSET_ID, asset_version=1, start_pts=int(audio["start_pts"]),
            time_base_num=audio["time_base"]["num"], time_base_den=audio["time_base"]["den"],
            sample_rate=audio["sample_rate"], sample_count=audio["sample_count"])
        bound.validate()
        return bound
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error, LocalASRError):
        raise LocalASRError("sample_unavailable", "指定素材的已入库音轨或源绑定不可用；未重提取、未改动工程") from None


def _fraction(value: Fraction) -> dict:
    return {"num": value.numerator, "den": value.denominator}


def parse_whisper_json(payload: dict, audio: BoundAudio) -> dict:
    """Normalize strict bounds, preserve raw local offsets and exact source time."""
    if (not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list)
            or len(payload["transcription"]) > 100000
            or not isinstance(payload.get("result", {}), dict)
            or payload.get("result", {}).get("language") is not None
            and not isinstance(payload["result"]["language"], str)):
        raise LocalASRError("invalid_output", "本地模型结果缺少 transcription 数组")
    duration_ms = math.ceil(audio.duration * 1000)
    warnings = set()

    def span(value, lower=0, upper=duration_ms):
        offsets = value.get("offsets")
        if (not isinstance(offsets, dict) or type(offsets.get("from")) is not int
                or type(offsets.get("to")) is not int):
            raise ValueError
        start, end = offsets["from"], offsets["to"]
        if start < lower or end <= start or end > upper + 20:
            raise ValueError
        clipped = min(end, upper)
        if clipped <= start:
            raise ValueError
        if clipped != end:
            warnings.add("end_quantization_clamped")
        return {"start_ms": start, "end_ms": clipped,
                "raw_start_ms": start, "raw_end_ms": end,
                "source_start": _fraction(audio.origin + Fraction(start, 1000)),
                "source_end": _fraction(audio.origin + Fraction(clipped, 1000))}

    segments = []
    try:
        previous_start = -1
        for number, item in enumerate(payload["transcription"]):
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) or len(item["text"]) > 10000:
                raise ValueError
            text = item["text"].strip()
            if not text:
                continue
            interval = span(item)
            if interval["start_ms"] < previous_start:
                raise ValueError
            previous_start = interval["start_ms"]
            tokens = []
            uncertainty = {"requires_listen_review", "estimated_timestamps"}
            raw_tokens = item.get("tokens", [])
            if not isinstance(raw_tokens, list) or len(raw_tokens) > 10000:
                raise ValueError
            for token in raw_tokens:
                if not isinstance(token, dict) or not isinstance(token.get("text"), str):
                    raise ValueError
                token_text = token["text"]
                if _SPECIAL.fullmatch(token_text) or not token_text.strip():
                    continue
                probability = token.get("p")
                if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                        or not math.isfinite(probability) or not 0 <= probability <= 1):
                    raise ValueError
                candidate = {"text": token_text, "decoder_probability": probability,
                             "probability_kind": "upstream_first_token_decoder_probability"}
                try:
                    candidate.update(span(token, interval["start_ms"], interval["end_ms"]))
                    candidate["timestamp_precision"] = "estimated_token"
                except ValueError:
                    candidate["timestamp_precision"] = "unavailable"
                    uncertainty.add("token_time_unavailable_or_out_of_segment")
                if probability < 0.5:
                    uncertainty.add("low_decoder_probability")
                tokens.append(candidate)
            if not tokens:
                uncertainty.add("token_evidence_unavailable")
            kind = "non_speech_annotation_candidate" if text in _NON_SPEECH_MARKERS else "speech_candidate"
            if kind == "non_speech_annotation_candidate":
                uncertainty.add("model_non_dialogue_marker_requires_audio_review")
            segments.append({"id": f"segment-{number + 1:04d}", **interval, "text": text,
                "kind": kind, "timestamp_precision": "estimated_segment", "tokens": tokens,
                "uncertainty": sorted(uncertainty), "human_confirmed": False})
    except (TypeError, ValueError, KeyError):
        raise LocalASRError("invalid_output", "本地模型返回的句词结构或时间范围未通过校验") from None
    return {"schema_version": 1, "binding": audio.binding(), "segments": segments,
        "language": payload.get("result", {}).get("language"),
        "status": "candidate_requires_review" if segments else "no_speech_candidate",
        "warnings": sorted(warnings | {"whisper_timestamps_are_estimates_not_forced_alignment",
            "token_probability_is_not_transcript_accuracy", "no_speaker_identity_inference"}),
        "quality_passed": False, "human_confirmed": False}


def _srt_time(milliseconds: int) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


def _exact_pcm_silence(path: Path) -> bool:
    """Only mathematical all-zero PCM, not a guessed speech/volume threshold."""
    return _validate_pcm16(path)


def run_local_asr(audio: BoundAudio, *, root: Path = DEFAULT_ROOT, language: str = "zh",
                  threads: int = 4, timeout_seconds: int = 900,
                  model_size: str = "small",
                  cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Run one local child, or replay validated output for the same content/config.

    A lock prevents duplicate concurrent work. Process death leaves a recoverable
    state; a subsequent call uses a new attempt directory, preserving evidence.
    No result is written back to the product database or current Document.
    """
    if language not in {"zh", "en", "auto"} or type(threads) is not int or not 1 <= threads <= 16:
        raise LocalASRError("invalid_config", "本地识别语言或线程设置无效")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 14400:
        raise LocalASRError("invalid_config", "本地识别超时设置无效")
    specification = local_model_spec(model_size)
    audio.validate()
    root = private_root(root)
    binary = root / f"build-v{WHISPER_VERSION}/bin/whisper-cli"
    model = root / "models" / specification["filename"]
    if not binary.is_file() or not os.access(binary, os.X_OK) or not model.is_file():
        raise LocalASRError("not_prepared", f"先准备固定版本 whisper.cpp 和多语 {model_size} 权重")
    model_hash = sha256(model)
    if model_hash != specification["sha256"] or model.stat().st_size != specification["bytes"]:
        raise LocalASRError("model_mismatch", f"本地 {model_size} 权重校验失败；未开始识别")
    config = {"engine": "whisper.cpp", "version": WHISPER_VERSION, "commit": WHISPER_COMMIT,
        "model": specification["name"], "model_sha256": model_hash, "binary_sha256": sha256(binary),
        "language": language, "threads": threads, "decode_temperature": 0,
        "beam_size": 5, "token_timestamps": "whisper_estimate", "network": False,
        "exact_zero_pcm_guard": 1, "normalization_schema": 1}
    fingerprint = hashlib.sha256(json.dumps({"binding": audio.binding(), "config": config},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    jobs = root / "runs"
    jobs.mkdir(mode=0o700, exist_ok=True)
    job = jobs / fingerprint
    job.mkdir(mode=0o700, exist_ok=True)
    lock = (job / "run.lock").open("a+")
    os.fchmod(lock.fileno(), 0o600)
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LocalASRError("already_running", "相同音轨和模型配置的识别正在运行") from None
        state_path = job / "state.json"
        old_state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if old_state.get("state") == "succeeded":
            result_path = job / "result.json"
            if not result_path.is_file() or sha256(result_path) != old_state.get("result_sha256"):
                raise LocalASRError("result_changed", "已完成结果被修改，不能当作原识别结果复用")
            return {**old_state, "reused": True, "job_dir": str(job)}
        # A crash during copy may leave an attempt directory before state.json
        # was committed. Preserve it and continue with a new number on recovery.
        existing_attempts = [int(path.name[8:]) for path in job.glob("attempt-*")
                             if re.fullmatch(r"attempt-\d{4,}", path.name)]
        attempt = max([old_state.get("attempt", 0), *existing_attempts]) + 1
        attempt_dir = job / f"attempt-{attempt:04d}"
        attempt_dir.mkdir(mode=0o700)
        copied_audio = attempt_dir / "input.wav"
        shutil.copyfile(audio.path, copied_audio)
        copied_audio.chmod(0o600)
        if sha256(copied_audio) != audio.audio_sha256:
            raise LocalASRError("audio_changed", "音轨在准备识别时发生变化")
        state = {"schema_version": 1, "fingerprint": fingerprint, "state": "running", "attempt": attempt,
            "started_at": time.time(), "binding": audio.binding(), "config": config,
            "recovered_from": old_state.get("state"), "quality_passed": False}
        _atomic_json(state_path, state)
        output = attempt_dir / "whisper"
        command = [str(binary), "--model", str(model), "--file", str(copied_audio),
            "--language", language, "--threads", str(threads), "--output-json-full",
            "--output-file", str(output), "--temperature", "0", "--beam-size", "5", "--no-prints"]
        process = None
        try:
            if cancelled():
                raise LocalASRError("cancelled", "本地识别已取消，结果未写入工程")
            exact_silence = _exact_pcm_silence(copied_audio)
            if exact_silence:
                parsed = parse_whisper_json({"transcription": []}, audio)
                parsed["warnings"].append("exact_zero_pcm_confirmed_engine_skipped")
            else:
                with (attempt_dir / "engine.log").open("wb") as log:
                    os.fchmod(log.fileno(), 0o600)
                    # The engine retains the lock if its Python parent dies;
                    # recovery cannot start a duplicate while it is still alive.
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        pass_fds=(lock.fileno(),),
                        env={key: value for key, value in os.environ.items() if key in {
                            "PATH", "TMPDIR", "LANG", "LC_ALL", "HOME"}})
                    started = time.monotonic()
                    while process.poll() is None:
                        if cancelled():
                            raise LocalASRError("cancelled", "本地识别已取消，结果未写入工程")
                        if time.monotonic() - started > timeout_seconds:
                            raise LocalASRError("timeout", "本地识别超时，可按同一输入配置恢复")
                        time.sleep(0.1)
                    if process.returncode != 0:
                        raise LocalASRError("engine_failed", "本地识别进程失败；诊断保留在私有运行目录")
                raw = output.with_suffix(".json")
                if not raw.is_file() or raw.stat().st_size > _MAX_RESULT_BYTES:
                    raise LocalASRError("invalid_output", "本地模型结果缺失或超出大小限制")
                raw.chmod(0o600)
                parsed = parse_whisper_json(json.loads(raw.read_text(encoding="utf-8")), audio)
            parsed["config"] = config
            _atomic_json(job / "result.json", parsed)
            with (job / "candidate.srt").open("w", encoding="utf-8") as subtitles:
                os.fchmod(subtitles.fileno(), 0o600)
                for number, segment in enumerate(parsed["segments"], start=1):
                    subtitles.write(f"{number}\n{_srt_time(segment['start_ms'])} --> {_srt_time(segment['end_ms'])}\n{segment['text']}\n\n")
            state.update(state="succeeded", finished_at=time.time(), result_sha256=sha256(job / "result.json"),
                segment_count=len(parsed["segments"]), result_status=parsed["status"], engine_executed=not exact_silence)
            _atomic_json(state_path, state)
            return {**state, "reused": False, "job_dir": str(job)}
        except BaseException as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            state.update(state="cancelled" if isinstance(exc, KeyboardInterrupt) or
                isinstance(exc, LocalASRError) and exc.code == "cancelled" else "failed",
                error_code=exc.code if isinstance(exc, LocalASRError) else type(exc).__name__,
                finished_at=time.time())
            _atomic_json(state_path, state)
            if isinstance(exc, (LocalASRError, KeyboardInterrupt)):
                raise
            raise LocalASRError("local_failure", "本地识别未完成，记录已保留，可恢复") from None
    finally:
        lock.close()
