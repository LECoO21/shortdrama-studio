import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { assertInside, validateBody, guardRoute, validateRequestBody, isLocalRequest, POC_CSP } from './openfilm_poc_guard.mjs';

test('paths stay in the isolated root, including existing symlink parents', () => {
  const root = mkdtempSync(join(tmpdir(), 'openfilm-guard-'));
  try {
    mkdirSync(join(root, 'imports'));
    assert.equal(assertInside(join(root, 'imports', 'new.mp4'), root), join(root, 'imports', 'new.mp4'));
    assert.throws(() => assertInside(join(root, '..', 'outside.mp4'), root));
    symlinkSync(tmpdir(), join(root, 'escape'));
    assert.throws(() => assertInside(join(root, 'escape', 'private.mp4'), root));
    assert.equal(validateBody({ jobs: [{ folder: join(root, 'exports') }], ops: [{ clip: { src: 'assets/a.mp4#t=1,2' } }] }, root).jobs.length, 1);
    for (const body of [{ paths: ['/Users/other/secret'] }, { projectsFolder: '/Movies/OpenFilm' }, { ops: [{ clip: { src: 'https://example.com/a.mp4' } }] }]) assert.throws(() => validateBody(body, root));
  } finally { rmSync(root, { force: true, recursive: true }); }
});

test('cloud, repository, transcription, native picker and external launch routes are blocked', () => {
  for (const route of ['providers/openai/connect', 'get/asr', 'projects/fetch', 'projects/id/subtitles/transcribe', 'projects/id/subtitles/translate', 'pick-files', 'pick-folder', 'browse', 'skills/a/installed', 'update', 'desktop', 'projects/id/files/relink', 'projects/id/exports/job/open']) {
    assert.equal(guardRoute(route.split('/'), 'POST').status, 403, route);
  }
  assert.equal(guardRoute(['providers'], 'GET').body.providers.length, 0);
  assert.equal(guardRoute(['projects', 'id', 'edit'], 'POST'), null);
  assert.equal(guardRoute(['projects', 'id', 'subtitles'], 'GET'), null);
  assert.throws(() => validateRequestBody({ url: '/api/projects/id/files/bring' }, {}));
});

test('path entry points fail closed on relative project destinations, tildes and encoded traversal', () => {
  const root = mkdtempSync(join(tmpdir(), 'openfilm-paths-'));
  const previous = process.env.OPENFILM_POC_ROOT;
  process.env.OPENFILM_POC_ROOT = root;
  try {
    for (const path of ['../../outside', 'relative-project', '~/Desktop', '~', '%2e%2e/outside', '%252e%252e/outside']) {
      assert.throws(() => validateRequestBody({ url: '/api/projects' }, { path }), path);
      assert.throws(() => validateRequestBody({ url: '/api/open' }, { path }), path);
      assert.throws(() => validateRequestBody({ url: '/api/projects/id/locate' }, { path }), path);
    }
    for (const folder of ['~/Desktop', '~', 'relative-export', '../../outside']) {
      assert.throws(() => validateRequestBody({ url: '/api/projects/id/exports' }, { jobs: [{ kind: 'subtitles' }], folder }), folder);
      assert.throws(() => validateRequestBody({ url: '/api/prefs' }, { projectsFolder: folder }), folder);
    }
    symlinkSync(tmpdir(), join(root, 'escape'));
    assert.throws(() => validateRequestBody({ url: '/api/projects' }, { path: join(root, 'escape', 'outside') }));
    assert.equal(validateRequestBody({ url: '/api/projects' }, { path: join(root, 'new-project') }).path, join(root, 'new-project'));
    assert.equal(validateRequestBody({ url: '/api/projects/id/files/audio' }, { path: '素材（中文路径）/音频.wav' }).path, '素材（中文路径）/音频.wav');
    assert.throws(() => validateRequestBody({ url: '/api/projects/id/files/bring' }, { paths: ['relative.mp4'] }));
  } finally {
    if (previous === undefined) delete process.env.OPENFILM_POC_ROOT; else process.env.OPENFILM_POC_ROOT = previous;
    rmSync(root, { force: true, recursive: true });
  }
});

test('Node fetch accepts only the two isolated local origins; preview CSP has no broad external source', () => {
  assert.equal(isLocalRequest('http://127.0.0.1:4748/api/health'), true);
  assert.equal(isLocalRequest('http://localhost:4749/p/a/film.html'), true);
  for (const url of ['https://api.openai.com/v1/responses', 'http://127.0.0.1:8031', 'http://localhost:3031', 'http://127.0.0.1:4748.evil.test/']) assert.equal(isLocalRequest(url), false);
  assert.ok(POC_CSP.includes("form-action 'none'"));
  assert.ok(!POC_CSP.includes('https: '));
});
