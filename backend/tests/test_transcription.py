"""Real local preprocessing and SQLite lifecycle; synthetic ASR, private test DBs."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


SETUP = '''
import hashlib,json,uuid
from pathlib import Path
from fastapi.testclient import TestClient
from app import analysis,preprocess,storage as db,transcription as tx
from app.main import app,demo
from app.local_asr import LocalASRError,parse_whisper_json
db.migrate()
project_id=demo()["project_id"]
with db.engine.connect() as c:
    media=next(iter(db.asset_map(c).values()))
    original_document=c.execute(db.projects.select()).first().document_json
asset_id=media["id"]
headers={"X-Studio-Client":"1"}
client=TestClient(app,headers=headers)
tx.ASR_ROOT=db.DATA/"private-test-model-cache"
tx.local_models=lambda:[{"id":"small","ready":True},{"id":"medium","ready":True}]
def fake_recognize(bound,configuration,cancelled):
    assert not cancelled()
    result=parse_whisper_json({"result":{"language":"zh"},"transcription":[
        {"offsets":{"from":100,"to":800},"text":"第一句候选","tokens":[
            {"text":"第一句","p":0.8,"offsets":{"from":100,"to":800}}]},
        {"offsets":{"from":1000,"to":1800},"text":"第二句候选","tokens":[]}]},bound)
    result["config"]={"model":configuration["model"]+"-multilingual", "model_sha256":configuration["model_sha256"]}
    return result
tx._recognize=fake_recognize
def queue(model="medium",request_id=None):
    response=client.post(f"/api/v1/assets/{asset_id}/transcription",json={
        "request_id":request_id or uuid.uuid4().hex,"model":model,"language":"zh"})
    assert response.status_code==200,response.text
    return response.json()
def state(job_id):
    response=client.get(f"/api/v1/analysis-jobs/{job_id}")
    assert response.status_code==200,response.text
    return response.json()
def current(transcript_id=None):
    response=client.get(f"/api/v1/assets/{asset_id}/transcription",params={"transcript_id":transcript_id} if transcript_id else {})
    assert response.status_code==200,response.text
    return response.json()
def complete(job):
    claimed=analysis.claim()
    assert claimed["id"]==job["id"]
    analysis.process_job(claimed)
    assert state(job["id"])["status"]=="succeeded",state(job["id"])
    return current()["current"]
def patch(transcript,segment_id="segment-0001",**changes):
    return client.patch(f"/api/v1/transcripts/{transcript['id']}/segments/{segment_id}",json={
        "expected_revision":transcript["revision"],**changes})
'''


def run_case(tmp_path, code, timeout=60):
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
    env["STUDIO_DATA_DIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", SETUP + textwrap.dedent(code)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generic_asset_durable_candidate_idempotency_and_no_project_write(tmp_path):
    run_case(tmp_path, '''
    import socket
    socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError("ASR used network"))
    assert current()["current"] is None
    assert current()["latest_job"] is None
    request_id=uuid.uuid4().hex
    job=queue(request_id=request_id)
    assert job["mode"]=="local_asr"
    assert queue(request_id=request_id)["id"]==job["id"]
    conflict=client.post(f"/api/v1/assets/{asset_id}/transcription",json={"request_id":request_id,"model":"small"})
    assert conflict.status_code==409
    busy=client.post(f"/api/v1/assets/{asset_id}/transcription",json={"request_id":uuid.uuid4().hex})
    assert busy.status_code==409 and busy.json()["error"]["code"]=="ANALYSIS_BUSY"
    result=complete(job)
    assert result["model"]=="medium" and result["generation"]==1 and result["revision"]==1
    assert result["quality_passed"] is False and result["human_confirmed"] is False
    assert all(s["status"]=="pending" for s in result["segments"])
    assert result["segments"][0]["source_start"]=={"num":1,"den":10}
    assert isinstance(result["binding"]["start_pts"],str)
    assert result["binding"]["asset_id"]==asset_id
    assert str(db.DATA) not in json.dumps(current())
    # Reading through a new client/process lifetime uses the persisted result.
    client.close()
    client=TestClient(app,headers=headers)
    assert current()["current"]["id"]==result["id"]
    assert current()["latest_job"]["id"]==job["id"]
    with db.engine.connect() as c:
        assert c.execute(db.projects.select()).first().document_json==original_document
        prepared=c.execute(db.analyses.select()).first()
        assert prepared.version==media["version"] and prepared.revision==1
        assert json.loads(prepared.manifest_json)["analysis_state"]=="local_ready"
        row=c.execute(db.transcripts.select()).first()
        assert json.loads(row.raw_json)["binding"]["start_pts"]==0
    ''')


def test_manual_cas_bounds_reset_and_generations_preserve_raw_corrections(tmp_path):
    run_case(tmp_path, '''
    first=complete(queue())
    with db.engine.connect() as c: raw_before=tx.get_transcript(c,first["id"]).raw_json
    confirmed=patch(first,text="人工确认第一句",start_ms=120,end_ms=780,status="confirmed")
    assert confirmed.status_code==200,confirmed.text
    edited=confirmed.json()
    assert edited["confirmed_count"]==1 and edited["quality_passed"] is False
    segment=edited["segments"][0]
    assert segment["text"]=="人工确认第一句" and segment["raw_text"]=="第一句候选"
    assert segment["human_confirmed"] and segment["token_alignment_stale"]
    assert segment["source_start"]=={"num":3,"den":25}
    assert patch(first,status="rejected").status_code==409
    assert patch(edited,end_ms=999999,status="confirmed").status_code==422
    reset=patch(edited,text="再次修改，需重新确认").json()
    assert reset["segments"][0]["status"]=="pending" and reset["confirmed_count"]==0
    confirmed=patch(reset,status="confirmed").json()
    rejected=patch(confirmed,segment_id="segment-0002",status="rejected").json()
    assert rejected["segments"][1]["status"]=="rejected"
    second=complete(queue(model="small"))
    assert second["generation"]==2 and second["model"]=="small"
    assert all(s["status"]=="pending" for s in second["segments"])
    old=current(first["id"])["current"]
    assert old["segments"][0]["text"]=="再次修改，需重新确认" and old["confirmed_count"]==1
    assert len(current()["history"])==2
    with db.engine.connect() as c: assert tx.get_transcript(c,first["id"]).raw_json==raw_before
    other=client.get("/api/v1/assets/not-this-asset/transcription",params={"transcript_id":first["id"]})
    assert other.status_code==404
    ''')


def test_cancel_retry_checkpoint_reuse_and_no_late_publication(tmp_path):
    run_case(tmp_path, '''
    queued=queue()
    assert client.post(f"/api/v1/analysis-jobs/{queued['id']}/cancel").json()["status"]=="cancelled"
    retried=client.post(f"/api/v1/analysis-jobs/{queued['id']}/retry").json()
    def cancelling(bound,configuration,cancelled):
        client.post(f"/api/v1/analysis-jobs/{retried['id']}/cancel")
        assert cancelled()
        # Explicit user cancellation wins even if the engine reports failure
        # at the same moment rather than its ordinary cancelled code.
        raise LocalASRError("engine_failed","本地识别进程失败")
    tx._recognize=cancelling
    analysis.process_job(analysis.claim())
    assert state(retried["id"])["status"]=="cancelled"
    assert current()["current"] is None
    resumed=client.post(f"/api/v1/analysis-jobs/{retried['id']}/retry").json()
    # Preparation completed before cancel; it must be reused after restart/retry.
    preprocess.prepare_media=lambda *a,**k: (_ for _ in ()).throw(AssertionError("prepared twice"))
    tx._recognize=fake_recognize
    result=complete(resumed)
    assert result["generation"]==1
    assert state(queued["id"])["status"]=="cancelled"
    assert client.post(f"/api/v1/analysis-jobs/{resumed['id']}/retry").status_code==409
    ''')


def test_worker_recovery_requeues_same_binding_and_supervisor_stop(tmp_path):
    run_case(tmp_path, '''
    job=queue()
    claimed=analysis.claim()
    assert claimed["attempt"]==1
    analysis.recover()
    assert state(job["id"])["status"]=="queued"
    stopped=[False]
    def interrupted(bound,configuration,cancelled):
        result=fake_recognize(bound,configuration,cancelled)
        stopped[0]=True
        return result
    tx._recognize=interrupted
    claimed=analysis.claim()
    assert claimed["attempt"]==2
    analysis.process_job(claimed,stopped=lambda:stopped[0])
    assert state(job["id"])["status"]=="running"
    assert current()["current"] is None
    analysis.recover()
    tx._recognize=fake_recognize
    assert complete(job)["generation"]==1
    assert state(job["id"])["attempt"]==3
    ''')


def test_source_version_change_cannot_publish_or_edit_old_transcript(tmp_path):
    run_case(tmp_path, '''
    old=complete(queue())
    pending=queue()
    with db.engine.begin() as c:
        c.execute(db.assets.update().where(db.assets.c.id==asset_id).values(version=2))
    analysis.process_job(analysis.claim())
    assert state(pending["id"])["status"]=="failed"
    assert current()["current"] is None and len(current()["history"])==1
    assert patch(old,status="confirmed").status_code==409
    assert current(old["id"])["current"]["id"]==old["id"]
    assert client.post(f"/api/v1/analysis-jobs/{pending['id']}/retry").status_code==409
    with db.engine.connect() as c: assert len(list(c.execute(db.transcripts.select())))==1
    ''')


def test_failed_rerun_preserves_old_candidate_and_human_confirmation(tmp_path):
    run_case(tmp_path, '''
    old=complete(queue())
    old=patch(old,status="confirmed").json()
    def failing(*args):raise LocalASRError("engine_failed","本地识别进程失败")
    tx._recognize=failing
    failed=queue()
    analysis.process_job(analysis.claim())
    assert state(failed["id"])["status"]=="failed"
    assert current()["current"]["id"]==old["id"] and current()["current"]["confirmed_count"]==1
    assert str(db.DATA) not in state(failed["id"])["error"]
    tx._recognize=fake_recognize
    retry=client.post(f"/api/v1/analysis-jobs/{failed['id']}/retry").json()
    assert complete(retry)["generation"]==2
    assert current(old["id"])["current"]["confirmed_count"]==1
    ''')


def test_no_audio_and_missing_models_are_explicit_without_job_or_network(tmp_path):
    run_case(tmp_path, '''
    metadata=dict(media["metadata"])
    metadata["audio_stream_index"]=None
    with db.engine.begin() as c:
        c.execute(db.assets.update().where(db.assets.c.id==asset_id).values(metadata_json=db.dumps(metadata)))
    response=client.post(f"/api/v1/assets/{asset_id}/transcription",json={"request_id":uuid.uuid4().hex})
    assert response.status_code==422 and response.json()["error"]["code"]=="NO_AUDIO"
    with db.engine.begin() as c:
        c.execute(db.assets.update().where(db.assets.c.id==asset_id).values(metadata_json=db.dumps(media["metadata"])))
    tx.local_models=lambda:[{"id":"small","ready":False},{"id":"medium","ready":False}]
    response=client.post(f"/api/v1/assets/{asset_id}/transcription",json={"request_id":uuid.uuid4().hex})
    assert response.status_code==422 and response.json()["error"]["code"]=="LOCAL_MODEL_NOT_READY"
    with db.engine.connect() as c: assert list(c.execute(db.analysis_jobs.select()))==[]
    ''')


def test_existing_analysis_reused_and_latest_job_is_transcription_only(tmp_path):
    run_case(tmp_path, '''
    local=client.post(f"/api/v1/assets/{asset_id}/analysis",json={"request_id":uuid.uuid4().hex,"mode":"local"}).json()
    analysis.process_job(analysis.claim())
    assert state(local["id"])["status"]=="succeeded"
    assert current()["latest_job"] is None
    with db.engine.connect() as c: manifest_before=c.execute(db.analyses.select()).first().manifest_json
    preprocess.prepare_media=lambda *a,**k: (_ for _ in ()).throw(AssertionError("existing derivatives not reused"))
    transcript=complete(queue())
    with db.engine.connect() as c:
        assert c.execute(db.analyses.select()).first().manifest_json==manifest_before
    assert transcript["segments"][0]["raw_text"]=="第一句候选"
    assert current()["latest_job"]["mode"]=="local_asr"
    assert client.get(f"/api/v1/assets/{asset_id}/analysis").json()["latest_job"]["id"]==local["id"]
    ''')


def test_first_asr_publishes_missing_preparation_and_next_generation_reuses_it(tmp_path):
    run_case(tmp_path, '''
    first=complete(queue())
    prepared=client.get(f"/api/v1/assets/{asset_id}/analysis").json()
    assert prepared["manifest"]["analysis_state"]=="local_ready"
    assert prepared["manifest"]["shots"] and prepared["manifest"]["waveform"]
    thumbnail=prepared["manifest"]["shots"][0]["thumbnail_url"]
    assert client.get(thumbnail).status_code==200
    preprocess.prepare_media=lambda *a,**k: (_ for _ in ()).throw(AssertionError("first ASR preparation not reused"))
    second=complete(queue())
    assert second["generation"]==2
    updated=client.get(f"/api/v1/assets/{asset_id}/analysis").json()
    assert updated["revision"]==prepared["revision"]
    assert updated["manifest"]["artifact_id"]==prepared["manifest"]["artifact_id"]
    ''')


def test_new_asset_version_keeps_older_analysis_and_reuses_own_checkpoint(tmp_path):
    run_case(tmp_path, '''
    first=complete(queue())
    with db.engine.begin() as c:
        before=c.execute(db.analyses.select()).first().manifest_json
        c.execute(db.assets.update().where(db.assets.c.id==asset_id).values(version=2))
    new=complete(queue())
    assert new["version"]==2 and new["generation"]==1
    assert "existing_analysis_other_version_retained" in new["warnings"]
    with db.engine.connect() as c:
        assert c.execute(db.analyses.select()).first().manifest_json==before
    preprocess.prepare_media=lambda *a,**k: (_ for _ in ()).throw(AssertionError("version2 checkpoint not reused"))
    next_generation=complete(queue())
    assert next_generation["version"]==2 and next_generation["generation"]==2
    assert current(first["id"])["current"]["version"]==1
    ''')


def test_version_change_during_recognition_rejects_late_publication(tmp_path):
    run_case(tmp_path, '''
    old=complete(queue())
    def change_version(bound,configuration,cancelled):
        result=fake_recognize(bound,configuration,cancelled)
        with db.engine.begin() as c:
            c.execute(db.assets.update().where(db.assets.c.id==asset_id).values(version=2))
        return result
    tx._recognize=change_version
    late=queue()
    analysis.process_job(analysis.claim())
    assert state(late["id"])["status"]=="failed"
    assert len(current()["history"])==1
    assert current(old["id"])["current"]["id"]==old["id"]
    ''')


def test_confirmed_sentence_proposal_is_read_only_and_normal_save_is_authoritative(tmp_path):
    run_case(tmp_path, '''
    project=client.get(f"/api/v1/projects/{project_id}").json()
    cleared=project["document"]
    cleared["subtitles"]=[]
    response=client.put(f"/api/v1/projects/{project_id}",json={"expected_revision":project["revision"],
        "request_id":uuid.uuid4().hex,"document":cleared})
    assert response.status_code==200,response.text
    project=response.json()
    transcript=complete(queue())
    transcript=patch(transcript,status="confirmed").json()
    clip=next(c for c in project["document"]["clips"] if c["source"]["asset_id"]==asset_id)
    assert clip["timeline_start_frame"]==0
    body={"expected_revision":project["revision"],"transcript_id":transcript["id"],
        "transcript_revision":transcript["revision"],"clip_id":clip["id"],"segment_ids":["segment-0001"]}
    def persisted():
        with db.engine.connect() as c:
            return {table.name:[dict(r._mapping) for r in c.execute(table.select())]
                for table in (db.projects,db.snapshots,db.receipts)}
    baseline=persisted()
    endpoint=f"/api/v1/projects/{project_id}/subtitle-proposals"
    proposal=client.post(endpoint,json=body)
    assert proposal.status_code==200,proposal.text
    candidate=proposal.json()
    assert len(candidate["added_ids"])==1
    subtitle=candidate["document"]["subtitles"][0]
    assert (subtitle["start_frame"],subtitle["end_frame"])==(3,20)
    assert subtitle["binding"]=={"asset_id":asset_id,"asset_version":clip["source"]["version"],
        "stream_index":clip["source"]["stream_index"],"transcript_id":transcript["id"],
        "transcript_revision":transcript["revision"],"segment_id":"segment-0001","review_required":False,
        "spans":[{"clip_id":clip["id"],"start_frame":3,"end_frame":20}]}
    assert persisted()==baseline
    invalid=json.loads(json.dumps(candidate["document"]))
    invalid["subtitles"][0]["binding"]["asset_id"]="another-asset"
    rejected=client.put(f"/api/v1/projects/{project_id}",json={"expected_revision":project["revision"],
        "request_id":uuid.uuid4().hex,"document":invalid})
    assert rejected.status_code==422 and rejected.json()["error"]["code"]=="INVALID_INPUT"
    assert persisted()==baseline
    unconfirmed=client.post(endpoint,json={**body,"segment_ids":["segment-0002"]})
    assert unconfirmed.status_code==422 and unconfirmed.json()["error"]["code"]=="UNCONFIRMED_TRANSCRIPT"
    stale_project=client.post(endpoint,json={**body,"expected_revision":project["revision"]-1})
    assert stale_project.status_code==409
    stale_transcript=client.post(endpoint,json={**body,"transcript_revision":transcript["revision"]-1})
    assert stale_transcript.status_code==409
    assert persisted()==baseline
    candidate["document"]["subtitles"][0]["text"]="加入工程后的人工改字"
    saved=client.put(f"/api/v1/projects/{project_id}",json={"expected_revision":project["revision"],
        "request_id":uuid.uuid4().hex,"document":candidate["document"]})
    assert saved.status_code==200,saved.text
    before_repeat=persisted()
    repeated=client.post(endpoint,json={**body,"expected_revision":saved.json()["revision"]})
    assert repeated.status_code==200,repeated.text
    repeated=repeated.json()
    assert repeated["added_ids"]==[] and repeated["skipped_count"]==1
    assert repeated["document"]["subtitles"][0]["text"]=="加入工程后的人工改字"
    assert persisted()==before_repeat
    # The normal Document transaction persists the binding in the live project,
    # its snapshot and the immutable render input, without a second write path.
    reopened=client.get(f"/api/v1/projects/{project_id}").json()
    assert reopened["document"]["subtitles"][0]["binding"]==subtitle["binding"]
    with db.engine.connect() as c:
        snapshot=c.execute(db.snapshots.select().where(db.snapshots.c.project_id==project_id,
            db.snapshots.c.revision==reopened["revision"])).first()
        assert json.loads(snapshot.document_json)["subtitles"][0]["binding"]==subtitle["binding"]
    rendered=client.post(f"/api/v1/projects/{project_id}/renders",json={
        "expected_revision":reopened["revision"],"request_id":uuid.uuid4().hex})
    assert rendered.status_code==200,rendered.text
    with db.engine.connect() as c:
        frozen=c.execute(db.jobs.select().where(db.jobs.c.id==rendered.json()["id"])).first()
        assert json.loads(frozen.document_json)["subtitles"][0]["binding"]==subtitle["binding"]
    ''')
