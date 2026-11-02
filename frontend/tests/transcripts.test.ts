import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {API_BASE, ApiError} from '../lib/api';
import {
  confirmedTranscriptSegments, transcriptDraft, transcriptJobActive, transcriptJobLabel,
  transcriptPreviewSeconds, transcripts, validateTranscriptEdit,
  type TranscriptBinding, type TranscriptGeneration, type TranscriptSegment,
} from '../lib/transcripts';
import type {Asset} from '../lib/types';

const binding: TranscriptBinding = {asset_id: 'asset', asset_version: 2, source_sha256: 'source',
  audio_sha256: 'audio', start_pts: '336000', time_base_num: 1, time_base_den: 48000,
  sample_count: 438000, sample_rate: 16000};
const segment: TranscriptSegment = {id: 's1', text: '一句候选', start_ms: 11800, end_ms: 14600,
  raw_text: '一句候选', raw_start_ms: 11800, raw_end_ms: 14600,
  source_start: {num: 94, den: 5}, source_end: {num: 108, den: 5},
  status: 'pending', human_confirmed: false, uncertainty: ['requires_listen_review']};
const originalFetch = globalThis.fetch;
afterEach(() => {globalThis.fetch = originalFetch;});

test('reading historical transcription is a protected local read with no inference or upload', async () => {
  const controller = new AbortController();
  let calls = 0;
  globalThis.fetch = async (input, options) => {
    ++calls;
    assert.equal(input, `${API_BASE}/api/v1/assets/a%2Fb/transcription?transcript_id=old%2Fresult`);
    assert.equal(options?.method, 'GET');
    assert.equal(options?.cache, 'no-store');
    assert.equal(options?.signal, controller.signal);
    assert.equal(options?.body, undefined);
    assert.equal(new Headers(options?.headers).get('X-Studio-Client'), '1');
    return Response.json({current: null, history: [], latest_job: null, local_models: []});
  };
  assert.equal((await transcripts.read('a/b', 'old/result', controller.signal)).current, null);
  assert.equal(calls, 1);
});

test('starting local recognition explicitly submits medium Chinese with an idempotent request id', async () => {
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/assets/asset/transcription`);
    assert.equal(options?.method, 'POST');
    assert.deepEqual(JSON.parse(String(options?.body)), {request_id: 'request-1', model: 'medium', language: 'zh'});
    return Response.json({id: 'job', status: 'queued'});
  };
  assert.equal((await transcripts.start('asset', 'request-1')).status, 'queued');
});

test('cancel and retry use persistent analysis jobs without constructing a new recognition request', async () => {
  const calls: string[] = [];
  globalThis.fetch = async (input, options) => {
    calls.push(String(input));
    assert.equal(options?.method, 'POST');
    assert.deepEqual(JSON.parse(String(options?.body)), {});
    return Response.json({id: 'job/1', status: 'cancelled'});
  };
  await transcripts.cancel('job/1'); await transcripts.retry('job/1');
  assert.deepEqual(calls, [`${API_BASE}/api/v1/analysis-jobs/job%2F1/cancel`, `${API_BASE}/api/v1/analysis-jobs/job%2F1/retry`]);
});

test('saving one sentence sends its expected generation revision and explicit review status', async () => {
  const edit = validateTranscriptEdit({text: ' 我只想陪在哥哥身边而已 ', start: '11.800', end: '14.600'}, binding, 'confirmed');
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/transcripts/gen%2F2/segments/s%2F1`);
    assert.equal(options?.method, 'PATCH');
    assert.deepEqual(JSON.parse(String(options?.body)), {expected_revision: 3,
      text: '我只想陪在哥哥身边而已', start_ms: 11800, end_ms: 14600, status: 'confirmed'});
    return Response.json({revision: 4});
  };
  assert.equal((await transcripts.save('gen/2', 's/1', 3, edit)).revision, 4);
});

test('conflict and validation messages remain actionable instead of pretending a sentence saved', async () => {
  globalThis.fetch = async () => Response.json({error: {code: 'REVISION_CONFLICT', message: '转写已更新', details: [{message: '请刷新后重试'}]}}, {status: 409});
  await assert.rejects(transcripts.save('gen', 's1', 1, {text: '候选', start_ms: 0, end_ms: 100, status: 'confirmed'}),
    error => error instanceof ApiError && error.status === 409 && error.code === 'REVISION_CONFLICT' && /转写已更新：请刷新后重试/.test(error.message));
});

test('millisecond drafts preserve boundaries without floating-point rounding or silent clipping', () => {
  assert.deepEqual(transcriptDraft({...segment, start_ms: 1, end_ms: 27375}), {text: '一句候选', start: '0.001', end: '27.375'});
  assert.deepEqual(validateTranscriptEdit({text: '对白', start: '0.001', end: '27.375'}, binding, 'pending'),
    {text: '对白', start_ms: 1, end_ms: 27375, status: 'pending'});
  for (const start of ['-1', 'NaN', '1e1', '0.0001', '1,2', '']) {
    assert.throws(() => validateTranscriptEdit({text: '对白', start, end: '2'}, binding, 'confirmed'), /时间/);
  }
  assert.throws(() => validateTranscriptEdit({text: '对白', start: '2', end: '2'}, binding, 'pending'), /结束时间/);
  assert.throws(() => validateTranscriptEdit({text: '对白', start: '0', end: '27.376'}, binding, 'pending'), /可用音轨/);
  assert.throws(() => validateTranscriptEdit({text: '  ', start: '0', end: '1'}, binding, 'confirmed'), /文字/);
  assert.throws(() => validateTranscriptEdit({text: '字'.repeat(1001), start: '0', end: '1'}, binding, 'confirmed'), /1000/);
  assert.throws(() => validateTranscriptEdit({text: '对白', start: '0', end: '0.002'}, {...binding, sample_count: 17}, 'pending'), /可用音轨/);
  assert.equal(validateTranscriptEdit({text: '对白', start: '0', end: '0.001'}, {...binding, sample_count: 17}, 'pending').end_ms, 1);
});

test('only persisted explicitly human-confirmed sentences enter a subtitle proposal', () => {
  const confirmed = {...segment, id: 'yes', status: 'confirmed', human_confirmed: true} as TranscriptSegment;
  const generation = {segments: [segment, confirmed,
    {...segment, id: 'inconsistent', status: 'confirmed', human_confirmed: false},
    {...segment, id: 'rejected', status: 'rejected', human_confirmed: true}]} as TranscriptGeneration;
  assert.deepEqual(confirmedTranscriptSegments(generation).map(value => value.id), ['yes']);
  assert.equal(generation.segments.length, 4);
  assert.deepEqual(confirmedTranscriptSegments(null), []);
});

test('source preview accounts for audio offset and media origin without replacing source fractions', () => {
  const asset = {metadata: {streams: [], media_origin_seconds: 5}} as unknown as Asset;
  assert.equal(transcriptPreviewSeconds(asset, binding, 11800), 13.8);
  assert.deepEqual(segment.source_start, {num: 94, den: 5});
});

test('preview subtracts large integer source anchors before conversion to preserve small seek offsets', () => {
  const anchor = '90071992547409920';
  const asset = {metadata: {streams: [{type: 'audio', start_pts: anchor, time_base: {num: 1, den: 48000}}],
    media_origin_seconds: Number(BigInt(anchor)) / 48000}} as unknown as Asset;
  const delayed = {...binding, start_pts: (BigInt(anchor) + 240n).toString()};
  assert.equal(transcriptPreviewSeconds(asset, delayed, 1000), 1.005);
});

test('task labels separate successful candidate generation from transcript quality approval', () => {
  assert.equal(transcriptJobActive({status: 'queued'} as never), true);
  assert.equal(transcriptJobActive({status: 'succeeded'} as never), false);
  assert.equal(transcriptJobLabel({status: 'succeeded'} as never), '候选已生成 · 待校对');
  assert.equal(transcriptJobLabel({status: 'running', cancel_requested: 1} as never), '正在停止');
});
