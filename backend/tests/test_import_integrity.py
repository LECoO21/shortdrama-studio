"""Regression probes for duplicate integrity and legacy archive races.

Only synthetic media and a per-test child database are used. No inherited home,
provider credentials, account state, workspace data, or network provider exists.
"""
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from test_uploads import SETUP


def run_case(tmp_path, code):
    env = {"PATH": "/Users/leco/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "STUDIO_DATA_DIR": str(tmp_path), "STUDIO_MODELS_ENABLED": "0", "STUDIO_MODEL_PROVIDER": "none",
        "FFMPEG_BIN": "/Users/leco/.local/bin/ffmpeg"}
    result = subprocess.run([sys.executable, "-c", SETUP + textwrap.dedent(code)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
def test_unavailable_duplicate_never_claims_complete_or_discards_verified_retry(tmp_path, damage):
    run_case(tmp_path, "damage=" + repr(damage) + "\n" + textwrap.dedent('''
    first=uploaded();aid=first["asset"]["id"]
    with db.engine.connect() as c:
        original_row=db.asset_map(c)[aid]
        source=Path(original_row["path"])
    if damage=="missing":
        source.unlink()
    elif damage=="corrupt":
        source.write_bytes(b"preserve this damaged original for inspection")
    else:
        target=db.DATA/"work"/"protected.wav";target.write_bytes(content)
        source.unlink();source.symlink_to(target)
    session=create();assert chunk(session,content).status_code==200
    failed=finish(session)
    assert failed["status"]=="failed" and failed["error"]["code"]=="ORIGINAL_UNAVAILABLE",failed
    with db.engine.connect() as c:
        row=up._get(c,session["id"])
        assert db.asset_map(c)[aid]==original_row
        assert len(db.asset_map(c))==1
    assert up._part(row).read_bytes()==content
    if damage=="missing":
        assert not source.exists()
    elif damage=="corrupt":
        assert source.read_bytes()==b"preserve this damaged original for inspection"
    else:
        assert source.is_symlink() and target.read_bytes()==content
    # Retry retains acknowledged verified bytes; failure is repeatable until
    # the explicitly unavailable original is repaired by a separate action.
    retry=client.post(f"/api/v1/uploads/{session['id']}/retry").json()
    assert retry["status"]=="uploading" and retry["offset"]==len(content)
    assert finish(session)["error"]["code"]=="ORIGINAL_UNAVAILABLE"
    assert up._part(row).read_bytes()==content
    '''))


def test_legacy_link_and_archive_are_serialized_under_write_lock(tmp_path):
    run_case(tmp_path, '''
    from concurrent.futures import ThreadPoolExecutor,TimeoutError
    import threading
    from sqlalchemy import event
    p=production();pid=p["production"]["id"]
    before_link=threading.Event();resume=threading.Event();archive_attempt=threading.Event()
    def observe(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith("INSERT INTO production_assets"):
            before_link.set();assert resume.wait(10)
        if before_link.is_set() and statement=="BEGIN IMMEDIATE":
            archive_attempt.set()
    event.listen(db.engine,"before_cursor_execute",observe)
    def legacy():
        return client.post("/api/v1/assets",files={"file":("same.wav",content,"audio/wav")},
            data={"production_id":pid})
    def archive():
        return client.patch(f"/api/v1/productions/{pid}",json={
            "request_id":uuid.uuid4().hex,"expected_revision":1,"archived":True})
    with ThreadPoolExecutor(max_workers=2) as pool:
        importing=pool.submit(legacy)
        assert before_link.wait(10)
        archiving=pool.submit(archive)
        assert archive_attempt.wait(10)
        try:
            archiving.result(timeout=0.15)
            raise AssertionError("archive committed while the earlier association transaction was still open")
        except TimeoutError:
            pass
        finally:
            resume.set()
        uploaded_result=importing.result(timeout=10);archived=archiving.result(timeout=10)
    event.remove(db.engine,"before_cursor_execute",observe)
    assert uploaded_result.status_code==200,uploaded_result.text
    assert archived.status_code==200,archived.text
    # Link committed first. Only then was the project archived; no association
    # may be added after a completed archive transaction.
    with db.engine.connect() as c:
        assert library.get_production(c,pid).archived
        assert len(c.execute(library.production_assets.select()).all())==1
    ''')


def test_legacy_upload_losing_to_archive_keeps_only_safe_shared_original(tmp_path):
    run_case(tmp_path, '''
    from concurrent.futures import ThreadPoolExecutor
    import threading
    p=production();pid=p["production"]["id"]
    registered=threading.Event();resume=threading.Event();real=main.register_asset
    def pause_after_registration(*args,**kwargs):
        result=real(*args,**kwargs);registered.set();assert resume.wait(10);return result
    main.register_asset=pause_after_registration
    with ThreadPoolExecutor(max_workers=1) as pool:
        importing=pool.submit(lambda:client.post("/api/v1/assets",
            files={"file":("same.wav",content,"audio/wav")},data={"production_id":pid}))
        assert registered.wait(10)
        archived=client.patch(f"/api/v1/productions/{pid}",json={
            "request_id":uuid.uuid4().hex,"expected_revision":1,"archived":True})
        assert archived.status_code==200
        resume.set();result=importing.result(timeout=10)
    error(result,409,"PRODUCTION_ARCHIVED")
    with db.engine.connect() as c:
        assert not c.execute(library.production_assets.select()).first()
        assets=db.asset_map(c);assert len(assets)==1
        assert Path(next(iter(assets.values()))["path"]).read_bytes()==content
    ''')
