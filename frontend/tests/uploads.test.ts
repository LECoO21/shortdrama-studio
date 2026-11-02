import {test} from 'node:test';
import assert from 'node:assert/strict';
import {importFingerprint, imports, importStatus, transferImport, validateImport, validateResume, type UploadState} from '../lib/uploads';

const state = (change: Partial<UploadState> = {}): UploadState => ({id: 'upload-1', request_id: 'request-1', filename: '片段.mp4',
  size: 9, sha256: 'a'.repeat(64), last_modified: 1, production_id: 'production-1', offset: 0, chunk_size: 4,
  status: 'uploading', asset: null, duplicate: null, error: null, created_at: '', updated_at: '', ...change});
const file = () => new File([new Uint8Array(9)], '片段.mp4', {lastModified: 1});
const asset = {id: 'asset-1', version: 1, sha256: 'a'.repeat(64), filename: '片段.mp4', media_url: '/media', metadata: {streams: []}};

test('local import validation rejects empty, unsupported and oversized files before creating a session', () => {
  assert.throws(() => validateImport({name: '伪装.txt', size: 1}), /支持/);
  assert.throws(() => validateImport({name: '片.mp4', size: 0}), /为空/);
  assert.throws(() => validateImport({name: '片.mp4', size: 100 * 1024 * 1024 + 1}), /100 MiB/);
  validateImport({name: '中文.MOV', size: 123});
});
test('fingerprints cover complete bytes and prevent resuming a same-name same-size file with different content', async () => {
  const original = new File(['123'], '片.mp4', {lastModified: 1});
  const different = new File(['124'], '片.mp4', {lastModified: 1});
  const sha = await importFingerprint(original);
  assert.equal(sha, 'a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3');
  validateResume(original, sha, state({size: 3, sha256: sha}));
  assert.throws(() => validateResume(different, 'b'.repeat(64), state({size: 3, sha256: sha})), /指纹不同/);
});
test('resume reads the acknowledged server offset before sending and never retransmits a completed prefix', async () => {
  const calls: {offset: number; size: number}[] = [];
  let remote = state({offset: 4}); const observations: UploadState[] = [];
  const transport = {...imports, read: async () => remote,
    chunk: async (_id: string, offset: number, blob: Blob) => {calls.push({offset, size: blob.size}); remote = {...remote, offset: offset + blob.size}; return remote;},
    complete: async () => state({offset: 9, status: 'completed', asset})};
  const result = await transferImport(file(), state(), new AbortController().signal, value => observations.push(value), transport);
  assert.deepEqual(calls, [{offset: 4, size: 4}, {offset: 8, size: 1}]);
  assert.equal(observations.at(-2)?.status, 'validating'); assert.equal(result.status, 'completed');
});
test('an uncertain chunk response is recovered from the server offset instead of duplicating the same bytes', async () => {
  let remote = state(); const offsets: number[] = []; let loseResponse = true;
  const transport = {...imports, read: async () => remote,
    chunk: async (_id: string, offset: number, blob: Blob) => {offsets.push(offset); remote = {...remote, offset: offset + blob.size};
      if (loseResponse) {loseResponse = false; throw new Error('响应丢失');} return remote;},
    complete: async () => state({offset: 9, status: 'completed', asset})};
  await assert.rejects(transferImport(file(), state(), new AbortController().signal, () => {}, transport), /响应丢失/);
  await transferImport(file(), state(), new AbortController().signal, () => {}, transport);
  assert.deepEqual(offsets, [0, 4, 8]);
});
test('validation is resumable without re-uploading and a 100 percent byte count is not an import success', async () => {
  const remote = state({offset: 9, status: 'validating'}); let completed = 0;
  const transport = {...imports, read: async () => remote,
    chunk: async () => {throw new Error('must not retransmit');},
    complete: async () => {completed++; return {...remote, status: 'failed' as const, error: {code: 'INVALID_MEDIA', message: '无法解码'}};}};
  await assert.rejects(transferImport(file(), remote, new AbortController().signal, () => {}, transport), /无法解码/);
  assert.equal(completed, 1); assert.match(importStatus(remote), /校验/);
});
test('duplicate successful imports retain their existing asset and expose an explicit duplicate status', async () => {
  const remote = state({offset: 9, status: 'completed', duplicate: true, asset});
  const transport = {...imports, read: async () => remote, complete: async () => {throw new Error('already done');}};
  assert.equal((await transferImport(file(), state(), new AbortController().signal, () => {}, transport)).asset?.id, asset.id);
  assert.match(importStatus(remote), /重复素材/);
});
test('pause stops before the next chunk and never changes a file source or labels it completed', async () => {
  const controller = new AbortController(); controller.abort();
  let sends = 0; const statuses: string[] = [];
  const transport = {...imports, read: async () => state(), chunk: async () => {sends++; return state();}};
  await assert.rejects(transferImport(file(), state(), controller.signal, value => statuses.push(value.status), transport));
  assert.equal(sends, 0); assert.ok(!statuses.includes('completed'));
});
