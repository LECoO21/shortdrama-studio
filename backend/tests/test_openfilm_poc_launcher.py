"""Isolation checks for the optional editor POC; no dependency installation or server launch."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_spec = importlib.util.spec_from_file_location('openfilm_poc', Path(__file__).resolve().parents[2] / 'scripts/openfilm_poc.py')
assert _spec and _spec.loader
poc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poc)


def test_launch_environment_does_not_inherit_credentials_or_personal_homes(monkeypatch, tmp_path):
    monkeypatch.setattr(poc, 'ROOT', tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-leak')
    monkeypatch.setenv('OPENFILM_HOME', '/personal/openfilm')
    monkeypatch.setenv('CODEX_HOME', '/personal/codex')
    monkeypatch.setenv('HTTPS_PROXY', 'http://private-proxy')
    env = poc.isolated_env()
    assert not any(k in env for k in ['HOME', 'home', 'OPENAI_API_KEY', 'CODEX_HOME', 'HTTPS_PROXY'])
    for key in ['OPENFILM_HOME', 'OPENFILM_SHARED_HOME', 'OPENFILM_LIBRARY', 'OPENFILM_SAVED', 'OPENFILM_EXPORTS', 'PLAYWRIGHT_BROWSERS_PATH', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME']:
        assert Path(env[key]).is_relative_to(tmp_path), key
    assert env['OPENFILM_STUDIO_PORT'] == '4748'
    assert env['OPENFILM_NO_UPDATE_CHECK'] == env['PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD'] == '1'


def test_source_patch_refuses_an_unexpected_source_and_is_repeatable(monkeypatch, tmp_path):
    monkeypatch.setattr(poc, 'CLONE', tmp_path)
    file = tmp_path / 'sample.mjs'
    file.write_text('before\n')
    poc.apply_once(file, 'before\n', 'after\n')
    poc.apply_once(file, 'before\n', 'after\n')
    assert file.read_text() == 'after\n'
    file.write_text('unexpected\n')
    import pytest
    with pytest.raises(RuntimeError, match='refusing unsafe patch'):
        poc.apply_once(file, 'before\n', 'after\n')


def test_status_does_not_expose_launch_key_or_claim_an_unmanaged_process(monkeypatch, tmp_path):
    monkeypatch.setattr(poc, 'ROOT', tmp_path)
    monkeypatch.setattr(poc, 'STATE', tmp_path / 'manager.json')
    monkeypatch.setattr(poc, 'RUN', tmp_path / 'run.json')
    (tmp_path / 'manager.json').write_text('{"pid":1234}')
    (tmp_path / 'run.json').write_text('{"pid":5678,"key":"private-local-key","origin":"http://127.0.0.1:4748"}')
    monkeypatch.setattr(poc, 'alive', lambda pid: True)
    state = poc.status()
    assert not state['running']
    assert 'key' not in state
    assert 'private-local-key' not in str(state)
