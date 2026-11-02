import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {segmentSubtitleProposal} from '../lib/subtitle-segmentation';
import {subtitleCells} from '../vendor/openfilm/subtitle-segments.mjs';
import type {Rational, Subtitle, SubtitleProposal} from '../lib/types';

const dialogue = '我只想陪在哥哥身边而已。可是你为什么一直不肯相信我？';
const longDialogue = '今天我们要一起去寻找那段被遗忘很久的记忆然后再把所有真相告诉大家';

function caption(id: string, text: string, start_frame: number, end_frame: number): Subtitle {
  return {id, track_id: 's', text, start_frame, end_frame, font_size: 36, color: '#FaBC90', bottom_percent: 12};
}

function proposal(fps: Rational = {num: 25, den: 1}): SubtitleProposal {
  return {project_id: 'p', base_revision: 8, transcript_id: 't', transcript_revision: 3, clip_id: 'c',
    added_ids: ['asr-confirmed-line'], skipped_count: 2, outside_count: 1,
    document: {schema_version: 1, project_id: 'p', name: 'test', canvas: {width: 640, height: 360},
      fps, sample_rate: 48000, duration_frames: 1000,
      tracks: [{id: 'v', kind: 'video', enabled: true}, {id: 's', kind: 'subtitle', enabled: true}],
      clips: [{id: 'c', track_id: 'v', timeline_start_frame: 0, duration_frames: 1000,
        source: {asset_id: 'asset', version: 3, stream_index: 0, time_base: {num: 1, den: 90000},
          in_pts: '9007199254740993', out_pts: '9007199258340993'},
        gain_db: -3, muted: false, fade_in_frames: 0, fade_out_frames: 0, fit: 'contain', include_source_audio: true}],
      subtitles: [caption('asr-confirmed-line', dialogue, 25, 225), caption('manual', '保留人工字幕', 500, 550)]}};
}

function newlyAdded(result: SubtitleProposal): Subtitle[] {
  return result.added_ids.map(id => result.document.subtitles.find(subtitle => subtitle.id === id)!);
}

test('Chinese punctuation creates separate captions while preserving exact outer frames and original punctuation', () => {
  const input = proposal(), before = structuredClone(input);
  const result = segmentSubtitleProposal(input), parts = newlyAdded(result);
  assert.deepEqual(parts.map(part => part.text), ['我只想陪在哥哥身边而已。', '可是你为什么一直不肯相信我？']);
  assert.equal(parts[0].id, input.added_ids[0]);
  assert.equal(parts[0].start_frame, 25);
  assert.equal(parts.at(-1)!.end_frame, 225);
  assert.equal(result.base_revision, 8);
  assert.equal(result.transcript_revision, 3);
  assert.equal(result.skipped_count, 2);
  assert.equal(result.outside_count, 1);
  for (const part of parts) {
    assert.deepEqual([part.font_size, part.color, part.bottom_percent, part.track_id], [36, '#FaBC90', 12, 's']);
    assert.ok(Number.isInteger(part.start_frame) && Number.isInteger(part.end_frame));
    assert.ok(part.start_frame < part.end_frame);
  }
  assert.deepEqual(result.document.clips, before.document.clips);
  assert.deepEqual(result.document.subtitles.find(subtitle => subtitle.id === 'manual'), before.document.subtitles[1]);
  assert.deepEqual(input, before);
  result.document.subtitles.find(subtitle => subtitle.id === 'manual')!.text = '只改结果';
  result.document.clips[0].source.in_pts = '0';
  assert.deepEqual(input, before);
});

test('a long Chinese sentence fits default line width without losing words', () => {
  const input = proposal(); input.document.subtitles[0].text = longDialogue;
  const parts = newlyAdded(segmentSubtitleProposal(input));
  assert.ok(parts.length > 1);
  assert.equal(parts.map(part => part.text).join(''), longDialogue);
  assert.ok(parts.every(part => subtitleCells(part.text) <= 16));
  assert.equal(parts[0].start_frame, 25);
  assert.equal(parts.at(-1)!.end_frame, 225);
});

test('mixed text keeps decimals, number units, Latin terms and complete emoji', () => {
  const input = proposal(); input.document.subtitles[0].text = '给我 3.5 秒，把 MIT 协议和 100 元一起记住😊。然后出发！';
  const parts = newlyAdded(segmentSubtitleProposal(input));
  assert.ok(parts.length > 1);
  assert.ok(parts.some(part => part.text.includes('3.5 秒')));
  assert.ok(parts.some(part => part.text.includes('MIT 协议')));
  assert.ok(parts.some(part => part.text.includes('100 元')));
  assert.equal(parts.filter(part => part.text.includes('😊')).length, 1);
  for (const part of parts) {
    assert.doesNotMatch(part.text, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u);
  }
});

test('only added_ids are segmented, including when existing captions contain long dialogue', () => {
  const input = proposal(); input.document.subtitles[1].text = longDialogue;
  const result = segmentSubtitleProposal(input);
  assert.deepEqual(result.document.subtitles.find(subtitle => subtitle.id === 'manual'), input.document.subtitles[1]);
  assert.ok(!result.added_ids.includes('manual'));
});

test('each segmented caption keeps only its own explicit clip-relative binding range', () => {
  const input = proposal();
  input.document.subtitles[0].binding = {asset_id: 'asset', asset_version: 3, stream_index: 0,
    transcript_id: 't', transcript_revision: 3, segment_id: 'line', review_required: false,
    spans: [{clip_id: 'c', start_frame: 25, end_frame: 225}]};
  const before = structuredClone(input), result = segmentSubtitleProposal(input);
  const parts = newlyAdded(result);
  assert.ok(parts.length > 1);
  for (const part of parts) {
    assert.deepEqual(part.binding?.spans, [{clip_id: 'c', start_frame: part.start_frame, end_frame: part.end_frame}]);
    assert.equal(part.binding?.transcript_id, 't');
    assert.equal(part.binding?.segment_id, 'line');
  }
  assert.deepEqual(input, before);
});

test('zero new captions preserve a re-import result and never resplit human-edited captions', () => {
  const input = proposal(); input.added_ids = []; input.document.subtitles[0].text = '已经人工改字，保持原样。';
  const result = segmentSubtitleProposal(input);
  assert.deepEqual(result, input);
  assert.notEqual(result.document, input.document);
});

test('derived identities are stable, bounded and retain the original re-import anchor', () => {
  const input = proposal(); input.document.subtitles[0].id = 'x'.repeat(100); input.added_ids = ['x'.repeat(100)];
  const first = segmentSubtitleProposal(input), again = segmentSubtitleProposal(input);
  assert.deepEqual(first, again);
  assert.equal(first.added_ids[0], 'x'.repeat(100));
  assert.equal(new Set(first.added_ids).size, first.added_ids.length);
  assert.ok(first.added_ids.every(id => id.length <= 100));
});

test('a derived ID collision preserves the existing caption and allocates another stable ID', () => {
  const input = proposal(), taken = segmentSubtitleProposal(input).added_ids[1];
  input.document.subtitles[1].id = taken;
  const first = segmentSubtitleProposal(input), again = segmentSubtitleProposal(input);
  assert.notEqual(first.added_ids[1], taken);
  assert.deepEqual(first.added_ids, again.added_ids);
  assert.deepEqual(first.document.subtitles.find(subtitle => subtitle.id === taken), input.document.subtitles[1]);
  assert.equal(new Set(first.document.subtitles.map(subtitle => subtitle.id)).size, first.document.subtitles.length);
});

test('NTSC frame mapping retains exact first and last project frames without touching large source PTS', () => {
  const input = proposal({num: 30000, den: 1001});
  input.document.subtitles[0].start_frame = 37; input.document.subtitles[0].end_frame = 260;
  const result = segmentSubtitleProposal(input), parts = newlyAdded(result);
  assert.ok(parts.length > 1);
  assert.equal(parts[0].start_frame, 37);
  assert.equal(parts.at(-1)!.end_frame, 260);
  assert.ok(parts.every(part => Number.isSafeInteger(part.start_frame) && part.start_frame < part.end_frame));
  assert.ok(parts.every((part, index) => !index || parts[index - 1].end_frame <= part.start_frame));
  assert.equal(result.document.clips[0].source.in_pts, '9007199254740993');
});

test('a one-frame short caption remains usable; an overlong sentence cannot create zero-frame pieces', () => {
  const input = proposal(); input.document.subtitles[0] = caption(input.added_ids[0], '好。', 25, 26);
  assert.deepEqual(newlyAdded(segmentSubtitleProposal(input)), [input.document.subtitles[0]]);
  input.document.subtitles[0].text = longDialogue;
  const before = structuredClone(input);
  assert.throws(() => segmentSubtitleProposal(input), /不足|时间不足/);
  assert.deepEqual(input, before);
});

test('missing or repeated added IDs are rejected atomically', () => {
  for (const ids of [['missing'], ['asr-confirmed-line', 'asr-confirmed-line']]) {
    const input = proposal(); input.added_ids = ids; const before = structuredClone(input);
    assert.throws(() => segmentSubtitleProposal(input), /ID 重复或不存在/);
    assert.deepEqual(input, before);
  }
});

test('overlapping, zero-length, fractional and out-of-range captions fail instead of being clipped', () => {
  for (const patch of [{start_frame: 225, end_frame: 225}, {start_frame: 25.5}, {end_frame: 1001}, {end_frame: 520}]) {
    const input = proposal(); Object.assign(input.document.subtitles[0], patch);
    assert.throws(() => segmentSubtitleProposal(input), /整数|不足一帧|超出|重叠/);
  }
  const duplicate = proposal(); duplicate.document.subtitles[1].id = duplicate.added_ids[0];
  assert.throws(() => segmentSubtitleProposal(duplicate), /ID 无效或重复/);
});

test('the 500-caption limit includes original captions and is checked before returning a partial result', () => {
  const input = proposal(); input.document.subtitles = [input.document.subtitles[0],
    ...Array.from({length: 499}, (_, index) => caption(`old-${index}`, '原有', 300 + index, 301 + index))];
  const before = structuredClone(input);
  assert.throws(() => segmentSubtitleProposal(input), /超过 500/);
  assert.deepEqual(input, before);
});

test('invalid frame rates are rejected before attempting segmentation', () => {
  for (const fps of [{num: 0, den: 1}, {num: 25, den: 0}, {num: 25.1, den: 1}, {num: NaN, den: 1},
    {num: 1, den: 2}, {num: 121, den: 1}]) {
    assert.throws(() => segmentSubtitleProposal(proposal(fps)), /帧率无效/);
  }
});

test('the vendored module and MIT license retain the pinned upstream content hashes', () => {
  const root = resolve(process.cwd(), 'vendor/openfilm');
  const expected = {
    'subtitle-segments.mjs': 'b4791b6ec527379aeedaa5329ee7efc3855b8b5dc99cf36db25c2d4652e5784f',
    LICENSE: '4f39fecc5d074a4795bb7c55c5aaad1a4505b5e022ae6c2c3b55b4f26e7c43fc',
  };
  for (const [file, hash] of Object.entries(expected)) {
    assert.equal(createHash('sha256').update(readFileSync(resolve(root, file))).digest('hex'), hash);
  }
  assert.deepEqual(JSON.parse(readFileSync(resolve(root, 'SHA256.json'), 'utf8')), expected);
});
