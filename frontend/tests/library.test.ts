import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {API_BASE, ApiError} from '../lib/api';
import {
  filterProductions, library, libraryDuration, libraryFps, productionDraft, validateProductionDraft,
  type EpisodeSummary, type ProductionSummary,
} from '../lib/library';

const production: ProductionSummary = {id: 'production', name: '一部短剧', description: '家庭故事', language: 'zh-CN',
  canvas: {width: 640, height: 320}, fps: {num: 30000, den: 1001}, revision: 3, archived: false,
  created_at: '2026-10-27T00:00:00Z', updated_at: '2026-10-27T00:00:00Z', episode_count: 2, active_episode_count: 1, asset_count: 4};
const episode: EpisodeSummary = {project_id: 'episode/one', production_id: 'production', name: '第 1 集', revision: 4,
  document_revision: 12, archived: false, canvas: production.canvas, fps: production.fps, duration_frames: 1800,
  created_at: production.created_at, updated_at: production.updated_at};
const originalFetch = globalThis.fetch;
afterEach(() => {globalThis.fetch = originalFetch;});

test('editing metadata preserves a non-preset canvas and exact rational frame rate', () => {
  const draft = productionDraft(production);
  draft.name = '  剧名修改  ';
  assert.deepEqual(validateProductionDraft(draft), {name: '剧名修改', description: production.description,
    language: 'zh-CN', canvas: {width: 640, height: 320}, fps: {num: 30000, den: 1001}});
  assert.deepEqual(production.canvas, {width: 640, height: 320});
});

test('new projects use explicit portrait and 25 fps defaults', () => {
  const draft = productionDraft();
  draft.name = '新短剧';
  assert.deepEqual(validateProductionDraft(draft), {name: '新短剧', description: '', language: 'zh-CN', canvas: {width: 1080, height: 1920}, fps: {num: 25, den: 1}});
});

test('invalid defaults fail before any empty or unsupported Document can be created', () => {
  const valid = {...productionDraft(), name: '短剧'};
  for (const changes of [{name: '  '}, {width: '63'}, {height: '1921'}, {width: '1081'}, {width: '10.8'},
    {fps: '29.97'}, {fps: '25/0'}, {fps: '121/1'}, {fps: '1/2'}, {language: ''}, {description: '字'.repeat(5001)}]) {
    assert.throws(() => validateProductionDraft({...valid, ...changes}));
  }
  assert.equal(validateProductionDraft({...valid, description: '字'.repeat(5000)}).description.length, 5000);
});

test('archive filter is reversible and search includes project descriptions', () => {
  const archived = {...production, id: 'archived', archived: true, name: '旧故事', description: '其他内容'};
  assert.deepEqual(filterProductions([production, archived], '家庭', 'active').map(item => item.id), ['production']);
  assert.deepEqual(filterProductions([production, archived], '', 'archived').map(item => item.id), ['archived']);
  assert.equal(filterProductions([production, archived], '', 'all').length, 2);
  assert.equal(archived.archived, true);
});

test('duration and fps labels use the actual saved episode rational', () => {
  assert.equal(libraryDuration(1800, {num: 30000, den: 1001}), '01:00');
  assert.equal(libraryDuration(1499, {num: 25, den: 1}), '00:59');
  assert.equal(libraryFps({num: 30000, den: 1001}), '29.97 fps (30000/1001)');
  assert.equal(libraryDuration(200, {num: 0, den: 1}), '时长未知');
});

test('library reads include archived records and preserve abort protection', async () => {
  const controller = new AbortController(), calls: string[] = [];
  globalThis.fetch = async (input, options) => {
    calls.push(String(input));
    assert.equal(options?.cache, 'no-store');
    assert.equal(options?.signal, controller.signal);
    assert.equal(options?.body, undefined);
    assert.equal(new Headers(options?.headers).get('X-Studio-Client'), '1');
    return Response.json({productions: [], production, episode, assets: []});
  };
  await library.list(controller.signal); await library.context('episode/one', controller.signal);
  assert.deepEqual(calls, [`${API_BASE}/api/v1/productions?include_archived=true`, `${API_BASE}/api/v1/library/context/episode%2Fone`]);
});

test('renaming carries independent metadata and saved Document revisions', async () => {
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/episodes/episode%2Fone`);
    assert.equal(options?.method, 'PATCH');
    assert.deepEqual(JSON.parse(String(options?.body)), {request_id: 'same-receipt', expected_revision: 4,
      expected_document_revision: 12, name: '第 1 集 修订'});
    return Response.json({production, episode});
  };
  await library.updateEpisode(episode, {name: '第 1 集 修订'}, 'same-receipt');
});

test('archiving uses metadata CAS without pretending to save or delete a Document', async () => {
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/episodes/episode%2Fone`);
    assert.equal(options?.method, 'PATCH');
    assert.deepEqual(JSON.parse(String(options?.body)), {request_id: 'archive-receipt', expected_revision: 4, archived: true});
    return Response.json({production, episode});
  };
  await library.updateEpisode(episode, {archived: true}, 'archive-receipt');
});

test('copying a saved episode uses its Document revision and the supplied stable receipt', async () => {
  const bodies: unknown[] = [];
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/episodes/episode%2Fone/copy`);
    bodies.push(JSON.parse(String(options?.body)));
    return Response.json({production, project: {id: 'copy'}});
  };
  await library.copyEpisode(episode, '第 1 集 副本', 'copy-receipt');
  await library.copyEpisode(episode, '第 1 集 副本', 'copy-receipt');
  assert.deepEqual(bodies, [0, 1].map(() => ({request_id: 'copy-receipt', expected_revision: 12, name: '第 1 集 副本'})));
});

test('project default changes are scoped to production metadata', async () => {
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/productions/production`);
    assert.deepEqual(JSON.parse(String(options?.body)), {request_id: 'default-receipt', expected_revision: 3,
      canvas: {width: 1920, height: 1080}, fps: {num: 30, den: 1}});
    return Response.json(production);
  };
  await library.update(production.id, production.revision, {canvas: {width: 1920, height: 1080}, fps: {num: 30, den: 1}}, 'default-receipt');
});

test('a stale metadata revision surfaces its API error rather than silently overwriting', async () => {
  globalThis.fetch = async () => Response.json({error: {code: 'REVISION_CONFLICT', message: '项目信息已更新，请刷新后重试。'}}, {status: 409});
  await assert.rejects(() => library.update(production.id, 2, {name: '旧表单名称'}, 'stale'),
    (error: unknown) => error instanceof ApiError && error.status === 409 && error.code === 'REVISION_CONFLICT');
});
