"""Provider contract tests only. Every HTTP request stays in MockTransport."""
import json
from dataclasses import replace

import httpx
import pytest

from app.model_services import (
    ASRSubmissionUnknown, BailianModelServices, ModelCallCancelled, ModelServiceConfig,
    ModelServiceError, ModelsUnavailable,
)


CONFIG = ModelServiceConfig(enabled=True, api_key="sk-test-never-real",
                            audio_hosts=("approved-audio.oss-cn-beijing.aliyuncs.com",))
JPEG = b"\xff\xd8\xffmock-image"
RESULT_URL = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.json?Signature=secret"
DESCRIPTION = {"scene": "室内桌边", "people_candidates": [
    {"description": "画面左侧白衣人物", "confidence": .8}],
    "actions": ["站立"], "tags": ["室内"], "unknowns": ["人物姓名"]}


def service(handler, config=CONFIG):
    return BailianModelServices(config, transport=httpx.MockTransport(handler))


def response(data, status=200):
    return httpx.Response(status, json=data)


def test_disabled_and_missing_key_make_no_network_request(monkeypatch):
    for config in (ModelServiceConfig(), ModelServiceConfig(enabled=True)):
        with service(lambda _: pytest.fail("disabled model called network"), config) as models:
            with pytest.raises(ModelsUnavailable):
                models.describe_shot(JPEG)
    monkeypatch.delenv("STUDIO_MODELS_ENABLED", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    config = ModelServiceConfig.from_env()
    assert not config.enabled
    assert "test-secret" not in repr(config)
    assert "test-secret" not in json.dumps(config.public_status())


def test_timeout_environment_defaults_to_60(monkeypatch):
    monkeypatch.delenv("STUDIO_MODEL_TIMEOUT_SECONDS", raising=False)
    assert ModelServiceConfig.from_env().timeout_seconds == 60


def test_chatgpt_default_prevents_legacy_bailian_even_with_old_key_and_switch(monkeypatch):
    monkeypatch.delenv("STUDIO_MODEL_PROVIDER", raising=False)
    monkeypatch.setenv("STUDIO_MODELS_ENABLED", "1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "old-secret-must-not-be-used")
    assert not ModelServiceConfig.from_env().enabled
    monkeypatch.setenv("STUDIO_MODEL_PROVIDER", "chatgpt")
    assert not ModelServiceConfig.from_env().enabled


@pytest.mark.parametrize("value", ["1", "120", "2.5"])
def test_timeout_environment_accepts_bounded_values(monkeypatch, value):
    monkeypatch.setenv("STUDIO_MODEL_TIMEOUT_SECONDS", value)
    assert ModelServiceConfig.from_env().timeout_seconds == float(value)


@pytest.mark.parametrize("value", ["", "not-a-timeout-secret", "0", "0.9", "120.1", "-1", "nan", "inf", "-inf"])
def test_timeout_environment_rejects_invalid_values_without_echoing_input(monkeypatch, value):
    monkeypatch.setenv("STUDIO_MODEL_TIMEOUT_SECONDS", value)
    with pytest.raises(ModelsUnavailable, match="1–120 秒") as failure:
        ModelServiceConfig.from_env()
    assert "not-a-timeout-secret" not in str(failure.value)


@pytest.mark.parametrize("choice", [None, "private-upstream-text", [], {}])
def test_vision_malformed_nested_choice_is_a_safe_model_error(choice):
    with service(lambda _: response({"choices": [choice]})) as models:
        with pytest.raises(ModelServiceError, match="结构校验") as failure:
            models.describe_shot(JPEG)
    assert "private-upstream-text" not in str(failure.value)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("operation", ["vision", "embedding"])
def test_paid_completion_posts_never_retry_even_on_rate_limit_or_server_failure(status, operation):
    calls = []
    def handler(request):
        calls.append(request)
        return response({"message": "private upstream response"}, status=status)
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match=f"HTTP {status}") as failure:
            models.describe_shot(JPEG) if operation == "vision" else models.embed_texts(["测试"])
    assert len(calls) == 1
    assert "private upstream response" not in str(failure.value)


def test_vision_uses_jpeg_json_mode_and_preserves_only_safe_metadata(caplog):
    def handler(request):
        assert request.url.host == "workspace-123.cn-beijing.maas.aliyuncs.com"
        assert request.url.path == "/compatible-mode/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer sk-test-never-real"
        body = json.loads(request.content)
        assert body["enable_thinking"] is False
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return response({"model": "qwen3-vl-plus-2025-12-19", "choices": [{
            "message": {"content": json.dumps(DESCRIPTION)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 8, "unexpected": "private text"}})
    with caplog.at_level("INFO"), service(handler, replace(CONFIG, workspace_id="workspace-123")) as models:
        result = models.describe_shot(JPEG)
    assert result.description.scene == "室内桌边"
    assert result.metadata.prompt_version == "shot_description_v1"
    assert result.metadata.returned_model == "qwen3-vl-plus-2025-12-19"
    assert result.metadata.model_dump()["usage"] == {"prompt_tokens": 40, "completion_tokens": 8}
    assert all(value not in caplog.text for value in ["sk-test-never-real", "private text", "室内桌边"])


@pytest.mark.parametrize("content,finish", [
    ("```json\n{}\n```", "stop"),
    (json.dumps(DESCRIPTION | {"invented_identity": "张三"}), "stop"),
    (json.dumps(DESCRIPTION | {"people_candidates": [{"description": "人", "confidence": 1.8}]}), "stop"),
    (json.dumps(DESCRIPTION), "length"),
])
def test_vision_rejects_invalid_or_truncated_evidence(content, finish):
    with service(lambda _: response({"choices": [{"message": {"content": content},
                                                  "finish_reason": finish}]})) as models:
        with pytest.raises(ModelServiceError, match="结构校验"):
            models.describe_shot(JPEG)


def test_embedding_checks_order_dimension_and_numeric_values():
    def handler(request):
        body = json.loads(request.content)
        assert body["dimensions"] == 1024 and body["encoding_format"] == "float"
        assert body["input"] == ["对白", "人物站立"]
        return response({"model": "text-embedding-v4", "data": [
            {"index": 1, "embedding": [.25] * 1024}, {"index": 0, "embedding": [.5] * 1024}],
            "usage": {"total_tokens": 6}})
    with service(handler) as models:
        result = models.embed_texts(["对白", "人物站立"])
    assert result.vectors[0][0] == .5 and len(result.vectors[1]) == 1024


@pytest.mark.parametrize("rows", [
    [{"index": 0, "embedding": [.1] * 1023}],
    [{"index": 1, "embedding": [.1] * 1024}],
    [{"index": 0, "embedding": [True] * 1024}],
    [{"index": 0, "embedding": [".1"] * 1024}],
    [],
])
def test_embedding_rejects_unusable_vectors(rows):
    with service(lambda _: response({"data": rows})) as models:
        with pytest.raises(ModelServiceError, match="1024"):
            models.embed_texts(["对白"])


def test_asr_submit_has_single_url_and_no_automatic_retry():
    calls = []
    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["input"]["file_urls"] == ["https://approved-audio.oss-cn-beijing.aliyuncs.com/audio.wav"]
        assert body["parameters"]["channel_id"] == [0]
        assert request.headers["X-DashScope-Async"] == "enable"
        raise httpx.ReadTimeout("secret upstream body must not escape")
    with service(handler) as models:
        with pytest.raises(ASRSubmissionUnknown) as failure:
            models.submit_asr("https://approved-audio.oss-cn-beijing.aliyuncs.com/audio.wav")
    assert len(calls) == 1
    assert "secret upstream body" not in str(failure.value)


def test_asr_submit_returns_recoverable_task_id():
    with service(lambda _: response({"output": {"task_id": "remote-task-1", "task_status": "PENDING"}})) as models:
        result = models.submit_asr("https://approved-audio.oss-cn-beijing.aliyuncs.com/audio.wav")
    assert result.task_id == "remote-task-1"
    assert result.metadata.requested_model == "paraformer-v2"


def test_asr_requires_explicit_audio_host_and_rejects_user_url():
    with service(lambda _: pytest.fail("must not send unapproved audio"), replace(CONFIG, audio_hosts=())) as models:
        with pytest.raises(ModelsUnavailable, match="不会自动上传"):
            models.submit_asr("https://example.com/user.mp3")
    with service(lambda _: pytest.fail("must not send unapproved audio")) as models:
        for url in ("http://approved-audio.oss-cn-beijing.aliyuncs.com/audio.wav",
                    "https://127.0.0.1/audio.wav", "https://unapproved.example/audio.wav",
                    "https://approved-audio.oss-cn-beijing.aliyuncs.com:8443/audio.wav",
                    "https://secret@approved-audio.oss-cn-beijing.aliyuncs.com/audio.wav"):
            with pytest.raises(ModelServiceError):
                models.submit_asr(url)


def complete_output(url=RESULT_URL, subtask="SUCCEEDED"):
    return {"output": {"task_id": "remote-task-1", "task_status": "SUCCEEDED", "results": [
        {"subtask_status": subtask, "transcription_url": url}]}, "usage": {"duration": 1.2}}


def test_asr_resume_poll_keeps_word_timing_and_never_sends_key_to_result_host():
    methods = []
    def handler(request):
        methods.append((request.method, request.url.path))
        if request.url.host == "dashscope.aliyuncs.com":
            assert request.url.path == "/api/v1/tasks/remote-task-1"
            return response(complete_output())
        assert "Authorization" not in request.headers
        return response({"transcripts": [{"channel_id": 0, "sentences": [{
            "begin_time": 100, "end_time": 700, "text": "你好。", "speaker_id": 1,
            "words": [{"begin_time": 100, "end_time": 600, "text": "你好", "punctuation": "。"}]}]}]})
    with service(handler) as models:
        result = models.poll_asr("remote-task-1")
    assert methods == [("POST", "/api/v1/tasks/remote-task-1"), ("GET", "/result.json")]
    assert result.segments[0].words[0].start_ms == 100
    assert result.segments[0].speaker_id == 1
    assert result.metadata.usage == {"duration": 1.2}


def test_asr_result_signed_url_is_not_logged_at_worker_info_level(caplog):
    downloaded = []
    def handler(request):
        if request.url.host == "dashscope.aliyuncs.com":
            return response(complete_output())
        downloaded.append(str(request.url))
        return response({"transcripts": []})
    with caplog.at_level("INFO"), service(handler) as models:
        result = models.poll_asr("remote-task-1")
    assert downloaded == [RESULT_URL]  # Real adapter fetch path, not a logger-only test.
    assert result.metadata.usage == {"duration": 1.2}
    assert "model_done" in caplog.text and "duration" in caplog.text
    assert "Signature=" not in caplog.text and RESULT_URL not in caplog.text
    assert "sk-test-never-real" not in caplog.text


@pytest.mark.parametrize("url", ["https://attacker.test/result.json", "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x",
    "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.attacker.test/x",
    "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:123/x",
    "https://u:p@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/x"])
def test_asr_never_downloads_untrusted_result_url(url):
    calls = []
    def handler(request):
        calls.append(request)
        return response(complete_output(url))
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match="白名单"):
            models.poll_asr("remote-task-1")
    assert len(calls) == 1


def test_asr_result_redirect_is_not_followed():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "dashscope.aliyuncs.com":
            return response(complete_output())
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match="302"):
            models.poll_asr("remote-task-1")
    assert len(calls) == 2


def test_asr_checks_subtask_failure_before_downloading():
    calls = []
    def handler(request):
        calls.append(request)
        return response(complete_output(subtask="FAILED"))
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match="子任务"):
            models.poll_asr("remote-task-1")
    assert len(calls) == 1


def test_asr_query_transient_failure_retries_without_resubmitting(monkeypatch):
    monkeypatch.setattr("app.model_services._pause", lambda *_: None)
    calls = []
    def handler(request):
        calls.append(request)
        return response({}, status=503)
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match="503"):
            models.poll_asr("remote-task-1")
    assert len(calls) == 3
    assert all(str(request.url).endswith("/api/v1/tasks/remote-task-1") for request in calls)


def test_poll_cancelled_never_calls_network():
    with service(lambda _: pytest.fail("cancelled poll called network")) as models:
        with pytest.raises(ModelCallCancelled):
            models.poll_asr("remote-task-1", cancelled=lambda: True)


def test_poll_can_cancel_while_pending():
    calls = []
    def handler(request):
        calls.append(request)
        return response({"output": {"task_status": "PENDING"}})
    # The stream is checked once after request, and once when polling resumes.
    checks = []
    def cancelled():
        checks.append(1)
        return len(checks) >= 4
    with service(handler) as models:
        with pytest.raises(ModelCallCancelled):
            models.poll_asr("remote-task-1", cancelled=cancelled)
    assert len(calls) == 1


def test_invalid_asr_sentence_timing_is_not_saved():
    def handler(request):
        if request.url.host == "dashscope.aliyuncs.com":
            return response(complete_output())
        return response({"transcripts": [{"channel_id": 0, "sentences": [
            {"begin_time": 100, "end_time": 50, "text": "invalid"}]}]})
    with service(handler) as models:
        with pytest.raises(ModelServiceError, match="时间范围"):
            models.poll_asr("remote-task-1")
