"""Isolated local media/session/reference integration; no user's data or cloud."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap


SETUP = '''
import copy,hashlib,io,json,os,uuid,wave
from pathlib import Path
from fastapi.testclient import TestClient
from app import storage as db,uploads as up,library
from app import main
db.migrate()
client=TestClient(main.app,headers={"X-Studio-Client":"1"})
buffer=io.BytesIO()
with wave.open(buffer,"wb") as audio:
    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
    audio.writeframes(bytes(16000))
content=buffer.getvalue()
def production(name="测试项目"):
    response=client.post("/api/v1/productions",json={"request_id":uuid.uuid4().hex,"name":name})
    assert response.status_code==200,response.text
    return response.json()
def create(data=content,pid=None,**overrides):
    body={"request_id":uuid.uuid4().hex,"filename":"测试音轨.wav","size":len(data),
          "sha256":hashlib.sha256(data).hexdigest(),"last_modified":1234567,"production_id":pid}
    body.update(overrides)
    response=client.post("/api/v1/uploads",json=body)
    assert response.status_code==200,response.text
    return response.json()
def chunk(session,data,offset=0):
    return client.put(f"/api/v1/uploads/{session['id']}/chunks",params={"offset":offset},
        content=data,headers={"Content-Type":"application/octet-stream"})
def finish(session):
    response=client.post(f"/api/v1/uploads/{session['id']}/complete")
    assert response.status_code==200,response.text
    return response.json()
def uploaded(pid=None):
    session=create(pid=pid)
    assert chunk(session,content).status_code==200
    result=finish(session)
    assert result["status"]=="completed",result
    return result
def error(response,status,code):
    assert response.status_code==status,response.text
    assert response.json()["error"]["code"]==code,response.text
'''


def run_case(tmp_path, code, timeout=60):
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
    env.update(STUDIO_DATA_DIR=str(tmp_path), STUDIO_MODELS_ENABLED="0", STUDIO_MODEL_PROVIDER="none")
    result = subprocess.run([sys.executable, "-c", SETUP + textwrap.dedent(code)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_interrupted_resume_hash_replay_offset_and_full_public_asset(tmp_path):
    run_case(tmp_path, '''
    p=production();pid=p["production"]["id"]
    session=create(pid=pid)
    first=chunk(session,content[:5000]);assert first.status_code==200,first.text
    assert first.json()["offset"]==5000
    assert chunk(session,content[:5000]).json()["offset"]==5000
    error(chunk(session,b"different"),409,"UPLOAD_CHUNK_CONFLICT")
    error(chunk(session,content[5000:],offset=4000),409,"UPLOAD_OFFSET_CONFLICT")
    error(client.post(f"/api/v1/uploads/{session['id']}/complete"),409,"UPLOAD_INCOMPLETE")
    client.close();client=TestClient(main.app,headers={"X-Studio-Client":"1"})
    saved=client.get(f"/api/v1/uploads/{session['id']}").json()
    assert saved["offset"]==5000 and saved["sha256"]==session["sha256"]
    assert chunk(session,content[5000:],offset=5000).json()["offset"]==len(content)
    result=finish(session)
    assert result["status"]=="completed" and result["duplicate"] is False
    assert result["asset"]["metadata"]["audio_stream_index"]==0
    assert result["asset"]["size_bytes"]==len(content)
    assert result["asset"]["created_at"]
    assert "path" not in result["asset"] and str(db.DATA) not in json.dumps(result)
    assert finish(session)==result
    with db.engine.connect() as c:
        assert len(c.execute(library.production_assets.select()).all())==1
        assert not c.execute(db.analyses.select()).first()
        row=up._get(c,session["id"])
    assert not up._part(row).exists()
    assert client.get("/api/v1/uploads",params={"production_id":pid}).json()["uploads"]==[result]
    ''')


def test_crash_tail_discarded_to_committed_offset_before_resume(tmp_path):
    run_case(tmp_path, '''
    session=create();chunk(session,content[:5000])
    with db.engine.connect() as c: row=up._get(c,session["id"])
    # Simulate fsync completed before the corresponding DB transaction committed.
    with up._part(row).open("ab") as stream: stream.write(b"unacknowledged crash tail")
    assert chunk(session,content[5000:],offset=5000).status_code==200
    assert finish(session)["status"]=="completed"
    with db.engine.connect() as c:
        asset=next(iter(db.asset_map(c).values()))
    assert Path(asset["path"]).read_bytes()==content
    ''')


def test_missing_or_truncated_part_fails_and_retry_restarts_without_publish(tmp_path):
    run_case(tmp_path, '''
    session=create();chunk(session,content[:5000])
    with db.engine.connect() as c: row=up._get(c,session["id"])
    up._part(row).write_bytes(content[:100])
    response=chunk(session,content[5000:],offset=5000)
    assert response.json()["status"]=="failed" and response.json()["error"]["code"]=="UPLOAD_PART_MISSING"
    with db.engine.connect() as c: assert not c.execute(db.assets.select()).first()
    retry=client.post(f"/api/v1/uploads/{session['id']}/retry").json()
    assert retry["status"]=="uploading" and retry["offset"]==0
    assert chunk(session,content).status_code==200
    assert finish(session)["status"]=="completed"
    # Replaying an acknowledged chunk cannot conceal a now-missing part file.
    replay_session=create();chunk(replay_session,content[:5000])
    with db.engine.connect() as c: replay_row=up._get(c,replay_session["id"])
    up._part(replay_row).unlink()
    assert chunk(replay_session,content[:5000]).json()["error"]["code"]=="UPLOAD_PART_MISSING"
    ''')


def test_full_hash_prevents_same_name_size_mtime_wrong_file_publication(tmp_path):
    run_case(tmp_path, '''
    session=create();wrong=content[:-1]+b"x"
    assert len(wrong)==len(content)
    assert chunk(session,wrong).status_code==200
    failed=finish(session)
    assert failed["status"]=="failed" and failed["error"]["code"]=="UPLOAD_HASH_MISMATCH"
    with db.engine.connect() as c: assert not c.execute(db.assets.select()).first()
    reset=client.post(f"/api/v1/uploads/{session['id']}/retry").json()
    assert reset["offset"]==0 and reset["status"]=="uploading"
    assert chunk(session,content).status_code==200
    assert finish(session)["status"]=="completed"
    ''')


def test_idempotency_limits_filename_format_and_local_boundary(tmp_path):
    run_case(tmp_path, '''
    session=create()
    replay=create(request_id=session["request_id"])
    assert replay==session
    body={key:session[key] for key in ("request_id","filename","size","sha256","last_modified","production_id")}
    body["last_modified"]+=1
    error(client.post("/api/v1/uploads",json=body),409,"IDEMPOTENCY_CONFLICT")
    for fields,code in [({"size":0},"INVALID_INPUT"),({"size":up.MAX_SIZE+1},"INVALID_INPUT"),
                        ({"filename":"../secret.wav"},"INVALID_FILENAME"),
                        ({"filename":"movie.exe"},"FORMAT_UNSUPPORTED"),({"sha256":"bad"},"INVALID_INPUT")]:
        invalid={**body,**fields,"request_id":uuid.uuid4().hex}
        error(client.post("/api/v1/uploads",json=invalid),422,code)
    error(chunk(session,b""),422,"UPLOAD_CHUNK_SIZE")
    error(chunk(session,b"x"*(up.CHUNK_SIZE+1)),413,"UPLOAD_CHUNK_SIZE")
    error(client.put(f"/api/v1/uploads/{session['id']}/chunks?offset=0",content=b"x"),415,"UPLOAD_CONTENT_TYPE")
    with TestClient(main.app) as foreign:
        error(foreign.post("/api/v1/uploads",json=body),403,"LOCAL_ACCESS_ONLY")
    ''')


def test_duplicate_cross_production_preserves_immutable_original(tmp_path):
    run_case(tmp_path, '''
    p1=production("第一项目");p2=production("第二项目")
    first=uploaded(p1["production"]["id"])
    with db.engine.connect() as c: asset=db.asset_map(c)[first["asset"]["id"]]
    source=Path(asset["path"]);before=source.stat()
    second=uploaded(p2["production"]["id"])
    after=source.stat()
    assert second["duplicate"] is True and second["asset"]["id"]==first["asset"]["id"]
    assert (before.st_ino,before.st_mtime_ns,before.st_size)==(after.st_ino,after.st_mtime_ns,after.st_size)
    assert source.read_bytes()==content
    with db.engine.connect() as c:
        assert len(c.execute(db.assets.select()).all())==1
        assert len(c.execute(library.production_assets.select()).all())==2
    ''')


def test_cancel_cleans_only_own_partial_and_completed_original_is_preserved(tmp_path):
    run_case(tmp_path, '''
    a=create();b=create();chunk(a,content[:5000]);chunk(b,content[:5000])
    with db.engine.connect() as c: ar=up._get(c,a["id"]);br=up._get(c,b["id"])
    cancelled=client.post(f"/api/v1/uploads/{a['id']}/cancel").json()
    assert cancelled["status"]=="cancelled"
    assert not up._part(ar).exists() and up._part(br).exists()
    error(client.post(f"/api/v1/uploads/{a['id']}/retry"),409,"UPLOAD_CANCELLED")
    error(chunk(a,content[5000:],offset=5000),409,"UPLOAD_NOT_WRITABLE")
    assert chunk(b,content[5000:],offset=5000).status_code==200
    result=finish(b)
    assert client.post(f"/api/v1/uploads/{b['id']}/cancel").json()==result
    with db.engine.connect() as c: assert Path(db.asset_map(c)[result["asset"]["id"]]["path"]).is_file()
    ''')


def test_invalid_media_and_duration_never_published_as_success(tmp_path):
    run_case(tmp_path, '''
    bad=create(data=b"invalid media bytes")
    chunk(bad,b"invalid media bytes")
    assert finish(bad)["status"]=="failed"
    assert client.post(f"/api/v1/uploads/{bad['id']}/retry").json()["offset"]==len(b"invalid media bytes")
    assert finish(bad)["status"]=="failed"
    from app import media
    original=media.probe_media
    media.probe_media=lambda p:{"duration_seconds":601}
    long=create();chunk(long,content)
    result=finish(long)
    assert result["status"]=="failed" and result["error"]["code"]=="MEDIA_TOO_LONG"
    media.probe_media=original
    with db.engine.connect() as c: assert not c.execute(db.assets.select()).first()
    ''')


def test_validating_and_post_registration_crash_resume_idempotently(tmp_path):
    run_case(tmp_path, '''
    session=create();chunk(session,content)
    original=main.register_asset
    class Crash(BaseException): pass
    def crashing(path,filename):
        original(path,filename)
        raise Crash()
    main.register_asset=crashing
    try: up.complete_upload(session["id"])
    except Crash: pass
    else: raise AssertionError("expected injected crash")
    state=client.get(f"/api/v1/uploads/{session['id']}").json()
    assert state["status"]=="validating"
    with db.engine.connect() as c:
        asset=next(iter(db.asset_map(c).values()));before=Path(asset["path"]).stat()
    main.register_asset=original
    done=finish(session)
    assert done["status"]=="completed" and done["duplicate"] is False
    with db.engine.connect() as c:
        assert len(c.execute(db.assets.select()).all())==1
        after=Path(db.asset_map(c)[done["asset"]["id"]]["path"]).stat()
    assert before.st_ino==after.st_ino and before.st_mtime_ns==after.st_mtime_ns
    ''')


def test_archive_during_registration_retains_asset_and_retryable_association(tmp_path):
    run_case(tmp_path, '''
    p=production();pid=p["production"]["id"]
    session=create(pid=pid);chunk(session,content)
    original=main.register_asset
    def archive_after_register(path,filename):
        asset=original(path,filename)
        with db.engine.begin() as c:
            c.execute(library.productions.update().where(library.productions.c.id==pid).values(archived=1))
        return asset
    main.register_asset=archive_after_register
    failed=finish(session)
    assert failed["status"]=="failed" and failed["error"]["code"]=="PRODUCTION_ARCHIVED"
    assert failed["asset"] is not None
    with db.engine.connect() as c:
        assert len(c.execute(db.assets.select()).all())==1
        assert not c.execute(library.production_assets.select()).first()
    main.register_asset=original
    with db.engine.begin() as c:
        c.execute(library.productions.update().where(library.productions.c.id==pid).values(archived=0))
    assert client.post(f"/api/v1/uploads/{session['id']}/retry").json()["status"]=="uploading"
    assert finish(session)["status"]=="completed"
    ''')


def test_concurrent_chunk_replay_has_single_committed_append(tmp_path):
    run_case(tmp_path, '''
    from concurrent.futures import ThreadPoolExecutor
    session=create()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:up._append(session["id"],0,content),range(2)))
    assert all(result["offset"]==len(content) for result in results)
    with db.engine.connect() as c:
        row=up._get(c,session["id"])
        assert len(json.loads(row.chunks_json))==1
    assert up._part(row).stat().st_size==len(content)
    assert finish(session)["status"]=="completed"
    ''')


def test_link_removal_checks_current_snapshot_job_and_preserves_other_production(tmp_path):
    run_case(tmp_path, '''
    p1=production("引用项目");p2=production("无引用项目")
    pid1=p1["production"]["id"];pid2=p2["production"]["id"]
    uploaded1=uploaded(pid1);aid=uploaded1["asset"]["id"];uploaded(pid2)
    doc=copy.deepcopy(p1["project"]["document"])
    audio=uploaded1["asset"]["metadata"]["streams"][0]
    doc["duration_frames"]=12
    doc["clips"]=[{"id":"audio-1","track_id":"audio","source":{
        "asset_id":aid,"version":1,"stream_index":0,"time_base":audio["time_base"],
        "in_pts":"0","out_pts":"7680"},"timeline_start_frame":0,"duration_frames":12}]
    project_id=p1["project"]["id"]
    saved=client.put(f"/api/v1/projects/{project_id}",json={"request_id":uuid.uuid4().hex,
        "expected_revision":1,"document":doc})
    assert saved.status_code==200,saved.text
    with db.engine.begin() as c:
        c.execute(db.jobs.insert().values(id="frozen",project_id=project_id,revision=2,
            document_json=db.dumps(saved.json()["document"]),status="failed",created_at=db.now(),updated_at=db.now()))
    refs=client.get(f"/api/v1/assets/{aid}/references",params={"production_id":pid1}).json()
    assert not refs["can_remove"]
    assert {v["kind"] for v in refs["blockers"]}=={"project","snapshot","job"}
    url=f"/api/v1/productions/{pid1}/assets/{aid}/remove"
    error(client.post(url,json={"request_id":uuid.uuid4().hex}),409,"ASSET_REFERENCED")
    other_refs=client.get(f"/api/v1/assets/{aid}/references",params={"production_id":pid2}).json()
    assert other_refs["can_remove"] and other_refs["references"]["projects"]
    request_id=uuid.uuid4().hex
    remove_url=f"/api/v1/productions/{pid2}/assets/{aid}/remove"
    removed=client.post(remove_url,json={"request_id":request_id}).json()
    assert removed["removed"] and removed["original_preserved"]
    assert client.post(remove_url,json={"request_id":request_id}).json()==removed
    with db.engine.connect() as c:
        links=c.execute(library.production_assets.select()).all()
        assert len(links)==1 and links[0].production_id==pid1
        assert Path(db.asset_map(c)[aid]["path"]).read_bytes()==content
    restored=client.post(f"/api/v1/productions/{pid2}/assets",json={"request_id":uuid.uuid4().hex,"asset_ids":[aid]})
    assert restored.status_code==200 and restored.json()["asset_count"]==1
    ''')


def test_analyses_transcripts_are_visible_but_link_removal_preserves_them(tmp_path):
    run_case(tmp_path, '''
    p=production();pid=p["production"]["id"];done=uploaded(pid);aid=done["asset"]["id"]
    with db.engine.begin() as c:
        c.execute(db.analyses.insert().values(asset_id=aid,version=1,revision=1,manifest_json="{}",updated_at=db.now()))
        c.execute(db.analysis_jobs.insert().values(id="analysis-1",asset_id=aid,version=1,mode="local",status="queued"))
        c.execute(db.transcripts.insert().values(id="transcript-1",asset_id=aid,version=1,generation=1,
            revision=1,job_id="transcript-job",model="medium",raw_json="{}",corrections_json="{}"))
    refs=client.get(f"/api/v1/assets/{aid}/references",params={"production_id":pid}).json()
    assert refs["can_remove"] and refs["references"]["analyses"] and refs["references"]["transcripts"]
    response=client.post(f"/api/v1/productions/{pid}/assets/{aid}/remove",json={"request_id":uuid.uuid4().hex})
    assert response.status_code==200,response.text
    with db.engine.connect() as c:
        for table in (db.assets,db.analyses,db.analysis_jobs,db.transcripts):
            assert len(c.execute(table.select()).all())==1
    ''')


def test_video_without_audio_is_valid_and_archive_blocks_library_mutation(tmp_path):
    run_case(tmp_path, '''
    import subprocess
    from app.media import ffmpeg_binary
    video=db.DATA/"work"/"silent.mp4"
    subprocess.run([ffmpeg_binary(),"-nostdin","-y","-loglevel","error","-f","lavfi",
        "-i","color=c=red:s=64x64:r=25:d=0.2","-c:v","libx264","-pix_fmt","yuv420p",str(video)],check=True)
    data=video.read_bytes()
    p=production();pid=p["production"]["id"]
    session=create(data=data,pid=pid,filename="无音轨.mp4")
    assert chunk(session,data).status_code==200
    done=finish(session)
    assert done["status"]=="completed"
    assert done["asset"]["metadata"]["audio_stream_index"] is None
    aid=done["asset"]["id"]
    with db.engine.begin() as c:
        c.execute(library.productions.update().where(library.productions.c.id==pid).values(archived=1))
    error(client.post(f"/api/v1/productions/{pid}/assets/{aid}/remove",
        json={"request_id":uuid.uuid4().hex}),409,"PRODUCTION_ARCHIVED")
    with db.engine.connect() as c:
        assert c.execute(library.production_assets.select()).first()
        assert Path(db.asset_map(c)[aid]["path"]).read_bytes()==data
    ''')


def test_parallel_completion_registers_once_with_duplicate_notification(tmp_path):
    run_case(tmp_path, '''
    from concurrent.futures import ThreadPoolExecutor
    a=create();b=create();chunk(a,content);chunk(b,content)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda s:up.complete_upload(s["id"]),(a,b)))
    assert all(result["status"]=="completed" for result in results)
    assert sorted(result["duplicate"] for result in results)==[False,True]
    with db.engine.connect() as c:
        assert len(c.execute(db.assets.select()).all())==1
    ''')
