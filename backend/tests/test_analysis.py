"""Analysis integration tests run in private subprocesses, never the user DB."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


SETUP = '''
import hashlib, json, os, time, uuid
from pathlib import Path
from fastapi.testclient import TestClient
from app import analysis, storage as db, model_services as models
from app.main import app, demo
db.migrate()
project_id = demo()["project_id"]
with db.engine.connect() as c:
    media = next(iter(db.asset_map(c).values()))
    original_project = c.execute(db.projects.select().where(db.projects.c.id == project_id)).first().document_json
source_hash = hashlib.sha256(Path(media["path"]).read_bytes()).hexdigest()
asset_id = media["id"]
headers = {"X-Studio-Client": "1"}
client = TestClient(app, headers=headers)
def enqueue(mode="local", request_id=None):
    response = client.post(f"/api/v1/assets/{asset_id}/analysis", json={
        "request_id": request_id or uuid.uuid4().hex, "mode": mode})
    assert response.status_code == 200, response.text
    return response.json()
def state(job_id):
    response = client.get(f"/api/v1/analysis-jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()
def current():
    response = client.get(f"/api/v1/assets/{asset_id}/analysis")
    assert response.status_code == 200, response.text
    return response.json()
def complete(job):
    claimed = analysis.claim()
    assert claimed and claimed["id"] == job["id"], claimed
    analysis.process_job(claimed)
    result = state(job["id"])
    assert result["status"] == "succeeded", result
    return current()
'''


def run_case(tmp_path, code, timeout=45):
    env = os.environ.copy()
    for key in ("STUDIO_MODELS_ENABLED", "DASHSCOPE_API_KEY", "STUDIO_ASR_AUDIO_HOSTS",
                "STUDIO_ASR_AUDIO_URLS_JSON", "STUDIO_BAILIAN_WORKSPACE_ID"):
        env.pop(key, None)
    env["STUDIO_DATA_DIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", SETUP + textwrap.dedent(code)],
                            env=env, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_local_pipeline_idempotency_public_files_and_no_cloud_calls(tmp_path):
    run_case(tmp_path, '''
    class UnexpectedCloud:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("local analysis must never instantiate a cloud client")
    models.BailianModelServices = UnexpectedCloud
    assert current()["manifest"] is None
    request_id = uuid.uuid4().hex
    job = enqueue(request_id=request_id)
    assert enqueue(request_id=request_id)["id"] == job["id"]
    conflict = client.post(f"/api/v1/assets/{asset_id}/analysis", json={"request_id": request_id, "mode": "cloud"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    busy = client.post(f"/api/v1/assets/{asset_id}/analysis", json={"request_id": uuid.uuid4().hex, "mode": "local"})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "ANALYSIS_BUSY"
    result = complete(job)
    manifest = result["manifest"]
    assert result["revision"] == 1
    assert manifest["analysis_state"] == "local_ready"
    assert manifest["steps"] == {"prepare": "succeeded", "asr": "not_requested", "vision": "not_requested", "embedding": "not_requested"}
    assert manifest["transcript"]["status"] == "not_configured"
    assert manifest["shots"] and manifest["waveform"]
    shot = manifest["shots"][0]
    assert shot["description"] is None and not shot["manual"]
    for url, content_type in ((manifest["proxy"]["url"], "video/mp4"), (shot["thumbnail_url"], "image/jpeg")):
        response = client.get(url)
        assert response.status_code == 200 and response.headers["content-type"].startswith(content_type)
        assert len(response.content) > 100
    base = f"/api/v1/assets/{asset_id}/analysis/files/{manifest['artifact_id']}"
    assert client.get(base + "/audio.wav").status_code == 404
    assert client.get(base + "/manifest.json").status_code == 404
    assert client.get(base + "/%2e%2e%2foriginals%2fsource.mp4").status_code == 404
    assert client.get(base.replace(manifest["artifact_id"], "f"*32 + "-a1") + "/proxy.mp4").status_code == 404
    cloud = client.post(f"/api/v1/assets/{asset_id}/analysis", json={"request_id": uuid.uuid4().hex, "mode": "cloud"})
    assert cloud.status_code == 422 and cloud.json()["error"]["code"] == "CLOUD_NOT_READY"
    semantic = client.get("/api/v1/search", params={"query": "场景", "mode": "semantic"})
    assert semantic.status_code == 422 and semantic.json()["error"]["code"] == "SEMANTIC_NOT_READY"
    assert str(db.DATA) not in json.dumps(result)
    assert source_hash == hashlib.sha256(Path(media["path"]).read_bytes()).hexdigest()
    with db.engine.connect() as c:
        assert c.execute(db.projects.select().where(db.projects.c.id == project_id)).first().document_json == original_project
    ''')


def test_annotations_cas_keyword_and_source_range_survive_reanalysis(tmp_path):
    run_case(tmp_path, '''
    first = complete(enqueue())
    shot = first["manifest"]["shots"][0]
    edit = {"expected_revision": first["revision"], "shot_id": shot["id"],
            "description": "人工核对：红色房间内发生争执", "tags": ["重逢证据", "争执", "重逢证据"]}
    response = client.patch(f"/api/v1/assets/{asset_id}/analysis/annotations", json=edit)
    assert response.status_code == 200, response.text
    revised = response.json()
    assert revised["revision"] == first["revision"] + 1
    assert revised["manifest"]["shots"][0]["manual"] is True
    assert revised["manifest"]["shots"][0]["tags"] == ["重逢证据", "争执"]
    stale = client.patch(f"/api/v1/assets/{asset_id}/analysis/annotations", json=edit)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    found = client.get("/api/v1/search", params={"query": "重逢证据", "asset_ids": asset_id}).json()["results"]
    assert len(found) == 1 and found[0]["shot_id"] == shot["id"]
    assert found[0]["start_pts"] == shot["start_pts"] and found[0]["time_base"] == first["manifest"]["video"]["time_base"]
    assert client.get("/api/v1/search", params={"query": "重逢证据", "asset_ids": "missing"}).json()["results"] == []
    second_job = enqueue()
    # The current published result remains readable throughout a rerun.
    assert client.get(first["manifest"]["proxy"]["url"]).status_code == 200
    second = complete(second_job)
    assert second["revision"] == revised["revision"] + 1
    updated = second["manifest"]["shots"][0]
    assert updated["description"] == edit["description"] and updated["manual"] is True
    assert second["manifest"]["unmatched_annotations"] == 0
    assert client.get(first["manifest"]["proxy"]["url"]).status_code == 404
    assert client.get(second["manifest"]["proxy"]["url"]).status_code == 200
    assert client.get("/api/v1/search", params={"query": "重逢证据"}).json()["results"]
    ''')


def test_cancel_stale_attempt_recovery_retry_preserve_published_result(tmp_path):
    run_case(tmp_path, '''
    original = complete(enqueue())
    pending = enqueue()
    assert client.post(f"/api/v1/analysis-jobs/{pending['id']}/cancel").json()["status"] == "cancelled"
    assert analysis.claim() is None
    retried = client.post(f"/api/v1/analysis-jobs/{pending['id']}/retry").json()
    assert retried["id"] != pending["id"]
    claimed = analysis.claim()
    assert claimed["id"] == retried["id"]
    # Late execution of an obsolete attempt must not replace the current
    # manifest or write a success into the newer attempt's row.
    with db.engine.begin() as c:
        c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == retried["id"]).values(attempt=2))
    analysis.process_job(claimed)
    assert state(retried["id"])["attempt"] == 2 and state(retried["id"])["status"] == "running"
    assert current()["manifest"]["artifact_id"] == original["manifest"]["artifact_id"]
    analysis.recover()
    assert state(retried["id"])["status"] == "queued"
    recovered = analysis.claim()
    assert recovered["attempt"] == 3
    analysis.process_job(recovered)
    assert state(retried["id"])["status"] == "succeeded"
    valid = current()
    assert valid["manifest"]["artifact_id"] != original["manifest"]["artifact_id"]
    running = enqueue()
    claimed = analysis.claim()
    assert client.post(f"/api/v1/analysis-jobs/{running['id']}/cancel").json()["cancel_requested"] == 1
    analysis.process_job(claimed)
    assert state(running["id"])["status"] == "cancelled"
    assert current()["manifest"]["artifact_id"] == valid["manifest"]["artifact_id"]
    bounded = enqueue()
    with db.engine.begin() as c:
        c.execute(db.analysis_jobs.update().where(db.analysis_jobs.c.id == bounded["id"]).values(status="running", attempt=3))
    analysis.recover()
    assert state(bounded["id"])["status"] == "failed"
    assert current()["manifest"]["artifact_id"] == valid["manifest"]["artifact_id"]
    assert client.get(valid["manifest"]["proxy"]["url"]).status_code == 200
    ''')


CLOUD_FIXTURE = '''
os.environ["STUDIO_MODEL_PROVIDER"] = "bailian"
os.environ["STUDIO_MODELS_ENABLED"] = "1"
os.environ["DASHSCOPE_API_KEY"] = "fixture-key-never-sent"
os.environ["STUDIO_ASR_AUDIO_HOSTS"] = "fixture-audio.example"
from app.preprocess import prepare_media
binding_dir = db.DATA / "fixture-binding"
prepare_media(media, binding_dir)
audio_sha256 = hashlib.sha256((binding_dir / "audio.wav").read_bytes()).hexdigest()
os.environ["STUDIO_ASR_AUDIO_URLS_JSON"] = json.dumps({asset_id: {
    "url": "https://fixture-audio.example/private-audio.wav?token=secret",
    "audio_sha256": audio_sha256, "asset_version": media["version"]}})
observed = {"submit": 0, "poll": 0, "vision": 0, "embedding": 0}
def meta(model):
    return models.CallMetadata(provider="bailian-beijing", requested_model=model,
        returned_model=model, elapsed_ms=2, usage={"input_tokens": 10}, prompt_version=None)
class FakeServices:
    def __init__(self, config):
        self.config = config
        config.require_enabled()
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def submit_asr(self, url, **kwargs):
        observed["submit"] += 1
        assert url == "https://fixture-audio.example/private-audio.wav?token=secret"
        return models.ASRSubmission(task_id="fixture-asr-task", metadata=meta(self.config.asr_model))
    def poll_asr(self, task_id, *, cancelled):
        observed["poll"] += 1
        assert task_id == "fixture-asr-task" and not cancelled()
        return models.ASRResult(segments=[models.ASRSegment(start_ms=0, end_ms=1200, text="她终于回来寻找证据", words=[
            models.ASRWord(start_ms=0, end_ms=800, text="她终于回来"),
            models.ASRWord(start_ms=800, end_ms=1200, text="寻找证据")])], metadata=meta(self.config.asr_model))
    def describe_shot(self, jpeg, **kwargs):
        observed["vision"] += 1
        assert jpeg.startswith(b"\\xff\\xd8\\xff")
        return models.VisionResult(description=models.ShotDescription(scene="红色场景，人物身份未知",
            people_candidates=[models.PersonCandidate(description="身份未知的人物候选", confidence=0.3)],
            actions=["画面静止"], tags=["重逢候选", "红色"], unknowns=["无法确认人物关系"]),
            metadata=meta(self.config.vision_model))
    def embed_texts(self, texts, **kwargs):
        observed["embedding"] += 1
        assert texts and all(isinstance(text, str) and text for text in texts)
        return models.EmbeddingResult(vectors=[[1.0] + [0.0] * 1023 for text in texts], metadata=meta(self.config.embedding_model))
models.BailianModelServices = FakeServices
'''


def test_cloud_pipeline_uses_real_pydantic_contracts_and_semantic_mock(tmp_path):
    run_case(tmp_path, CLOUD_FIXTURE + textwrap.dedent('''
    result = complete(enqueue(mode="cloud"))
    manifest = result["manifest"]
    assert manifest["analysis_state"] == "cloud_ready"
    assert manifest["steps"] == {"prepare": "succeeded", "asr": "succeeded", "vision": "succeeded", "embedding": "succeeded"}
    assert observed == {"submit": 1, "poll": 1, "vision": 1, "embedding": 1}, observed
    shot = manifest["shots"][0]
    assert shot["description_status"] == "candidate"
    assert shot["visual_evidence"]["unknowns"] == ["无法确认人物关系"]
    assert "她终于回来" in shot["transcript"]
    assert "embedding" not in shot and "embedding_model" in manifest
    assert manifest["transcript"]["segments"][0]["words"][0]["start_ms"] == 0
    serialized = json.dumps(result)
    assert "fixture-key-never-sent" not in serialized and "token=secret" not in serialized
    assert "fixture-asr-task" not in serialized
    denied = TestClient(app).get("/api/v1/search", params={"query": "人物返乡", "mode": "semantic"})
    assert denied.status_code == 403 and observed["embedding"] == 1
    found = client.get("/api/v1/search", params={"query": "人物返乡", "mode": "semantic"})
    assert found.status_code == 200, found.text
    found = found.json()
    assert len(found["results"]) == 1 and found["results"][0]["score"] == 1.0
    assert found["warnings"] and "_vector" not in found["results"][0]
    assert observed["embedding"] == 2
    edit = client.patch(f"/api/v1/assets/{asset_id}/analysis/annotations", json={
        "expected_revision": result["revision"], "shot_id": shot["id"], "description": "人工确认的场景", "tags": ["人工证据"]})
    assert edit.status_code == 200
    semantic = client.get("/api/v1/search", params={"query": "人物返乡", "mode": "semantic"})
    assert semantic.status_code == 422 and semantic.json()["error"]["code"] == "SEMANTIC_NOT_READY"
    assert observed["embedding"] == 2  # no paid query using stale manual-edit vectors
    assert client.get("/api/v1/search", params={"query": "人工证据"}).json()["results"]
    '''))


def test_cloud_failure_retry_resumes_asr_without_resubmission(tmp_path):
    run_case(tmp_path, CLOUD_FIXTURE + textwrap.dedent('''
    original = complete(enqueue())
    original_describe = FakeServices.describe_shot
    def fail_vision(self, jpeg, **kwargs):
        raise models.ModelServiceError("private provider body token=secret /internal/path fixture-key-never-sent")
    FakeServices.describe_shot = fail_vision
    job = enqueue(mode="cloud")
    analysis.process_job(analysis.claim())
    failed = state(job["id"])
    assert failed["status"] == "failed" and failed["steps"]["asr"] == "succeeded", failed
    assert "token=secret" not in failed["error"] and "/internal/path" not in failed["error"]
    assert current()["manifest"]["artifact_id"] == original["manifest"]["artifact_id"]
    assert client.get(original["manifest"]["proxy"]["url"]).status_code == 200
    assert observed["submit"] == 1 and observed["poll"] == 1
    FakeServices.describe_shot = original_describe
    retry = client.post(f"/api/v1/analysis-jobs/{job['id']}/retry")
    assert retry.status_code == 200, retry.text
    result = complete(retry.json())
    assert result["manifest"]["analysis_state"] == "cloud_ready"
    assert observed["submit"] == 1 and observed["poll"] == 1
    assert observed["vision"] == 1 and observed["embedding"] == 1
    assert state(job["id"])["status"] == "failed"
    '''))


def test_unknown_asr_submission_retry_never_submits_again(tmp_path):
    run_case(tmp_path, CLOUD_FIXTURE + textwrap.dedent('''
    original = complete(enqueue())
    def uncertain_submission(self, url, **kwargs):
        observed["submit"] += 1
        raise models.ASRSubmissionUnknown("accepted response was lost")
    FakeServices.submit_asr = uncertain_submission
    job = enqueue(mode="cloud")
    analysis.process_job(analysis.claim())
    assert state(job["id"])["status"] == "failed"
    assert observed["submit"] == 1 and observed["poll"] == 0
    retried = client.post(f"/api/v1/analysis-jobs/{job['id']}/retry")
    assert retried.status_code == 409, retried.text
    assert retried.json()["error"]["code"] == "ASR_SUBMISSION_UNKNOWN"
    assert analysis.claim() is None
    assert observed["submit"] == 1 and observed["poll"] == 0
    assert current()["manifest"]["artifact_id"] == original["manifest"]["artifact_id"]
    '''))


def test_invalid_cloud_timeout_does_not_break_local_analysis(tmp_path):
    run_case(tmp_path, '''
    os.environ["STUDIO_MODEL_TIMEOUT_SECONDS"] = "invalid"
    class UnexpectedCloud:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("invalid cloud config must never instantiate a cloud client")
    models.BailianModelServices = UnexpectedCloud
    initial = current()
    assert initial["manifest"] is None
    assert initial["cloud"]["error"] and initial["cloud"]["configured"] is False
    result = complete(enqueue())
    assert result["manifest"]["analysis_state"] == "local_ready"
    assert result["manifest"]["proxy"] and result["manifest"]["shots"]
    assert result["cloud"]["error"]
    assert client.get(result["manifest"]["proxy"]["url"]).status_code == 200
    denied = client.post(f"/api/v1/assets/{asset_id}/analysis", json={"request_id": uuid.uuid4().hex, "mode": "cloud"})
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "CLOUD_NOT_READY"
    assert source_hash == hashlib.sha256(Path(media["path"]).read_bytes()).hexdigest()
    ''')


def test_cancel_after_paid_asr_result_checkpoints_without_publishing(tmp_path):
    run_case(tmp_path, CLOUD_FIXTURE + textwrap.dedent('''
    original = complete(enqueue())
    original_poll = FakeServices.poll_asr
    job = enqueue(mode="cloud")
    def cancel_after_result(self, task_id, *, cancelled):
        result = original_poll(self, task_id, cancelled=cancelled)
        response = client.post(f"/api/v1/analysis-jobs/{job['id']}/cancel")
        assert response.status_code == 200
        return result
    FakeServices.poll_asr = cancel_after_result
    analysis.process_job(analysis.claim())
    cancelled = state(job["id"])
    assert cancelled["status"] == "cancelled" and cancelled["steps"]["asr"] == "succeeded", cancelled
    assert current()["manifest"]["artifact_id"] == original["manifest"]["artifact_id"]
    assert observed["submit"] == observed["poll"] == 1 and observed["vision"] == 0
    FakeServices.poll_asr = original_poll
    retried = client.post(f"/api/v1/analysis-jobs/{job['id']}/retry")
    assert retried.status_code == 200, retried.text
    result = complete(retried.json())
    assert result["manifest"]["analysis_state"] == "cloud_ready"
    assert observed["submit"] == observed["poll"] == 1
    assert observed["vision"] == observed["embedding"] == 1
    '''))


def test_cross_shot_word_attribution_uses_source_ranges(tmp_path):
    video_setup = '''
from app.media import ffmpeg_binary, probe_media
source = db.DATA / "originals" / "cross-shot-words.mp4"
subprocess.run([ffmpeg_binary(), "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=red:s=160x90:r=25:d=1",
    "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=25:d=1",
    "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=2",
    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source)],
    check=True, capture_output=True, timeout=30)
with db.engine.begin() as c:
    c.execute(db.assets.update().where(db.assets.c.id == asset_id).values(path=str(source),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(), metadata_json=db.dumps(probe_media(source))))
    media = db.asset_map(c)[asset_id]
'''
    run_case(tmp_path, "import subprocess\n" + video_setup + CLOUD_FIXTURE + textwrap.dedent('''
    def split_words(self, task_id, *, cancelled):
        observed["poll"] += 1
        return models.ASRResult(segments=[models.ASRSegment(start_ms=0, end_ms=1800,
            text="左镜头独有的词。右镜头独有的词。", words=[
                models.ASRWord(start_ms=0, end_ms=800, text="左镜头独有的词", punctuation="。"),
                models.ASRWord(start_ms=1200, end_ms=1800, text="右镜头独有的词", punctuation="。")])],
            metadata=meta(self.config.asr_model))
    FakeServices.poll_asr = split_words
    result = complete(enqueue(mode="cloud"))
    left, right = result["manifest"]["shots"]
    assert left["end_pts"] == right["start_pts"]
    assert left["transcript"] == "左镜头独有的词。", left
    assert right["transcript"] == "右镜头独有的词。", right
    assert left["transcript_precision"] == right["transcript_precision"] == "word"
    first_only = client.get("/api/v1/search", params={"query": "左镜头独有"}).json()["results"]
    second_only = client.get("/api/v1/search", params={"query": "右镜头独有"}).json()["results"]
    assert len(first_only) == len(second_only) == 1
    assert first_only[0]["shot_id"] == left["id"] and second_only[0]["shot_id"] == right["id"]
    '''))
