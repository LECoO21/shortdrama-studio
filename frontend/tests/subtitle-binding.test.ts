import assert from 'node:assert/strict';
import {test} from 'node:test';
import {frameToPts, runCoreAction} from '../lib/editor';
import {removeClip, reorderVideo, splitSelection, trimClip} from '../lib/timeline-editing';
import {bindSubtitleToClip, detachClipSubtitleBindings, detachSubtitleBinding, reconcileSubtitleBindings,
  retimeSubtitle, sliceSubtitleBinding, validateSubtitleBindings} from '../lib/subtitle-binding';
import type {Asset, Clip, ProjectDocument, Rational, Subtitle} from '../lib/types';

const anchor = 9007199254740995n;
const videoBase: Rational = {num: 1, den: 90000};
const audioBase: Rational = {num: 1, den: 48000};

function fixture(fps: Rational = {num: 25, den: 1}) {
  const asset: Asset = {id: 'source', version: 2, sha256: 'original', filename: 'take.mp4',
    media_url: '/api/v1/assets/source/media', metadata: {video_stream_index: 0, audio_stream_index: 1,
      streams: [
        {index: 0, type: 'video', time_base: videoBase, start_pts: anchor.toString(),
          end_pts: (anchor + frameToPts(600, fps, videoBase)).toString()},
        {index: 1, type: 'audio', time_base: audioBase, start_pts: anchor.toString(),
          end_pts: (anchor + frameToPts(600, fps, audioBase)).toString()},
      ]}};
  const clip = (id: string, start: number, kind: 'video' | 'audio' = 'video'): Clip => {
    const time_base = kind === 'video' ? videoBase : audioBase;
    return {id, track_id: kind === 'video' ? 'v' : 'a', timeline_start_frame: start, duration_frames: 100,
      source: {asset_id: asset.id, version: asset.version, stream_index: kind === 'video' ? 0 : 1,
        time_base, in_pts: (anchor + frameToPts(80, fps, time_base)).toString(),
        out_pts: (anchor + frameToPts(180, fps, time_base)).toString()},
      gain_db: -3, muted: false, fade_in_frames: 0, fade_out_frames: 0, fit: 'contain', include_source_audio: kind === 'video'};
  };
  const document: ProjectDocument = {schema_version: 1, project_id: 'p', name: 'test', canvas: {width: 640, height: 360},
    fps, sample_rate: 48000, duration_frames: 300,
    tracks: [{id: 'v', kind: 'video', enabled: true}, {id: 'a', kind: 'audio', enabled: true},
      {id: 's', kind: 'subtitle', enabled: true}],
    clips: [clip('one', 0), clip('two', 100), clip('three', 200)], subtitles: []};
  return {document, asset, audio: clip('sound', 20, 'audio')};
}

function subtitle(id: string, start: number, end: number): Subtitle {
  return {id, track_id: 's', text: '保留整句文字。', start_frame: start, end_frame: end,
    font_size: 31, color: '#AAbbCC', bottom_percent: 9};
}

function captions(fps?: Rational) {
  const {document, asset, audio} = fixture(fps);
  document.subtitles = [subtitle('first', 20, 80), subtitle('second', 120, 160), subtitle('independent', 250, 280)];
  const bound = bindSubtitleToClip(bindSubtitleToClip(document, 'first', 'one'), 'second', 'two');
  bound.subtitles[0].binding = {...bound.subtitles[0].binding!, transcript_id: 'transcript',
    transcript_revision: 4, segment_id: 'reviewed-line'};
  return {document: bound, asset, audio};
}

function find(document: ProjectDocument, id = 'first') {
  return document.subtitles.find(item => item.id === id)!;
}

function moved(document: ProjectDocument, id: string, start: number) {
  const after = structuredClone(document);
  after.clips.find(clip => clip.id === id)!.timeline_start_frame = start;
  after.duration_frames = Math.max(after.duration_frames, start + after.clips.find(clip => clip.id === id)!.duration_frames);
  return reconcileSubtitleBindings(document, after);
}

test('explicit binding records the selected instance and does not infer transcript provenance', () => {
  const {document} = fixture(); document.subtitles = [subtitle('manual', 20, 80)];
  const before = structuredClone(document), next = bindSubtitleToClip(document, 'manual', 'one');
  assert.deepEqual(next.subtitles[0].binding, {asset_id: 'source', asset_version: 2, stream_index: 0,
    review_required: false, spans: [{clip_id: 'one', start_frame: 20, end_frame: 80}]});
  assert.deepEqual({...next.subtitles[0], binding: undefined}, {...before.subtitles[0], binding: undefined});
  assert.deepEqual(document, before);
  assert.throws(() => bindSubtitleToClip(document, 'manual', 'two'), /完整位于/);
});

test('two copies of one source follow their own instances; old independent captions stay absolute', () => {
  const {document} = captions(), before = structuredClone(document);
  const next = reorderVideo(document, 'one', 299);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [220, 280]);
  assert.deepEqual([find(next, 'second').start_frame, find(next, 'second').end_frame], [20, 60]);
  assert.deepEqual(find(next, 'independent'), find(document, 'independent'));
  assert.equal(find(next).binding!.spans[0].clip_id, 'one');
  assert.equal(find(next, 'second').binding!.spans[0].clip_id, 'two');
  assert.deepEqual(next.clips.map(clip => clip.source), document.clips.map(clip => clip.source));
  assert.deepEqual(document, before); validateSubtitleBindings(next);
});

test('free media movement follows a bound caption without mutating its source or input', () => {
  const {document} = captions(), before = structuredClone(document), next = moved(document, 'one', 300);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [320, 380]);
  assert.deepEqual(find(next, 'second'), find(document, 'second'));
  assert.deepEqual(find(next, 'independent'), find(document, 'independent'));
  assert.deepEqual(next.clips[0].source, document.clips[0].source);
  assert.deepEqual(document, before);
});

test('video in-trim clips its own caption once and shifts later bound captions once', () => {
  const {document, asset} = captions(), before = structuredClone(document);
  const next = trimClip(document, [asset], 'one', 'in', 30);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [0, 50]);
  assert.deepEqual(find(next).binding!.spans, [{clip_id: 'one', start_frame: 0, end_frame: 50}]);
  assert.equal(find(next).binding!.review_required, true);
  assert.equal(find(next).text, find(document).text);
  assert.deepEqual([find(next, 'second').start_frame, find(next, 'second').end_frame], [90, 130]);
  assert.deepEqual([find(next, 'independent').start_frame, find(next, 'independent').end_frame], [220, 250]);
  assert.equal(BigInt(next.clips[0].source.in_pts) - BigInt(document.clips[0].source.in_pts), frameToPts(30, document.fps, videoBase));
  assert.deepEqual(document, before);
});

test('video out-trim clamps derived captions, keeps their text, and marks a review', () => {
  const {document, asset} = captions();
  const next = trimClip(document, [asset], 'one', 'out', -40);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [20, 60]);
  assert.equal(find(next).text, find(document).text);
  assert.equal(find(next).binding!.review_required, true);
  assert.deepEqual([find(next, 'second').start_frame, find(next, 'second').end_frame], [80, 120]);
  assert.equal(find(next, 'second').binding!.review_required, false);
});

test('extending an in-point retains the original cue content and relative timing without a new review', () => {
  const {document, asset} = captions();
  const next = trimClip(document, [asset], 'one', 'in', -10);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [30, 90]);
  assert.deepEqual(find(next).binding!.spans, [{clip_id: 'one', start_frame: 30, end_frame: 90}]);
  assert.equal(find(next).binding!.review_required, false);
  assert.deepEqual([find(next, 'second').start_frame, find(next, 'second').end_frame], [130, 170]);
});

test('independent audio movement and trimming follow only that audio caption', () => {
  const {document, asset, audio} = fixture(); document.clips.push(audio);
  document.subtitles = [subtitle('sound-caption', 30, 60), subtitle('legacy', 150, 170)];
  const bound = bindSubtitleToClip(document, 'sound-caption', 'sound');
  const movedAudio = moved(bound, 'sound', 35);
  assert.deepEqual([find(movedAudio, 'sound-caption').start_frame, find(movedAudio, 'sound-caption').end_frame], [45, 75]);
  assert.deepEqual(movedAudio.clips.filter(clip => clip.track_id === 'v'), document.clips.filter(clip => clip.track_id === 'v'));
  const trimmed = trimClip(bound, [asset], 'sound', 'in', 20);
  assert.deepEqual([find(trimmed, 'sound-caption').start_frame, find(trimmed, 'sound-caption').end_frame], [40, 60]);
  assert.equal(find(trimmed, 'sound-caption').binding!.review_required, true);
  assert.deepEqual(find(trimmed, 'legacy'), find(document, 'legacy'));
});

test('removing media deletes only its fully owned captions and keeps legacy ripple behavior', () => {
  const {document} = captions(), before = structuredClone(document);
  const ripple = removeClip(document, 'one');
  assert.equal(find(ripple), undefined);
  assert.deepEqual([find(ripple, 'second').start_frame, find(ripple, 'second').end_frame], [20, 60]);
  assert.deepEqual([find(ripple, 'independent').start_frame, find(ripple, 'independent').end_frame], [150, 180]);
  const local = removeClip(document, 'one', false);
  assert.equal(find(local), undefined);
  assert.deepEqual(find(local, 'second'), find(document, 'second'));
  assert.deepEqual(find(local, 'independent'), find(document, 'independent'));
  assert.deepEqual(document, before);
});

test('real media split preserves one caption and gives a crossing cue two contiguous owners', async () => {
  const {document, asset} = captions(), before = structuredClone(document);
  const split = await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 50);
  assert.equal(split.document.subtitles.length, document.subtitles.length);
  assert.deepEqual([find(split.document).text, find(split.document).start_frame, find(split.document).end_frame],
    [find(document).text, 20, 80]);
  const right = split.document.clips.find(clip => clip.id !== 'one' && clip.timeline_start_frame === 50)!;
  assert.deepEqual(find(split.document).binding!.spans, [
    {clip_id: 'one', start_frame: 20, end_frame: 50}, {clip_id: right.id, start_frame: 0, end_frame: 30}]);
  assert.equal(find(split.document).binding!.review_required, false);
  assert.deepEqual(find(split.document, 'second'), find(document, 'second'));
  assert.deepEqual(document, before); validateSubtitleBindings(split.document);
});

test('a split at caption edges reassigns ownership without splitting or losing caption frames', async () => {
  for (const cut of [20, 80]) {
    const {document, asset} = captions();
    const next = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, cut)).document;
    const right = next.clips.find(clip => clip.id !== 'one' && clip.timeline_start_frame === cut)!;
    assert.equal(find(next).binding!.spans.length, 1);
    assert.equal(find(next).binding!.spans[0].clip_id, cut === 20 ? right.id : 'one');
    assert.deepEqual([find(next).start_frame, find(next).end_frame], [20, 80]);
    assert.equal(next.subtitles.length, document.subtitles.length);
  }
});

test('an unrelated split half remains movable and a multi-span cue rejects mixed media clocks or kinds', async () => {
  const {document, asset} = captions();
  const edgeSplit = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 80)).document;
  const unrelated = edgeSplit.clips.find(clip => clip.id !== 'one' && clip.timeline_start_frame === 80)!;
  const next = moved(edgeSplit, unrelated.id, 280);
  assert.deepEqual(find(next), find(edgeSplit));
  const crossing = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 50)).document;
  const rightId = find(crossing).binding!.spans[1].clip_id;
  for (const changeKind of [true, false]) {
    const invalid = structuredClone(crossing), right = invalid.clips.find(clip => clip.id === rightId)!;
    if (changeKind) right.track_id = 'a';
    else right.source.time_base = {num: 1, den: 48000};
    assert.throws(() => validateSubtitleBindings(invalid), /相同媒体类型和源时间基/);
  }
});

test('moving one half of a crossing caption refuses atomically; detaching permits independent editing', async () => {
  const {document, asset} = captions();
  const split = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 50)).document;
  const right = find(split).binding!.spans[1].clip_id, before = structuredClone(split);
  assert.throws(() => moved(split, right, 250), /不连续|倒序/);
  assert.deepEqual(split, before);
  const detached = detachSubtitleBinding(split, 'first'), next = moved(detached, right, 250);
  assert.deepEqual(find(next), find(detached));
  assert.equal(find(next).binding, undefined);
  assert.deepEqual(next.clips.find(clip => clip.id === right)!.source, split.clips.find(clip => clip.id === right)!.source);
});

test('trimming a split half keeps a crossing caption whole and marks only actual content loss', async () => {
  const {document, asset} = captions();
  const split = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 50)).document;
  const next = trimClip(split, [asset], 'one', 'out', -10);
  assert.equal(next.subtitles.length, split.subtitles.length);
  assert.deepEqual([find(next).text, find(next).start_frame, find(next).end_frame], [find(split).text, 20, 70]);
  assert.equal(find(next).binding!.review_required, true);
  assert.equal(find(next).binding!.spans.length, 2);
});

test('selected caption split restricts each binding without adding any media split', async () => {
  const {document, asset} = captions();
  const split = (await splitSelection(document, [asset], {kind: 'clip', id: 'one'}, 50)).document;
  const before = structuredClone(split), next = (await splitSelection(split, [asset], {kind: 'subtitle', id: 'first'}, 50)).document;
  const left = find(next), right = next.subtitles.find(subtitle => !split.subtitles.some(old => old.id === subtitle.id))!;
  assert.deepEqual(next.clips, before.clips);
  assert.equal(left.text, find(split).text); assert.equal(right.text, left.text);
  assert.deepEqual(left.binding!.spans, [{clip_id: 'one', start_frame: 20, end_frame: 50}]);
  assert.deepEqual(right.binding!.spans, [{clip_id: find(split).binding!.spans[1].clip_id, start_frame: 0, end_frame: 30}]);
  assert.equal(next.subtitles.length, split.subtitles.length + 1);
  assert.deepEqual(split, before); validateSubtitleBindings(next);
});

test('display segmentation slices provenance and timing without changing the original binding', () => {
  const {document} = captions(), caption = find(document), before = structuredClone(document);
  const piece = sliceSubtitleBinding(document, caption, 20, 45)!;
  assert.deepEqual(piece.spans, [{clip_id: 'one', start_frame: 20, end_frame: 45}]);
  assert.deepEqual([piece.transcript_id, piece.transcript_revision, piece.segment_id], ['transcript', 4, 'reviewed-line']);
  piece.spans[0].start_frame = 25;
  assert.deepEqual(document, before);
  assert.throws(() => sliceSubtitleBinding(document, caption, 0, 45), /超出原字幕/);
  assert.equal(sliceSubtitleBinding(document, find(document, 'independent'), 250, 260), undefined);
});

test('manual retiming remains inside explicit owner coverage and preserves existing review flags', () => {
  const {document} = captions(), before = structuredClone(document);
  document.subtitles[0].binding!.review_required = true;
  const next = retimeSubtitle(document, 'first', 5, 95);
  assert.deepEqual(find(next).binding!.spans, [{clip_id: 'one', start_frame: 5, end_frame: 95}]);
  assert.equal(find(next).binding!.review_required, true);
  assert.throws(() => retimeSubtitle(document, 'first', 90, 110), /解除跟随/);
  assert.equal(document.subtitles[0].start_frame, before.subtitles[0].start_frame);
  const legacy = retimeSubtitle(document, 'independent', 1, 200);
  assert.deepEqual([find(legacy, 'independent').start_frame, find(legacy, 'independent').end_frame], [1, 200]);
  assert.equal(find(legacy, 'independent').binding, undefined);
});

test('source replacement or an unexplained range/clock change cannot keep a stale binding', () => {
  const {document} = captions(), before = structuredClone(document);
  for (const patch of [
    {asset_id: 'other'}, {version: 3}, {stream_index: 1}, {time_base: {num: 1, den: 1000}},
    {in_pts: (BigInt(document.clips[0].source.in_pts) + 1n).toString()},
  ]) {
    const after = structuredClone(document); Object.assign(after.clips[0].source, patch);
    assert.throws(() => reconcileSubtitleBindings(document, after), /关联|源区间/);
  }
  const after = structuredClone(document); after.clips[0].source.in_pts = (BigInt(after.clips[0].source.in_pts) + 1n).toString();
  assert.throws(() => reconcileSubtitleBindings(document, after, {trim: {clipId: 'one', shiftFrames: 5}}), /偏移不一致/);
  assert.deepEqual(document, before);
});

test('binding validation rejects missing owners, repeated spans, bad provenance and mismatched projection', () => {
  const {document} = captions();
  const cases = [
    (doc: ProjectDocument) => {find(doc).binding!.spans[0].clip_id = 'missing';},
    (doc: ProjectDocument) => {find(doc).binding!.spans.push({...find(doc).binding!.spans[0]});},
    (doc: ProjectDocument) => {find(doc).binding!.spans[0].end_frame = 101;},
    (doc: ProjectDocument) => {find(doc).binding!.spans[0].start_frame = 21;},
    (doc: ProjectDocument) => {delete find(doc).binding!.segment_id;},
    (doc: ProjectDocument) => {find(doc).binding!.asset_version = 3;},
  ];
  for (const mutate of cases) {
    const invalid = structuredClone(document); mutate(invalid);
    assert.throws(() => validateSubtitleBindings(invalid), /字幕关联未应用/);
  }
  const repaired = structuredClone(document); find(repaired).binding!.spans[0].clip_id = 'missing';
  validateSubtitleBindings(detachSubtitleBinding(repaired, 'first'));
});

test('bulk detaching a clip preserves its captions and leaves other instances associated', () => {
  const {document} = captions(), before = structuredClone(document), next = detachClipSubtitleBindings(document, 'one');
  assert.equal(find(next).binding, undefined);
  assert.deepEqual(find(next, 'second'), find(document, 'second'));
  assert.equal(find(next).text, find(document).text);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [20, 80]);
  assert.deepEqual(document, before);
});

test('high absolute PTS and NTSC trims round-trip bindings through JSON without precision loss', () => {
  const {document, asset} = captions({num: 30000, den: 1001}), before = structuredClone(document);
  const next = trimClip(document, [asset], 'one', 'in', 17);
  assert.deepEqual([find(next).start_frame, find(next).end_frame], [3, 63]);
  assert.equal(next.clips[0].source.in_pts, (BigInt(document.clips[0].source.in_pts) + 51051n).toString());
  const reopened: ProjectDocument = JSON.parse(JSON.stringify(next));
  validateSubtitleBindings(reopened); assert.deepEqual(reopened, next); assert.deepEqual(document, before);
});

test('real action-core move, trim and split routes preserve the same binding contract', async () => {
  const {document, asset} = captions();
  const movedCore = await runCoreAction(document, [asset], 'clip/move', {clipId: 'one', startTime: 8});
  assert.deepEqual([find(movedCore).start_frame, find(movedCore).end_frame], [220, 280]);
  const trimmed = await runCoreAction(document, [asset], 'clip/trim', {clipId: 'one', inPoint: 4, outPoint: 6});
  validateSubtitleBindings(trimmed);
  assert.ok(find(trimmed).end_frame <= trimmed.clips.find(clip => clip.id === 'one')!.timeline_start_frame + trimmed.clips.find(clip => clip.id === 'one')!.duration_frames);
  const split = await runCoreAction(document, [asset], 'clip/split', {clipId: 'one', time: 2});
  assert.equal(split.subtitles.length, document.subtitles.length);
  assert.equal(find(split).binding!.spans.length, 2);
});

test('unbound legacy documents remain byte-shape compatible through no-op reconciliation', () => {
  const {document} = fixture(); document.subtitles = [subtitle('legacy', 20, 80)];
  const before = structuredClone(document);
  const next = reconcileSubtitleBindings(document, structuredClone(document));
  assert.deepEqual(next, before); assert.notEqual(next, document);
  assert.ok(!Object.hasOwn(next.subtitles[0], 'binding'));
  validateSubtitleBindings(next);
});
