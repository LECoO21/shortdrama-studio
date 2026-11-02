import {test} from 'node:test';
import assert from 'node:assert/strict';
import {frameToPts} from '../lib/editor';
import {appendAsset, formatSeconds, formatSourceTime, formatTime, removeClip, reorderVideo, secondsToFrames, splitSelection, trimClip} from '../lib/timeline-editing';
import type {Asset, Clip, ProjectDocument, Rational} from '../lib/types';

const fps: Rational = {num: 30000, den: 1001};
const tb: Rational = {num: 1, den: 90000};
const first = 9007199254740995n;
const asset: Asset = {
  id: 'source', version: 2, sha256: 'immutable-original', filename: 'take.mp4', media_url: '/api/v1/assets/source/media',
  metadata: {video_stream_index: 3, audio_stream_index: 5, duration_seconds: 999, average_fps: 1,
    streams: [
      {index: 3, type: 'video', time_base: tb, start_pts: first.toString(), end_pts: (first + frameToPts(600, fps, tb)).toString()},
      {index: 5, type: 'audio', time_base: {num: 1, den: 48000}, start_pts: '48000', end_pts: '1008960'},
    ]},
};

function mediaClip(id: string, start: number, duration = 100, sourceOffset = 0): Clip {
  return {id, track_id: 'v', source: {asset_id: asset.id, version: asset.version, stream_index: 3, time_base: {...tb},
    in_pts: (first + frameToPts(sourceOffset, fps, tb)).toString(),
    out_pts: (first + frameToPts(sourceOffset + duration, fps, tb)).toString()},
  timeline_start_frame: start, duration_frames: duration, gain_db: -6, muted: true,
  fade_in_frames: 0, fade_out_frames: 0, fit: 'cover', include_source_audio: true};
}

function document(): ProjectDocument {
  return {schema_version: 1, project_id: 'project', name: 'test', canvas: {width: 1280, height: 720}, fps: {...fps},
    sample_rate: 48000, duration_frames: 300,
    tracks: [{id: 'v', kind: 'video', enabled: true}, {id: 'a', kind: 'audio', enabled: true}, {id: 's', kind: 'subtitle', enabled: true}],
    clips: [mediaClip('one', 0), mediaClip('two', 100, 100, 100), mediaClip('three', 200, 100, 200)],
    subtitles: []};
}

function subtitle(id: string, start_frame: number, end_frame: number) {
  return {id, track_id: 's', text: id, start_frame, end_frame, font_size: 28, color: '#FFFFFF', bottom_percent: 8};
}

const audioTb = {num: 1, den: 48000};
const audioAsset: Asset = {
  id: 'music-source', version: 3, sha256: 'immutable-audio', filename: 'music.wav', media_url: '/api/v1/assets/music-source/media',
  metadata: {audio_stream_index: 7, streams: [{index: 7, type: 'audio', time_base: audioTb,
    start_pts: first.toString(), end_pts: (first + frameToPts(500, fps, audioTb)).toString()}]},
};

function withAudio() {
  const original = document();
  original.clips.push({...mediaClip('music', 20, 180), track_id: 'a', gain_db: -9, muted: false,
    fade_in_frames: 4, fade_out_frames: 7, include_source_audio: false,
    source: {asset_id: audioAsset.id, version: audioAsset.version, stream_index: 7, time_base: {...audioTb},
      in_pts: (first + frameToPts(11, fps, audioTb)).toString(), out_pts: (first + frameToPts(191, fps, audioTb)).toString()}});
  original.subtitles = [subtitle('caption', 25, 95)];
  return original;
}

test('selected subtitle splits its full text and style without splitting overlapping media', async () => {
  const original = withAudio(), before = structuredClone(original), assetsBefore = structuredClone([asset, audioAsset]);
  const {document: next, selection} = await splitSelection(original, [asset, audioAsset], {kind: 'subtitle', id: 'caption'}, 60);
  assert.deepEqual(next.clips, original.clips);
  assert.deepEqual(selection, {kind: 'subtitle', id: 'caption'});
  assert.deepEqual(next.subtitles[0], {...original.subtitles[0], end_frame: 60});
  assert.notEqual(next.subtitles[1].id, 'caption');
  assert.match(next.subtitles[1].id, /^[\da-f-]{36}$/i);
  assert.deepEqual(next.subtitles[1], {...original.subtitles[0], id: next.subtitles[1].id, start_frame: 60});
  assert.equal(next.duration_frames, original.duration_frames);
  assert.deepEqual(original, before);
  assert.deepEqual([asset, audioAsset], assetsBefore);
});

test('subtitle-only project can split and reopen without a media stream', async () => {
  const original = document(); original.clips = []; original.subtitles = [subtitle('words', 0, 300)];
  const firstSplit = await splitSelection(original, [], {kind: 'subtitle', id: 'words'}, 100);
  const reopened: ProjectDocument = JSON.parse(JSON.stringify(firstSplit.document));
  const secondSplit = await splitSelection(reopened, [], {kind: 'subtitle', id: reopened.subtitles[1].id}, 200);
  assert.deepEqual(secondSplit.document.subtitles.map(sub => [sub.start_frame, sub.end_frame]), [[0, 100], [100, 200], [200, 300]]);
  assert.equal(new Set(secondSplit.document.subtitles.map(sub => sub.id)).size, 3);
});

test('selected independent audio uses real upstream split and preserves stream PTS, gain and outer fades', async () => {
  const original = withAudio(), before = structuredClone(original), audio = original.clips.at(-1)!;
  const {document: next, selection} = await splitSelection(original, [asset, audioAsset], {kind: 'clip', id: 'music'}, 80);
  const halves = next.clips.filter(clip => clip.track_id === 'a');
  assert.deepEqual(selection, {kind: 'clip', id: 'music'});
  assert.equal(halves.length, 2);
  assert.equal(halves[0].id, 'music');
  assert.notEqual(halves[1].id, 'music');
  assert.deepEqual(halves.map(clip => [clip.timeline_start_frame, clip.duration_frames]), [[20, 60], [80, 120]]);
  assert.deepEqual(halves.map(clip => [clip.gain_db, clip.muted, clip.fade_in_frames, clip.fade_out_frames]), [[-9, false, 4, 0], [-9, false, 0, 7]]);
  assert.equal(halves[0].source.in_pts, audio.source.in_pts);
  assert.equal(halves[1].source.out_pts, audio.source.out_pts);
  assert.equal(halves[0].source.out_pts, halves[1].source.in_pts);
  assert.equal(BigInt(halves[1].source.in_pts) - BigInt(audio.source.in_pts), frameToPts(60, fps, audioTb));
  for (const half of halves) {
    assert.deepEqual({...half.source, in_pts: audio.source.in_pts, out_pts: audio.source.out_pts}, audio.source);
    assert.equal(half.include_source_audio, false);
  }
  assert.deepEqual(next.clips.filter(clip => clip.track_id === 'v'), original.clips.filter(clip => clip.track_id === 'v'));
  assert.deepEqual(next.subtitles, original.subtitles);
  assert.equal(next.duration_frames, original.duration_frames);
  assert.deepEqual(original, before);
});

test('explicit video selection splits only its target', async () => {
  const original = withAudio();
  const {document: next, selection: selected} = await splitSelection(original, [asset, audioAsset], {kind: 'clip', id: 'one'}, 50);
  assert.deepEqual(selected, {kind: 'clip', id: 'one'});
  assert.equal(next.clips.filter(clip => clip.track_id === 'v').length, 4);
  assert.deepEqual(next.clips.filter(clip => clip.track_id === 'a'), original.clips.filter(clip => clip.track_id === 'a'));
  assert.deepEqual(next.subtitles, original.subtitles);
  const left = next.clips.find(clip => clip.id === 'one')!;
  const right = next.clips.find(clip => clip.track_id === 'v' && clip.timeline_start_frame === 50)!;
  assert.equal(left.duration_frames, 50);
  assert.equal(right.duration_frames, 50);
  assert.equal(left.source.out_pts, right.source.in_pts);
});

test('no selection refuses to split even where video, audio and subtitle overlap', async () => {
  const original = withAudio(), before = structuredClone(original);
  await assert.rejects(() => splitSelection(original, [asset, audioAsset], null, 60), /请先选中时间线/);
  assert.deepEqual(original, before);
});

test('invalid explicit selection and cuts outside selected ranges never fall back to the overlapping video', async () => {
  const original = withAudio(), before = structuredClone(original);
  await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind: 'asset', id: asset.id}, 60), /选中时间线/);
  for (const kind of ['clip', 'subtitle'] as const) {
    await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind, id: 'missing'}, 60), /已不存在/);
  }
  for (const frame of [20, 200, 250]) {
    await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind: 'clip', id: 'music'}, frame), /选中音频片段的内部/);
  }
  for (const frame of [0, 100, 150]) {
    await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind: 'clip', id: 'one'}, frame), /选中视频片段的内部/);
  }
  for (const frame of [25, 95, 150]) {
    await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind: 'subtitle', id: 'caption'}, frame), /选中字幕的内部/);
  }
  await assert.rejects(() => splitSelection(original, [asset, audioAsset], null, 100), /先选中/);
  await assert.rejects(() => splitSelection(original, [asset, audioAsset], null, 1.5), /整数帧/);
  await assert.rejects(() => splitSelection(original, [asset, audioAsset], null, -1), /工程范围/);
  assert.deepEqual(original, before);
});

test('selected audio refuses a cut through its fade without editing any other track', async () => {
  const original = withAudio(), before = structuredClone(original);
  for (const frame of [21, 199]) {
    await assert.rejects(() => splitSelection(original, [asset, audioAsset], {kind: 'clip', id: 'music'}, frame), /淡入或淡出区间/);
  }
  assert.deepEqual(original, before);
});

test('append uses real high PTS and rational clock, not nominal duration/fps; inputs stay immutable', () => {
  const original = document(), before = structuredClone(original), assetBefore = structuredClone(asset);
  const result = appendAsset(original, asset), clip = result.clips.at(-1)!;
  assert.equal(clip.timeline_start_frame, 300);
  assert.equal(clip.duration_frames, 600);
  assert.equal(result.duration_frames, 900);
  assert.deepEqual(clip.source, {asset_id: 'source', version: 2, stream_index: 3, time_base: tb,
    in_pts: first.toString(), out_pts: (first + frameToPts(600, fps, tb)).toString()});
  assert.equal(clip.include_source_audio, true);
  assert.deepEqual(original, before);
  assert.deepEqual(asset, assetBefore);
  assert.notEqual(clip.source.time_base, asset.metadata.streams[0].time_base);
  const twice = appendAsset(result, asset);
  assert.notEqual(clip.id, twice.clips.at(-1)!.id);
});

test('append creates the matching audio track and appends only the real audio stream', () => {
  const original = document();
  original.tracks = original.tracks.filter(track => track.kind !== 'audio');
  const audio = structuredClone(asset);
  delete audio.metadata.video_stream_index;
  audio.metadata.streams = audio.metadata.streams.filter(stream => stream.type === 'audio');
  const next = appendAsset(original, audio), clip = next.clips.at(-1)!;
  assert.equal(next.tracks.find(track => track.id === clip.track_id)?.kind, 'audio');
  assert.equal(clip.source.stream_index, 5);
  assert.equal(clip.source.in_pts, '48000');
  assert.equal(clip.include_source_audio, false);
  assert.equal(clip.timeline_start_frame, 0);
  assert.equal(clip.duration_frames, 600);
  assert.equal(next.duration_frames, 600);
  assert.equal(appendAsset(next, audio).clips.at(-1)!.timeline_start_frame, 600);
});

test('VFR-like whole source range is half-up rounded while actual endpoints are retained', () => {
  const original = document();
  original.fps = {num: 25, den: 1};
  original.clips = [];
  original.duration_frames = 1;
  const irregular = structuredClone(asset);
  irregular.metadata.streams[0] = {index: 3, type: 'video', time_base: {num: 1, den: 1000}, start_pts: '-17', end_pts: '4003'};
  irregular.metadata.frame_pts = ['-17', '20', '78', '105'];
  const next = appendAsset(original, irregular);
  assert.equal(next.clips[0].duration_frames, 101); // 4.020 s × 25 = 100.5 frames.
  assert.equal(next.clips[0].source.in_pts, '-17');
  assert.equal(next.clips[0].source.out_pts, '4003');
  assert.equal(next.duration_frames, 101);
});

test('invalid, sub-frame and oversized sources are rejected before changing the document', () => {
  const original = document(), before = structuredClone(original);
  const bad = structuredClone(asset);
  bad.metadata.streams[0].end_pts = first.toString();
  assert.throws(() => appendAsset(original, bad), /有效时长/);
  bad.metadata.streams[0].end_pts = (first + 1n).toString();
  assert.throws(() => appendAsset(original, bad), /不足一个工程帧/);
  bad.metadata.streams[0].end_pts = (first + frameToPts(20_000, fps, tb)).toString();
  assert.throws(() => appendAsset(original, bad), /10 分钟/);
  bad.metadata.streams[0].start_pts = 'NaN';
  assert.throws(() => appendAsset(original, bad), /时间码/);
  assert.deepEqual(original, before);
});

test('video reorder packs from zero without changing sources or independent caption/audio times', () => {
  const original = document();
  original.subtitles = [subtitle('caption', 25, 75)];
  const audio = mediaClip('music', 250, 25); audio.track_id = 'a'; original.clips.push(audio);
  const before = structuredClone(original);
  const next = reorderVideo(original, 'one', 299);
  assert.deepEqual(next.clips.filter(clip => clip.track_id === 'v').sort((a, b) => a.timeline_start_frame - b.timeline_start_frame).map(clip => [clip.id, clip.timeline_start_frame]), [['two', 0], ['three', 100], ['one', 200]]);
  for (const clip of original.clips) {
    const edited = next.clips.find(value => value.id === clip.id)!;
    assert.deepEqual({...edited, timeline_start_frame: clip.timeline_start_frame}, clip);
  }
  assert.deepEqual(next.subtitles, original.subtitles);
  assert.equal(next.clips.find(clip => clip.id === 'music')!.timeline_start_frame, 250);
  assert.equal(next.duration_frames, 300);
  assert.deepEqual(original, before);
  const back = reorderVideo(next, 'one', 0);
  assert.equal(back.clips.find(clip => clip.id === 'one')!.timeline_start_frame, 0);
  assert.throws(() => reorderVideo(original, 'music', 0), /只适用于视频/);
});

test('ripple video removal maps subtitle boundaries and shifts later audio source-identically', () => {
  const original = document();
  original.subtitles = [subtitle('before', 10, 40), subtitle('deleted', 110, 150), subtitle('crossing', 80, 230),
    subtitle('after', 220, 260), subtitle('touching', 200, 210)];
  const audio = mediaClip('music', 250, 25); audio.track_id = 'a'; original.clips.push(audio);
  const before = structuredClone(original), next = removeClip(original, 'two');
  assert.equal(next.duration_frames, 200);
  assert.deepEqual(next.clips.filter(clip => clip.track_id === 'v').map(clip => [clip.id, clip.timeline_start_frame]), [['one', 0], ['three', 100]]);
  assert.deepEqual(next.subtitles.map(sub => [sub.id, sub.start_frame, sub.end_frame]), [['before', 10, 40], ['crossing', 80, 130], ['after', 120, 160], ['touching', 100, 110]]);
  assert.deepEqual(next.clips.find(clip => clip.id === 'music'), {...audio, timeline_start_frame: 150});
  assert.deepEqual(original, before);
});

test('ripple refuses intersecting independent audio and malformed video gaps atomically', () => {
  const original = document(), audio = mediaClip('music', 80, 150); audio.track_id = 'a'; original.clips.push(audio);
  const before = structuredClone(original);
  assert.throws(() => removeClip(original, 'two'), /独立音频/);
  assert.throws(() => trimClip(original, [asset], 'two', 'out', -10), /独立音频/);
  assert.deepEqual(original, before);
  const gap = document(); gap.clips[1].timeline_start_frame += 2;
  assert.throws(() => removeClip(gap, 'one'), /空洞或重叠/);
});

test('non-ripple removal and independent audio removal preserve the timeline and captions', () => {
  const original = document(); original.subtitles = [subtitle('caption', 100, 180)];
  const next = removeClip(original, 'two', false);
  assert.equal(next.duration_frames, 300);
  assert.equal(next.clips.find(clip => clip.id === 'three')!.timeline_start_frame, 200);
  assert.deepEqual(next.subtitles, original.subtitles);
  const audio = mediaClip('music', 50, 100); audio.track_id = 'a'; original.clips.push(audio);
  const withoutAudio = removeClip(original, 'music');
  assert.deepEqual(withoutAudio.clips, original.clips.slice(0, 3));
  assert.equal(withoutAudio.duration_frames, 300);
});

test('removing the final video leaves a legal empty one-frame draft, not negative duration', () => {
  const original = document(); original.clips = [mediaClip('one', 0)]; original.duration_frames = 100;
  original.subtitles = [subtitle('caption', 5, 90)];
  const next = removeClip(original, 'one');
  assert.equal(next.duration_frames, 1);
  assert.deepEqual(next.clips, []);
  assert.deepEqual(next.subtitles, []);
});

test('NTSC ripple in/out trim keeps exact untouched PTS and adjusts downstream time once', () => {
  const original = document(); original.subtitles = [subtitle('head', 0, 20), subtitle('cross', 5, 150), subtitle('later', 200, 250)];
  const before = structuredClone(original);
  const next = trimClip(original, [asset], 'one', 'in', 10);
  assert.equal(next.duration_frames, 290);
  assert.equal(next.clips[0].timeline_start_frame, 0);
  assert.equal(next.clips[0].duration_frames, 90);
  assert.equal(next.clips[0].source.in_pts, (first + 30030n).toString());
  assert.equal(next.clips[0].source.out_pts, original.clips[0].source.out_pts);
  assert.deepEqual(next.clips.slice(1).map(clip => clip.timeline_start_frame), [90, 190]);
  assert.deepEqual(next.subtitles.map(sub => [sub.start_frame, sub.end_frame]), [[0, 10], [0, 140], [190, 240]]);
  assert.deepEqual(original, before);
  const shorter = trimClip(original, [asset], 'two', 'out', -20);
  assert.equal(shorter.clips[1].source.in_pts, original.clips[1].source.in_pts);
  assert.equal(shorter.clips[1].source.out_pts, (BigInt(original.clips[1].source.out_pts) - 60060n).toString());
  assert.equal(shorter.clips[2].timeline_start_frame, 180);
  assert.equal(shorter.duration_frames, 280);
});

test('extension inserts timeline time and preserves offset VFR source phase', () => {
  const original = document();
  const phase = 33n;
  for (const clip of original.clips) {
    clip.source.in_pts = (BigInt(clip.source.in_pts) + phase).toString();
    clip.source.out_pts = (BigInt(clip.source.out_pts) + phase).toString();
  }
  original.subtitles = [subtitle('at-boundary', 100, 120), subtitle('before', 80, 100), subtitle('cross', 90, 110)];
  const next = trimClip(original, [asset], 'two', 'in', -10);
  assert.equal(next.clips[1].source.in_pts, (BigInt(original.clips[1].source.in_pts) - 30030n).toString());
  assert.equal(next.clips[1].source.out_pts, original.clips[1].source.out_pts);
  assert.equal(next.clips[1].timeline_start_frame, 100);
  assert.equal(next.clips[2].timeline_start_frame, 210);
  assert.equal(next.duration_frames, 310);
  assert.deepEqual(next.subtitles.map(sub => [sub.start_frame, sub.end_frame]), [[110, 130], [80, 100], [90, 120]]);
  const extendedOut = trimClip(original, [asset], 'one', 'out', 10);
  assert.equal(extendedOut.clips[0].source.out_pts, (BigInt(original.clips[0].source.out_pts) + 30030n).toString());
  assert.equal(extendedOut.clips[1].timeline_start_frame, 110);
});

test('trim rejects source overflow, total removal, incompatible clocks and fade truncation', () => {
  const original = document(), before = structuredClone(original);
  assert.throws(() => trimClip(original, [asset], 'one', 'in', -1), /源素材范围/);
  assert.throws(() => trimClip(original, [asset], 'one', 'out', -100), /至少需要保留一帧/);
  assert.throws(() => trimClip(original, [asset], 'three', 'out', 500), /源素材范围/);
  const clock = structuredClone(asset); clock.metadata.streams[0].time_base.den = 1000;
  assert.throws(() => trimClip(original, [clock], 'one', 'out', -1), /时间基/);
  assert.deepEqual(original, before);
  original.clips[0].fade_out_frames = 10;
  assert.throws(() => trimClip(original, [asset], 'one', 'out', -1), /淡入或淡出/);
  assert.throws(() => trimClip(original, [asset], 'one', 'in', 95), /截断淡入淡出/);
  const next = trimClip(original, [asset], 'one', 'in', 10);
  assert.equal(next.clips[0].fade_out_frames, 10);
});

test('source tick rounding is relative to current PTS, without rounding to average VFR frames', () => {
  const original = document(); original.fps = {num: 25, den: 1};
  original.clips = [mediaClip('one', 0, 100)]; original.duration_frames = 100;
  original.clips[0].source = {asset_id: 'vfr', version: 1, stream_index: 0, time_base: {num: 1, den: 1001}, in_pts: '123', out_pts: '4127'};
  const vfr: Asset = {id: 'vfr', version: 1, sha256: 'vfrhash', filename: 'vfr.mov', media_url: '/source',
    metadata: {video_stream_index: 0, streams: [{index: 0, type: 'video', time_base: {num: 1, den: 1001}, start_pts: '0', end_pts: '8008'}]}};
  const next = trimClip(original, [vfr], 'one', 'in', 1);
  assert.equal(next.clips[0].source.in_pts, '163');
  assert.equal(next.clips[0].source.out_pts, '4127');
  assert.equal(next.clips[0].duration_frames, 99);
});

test('audio-only trim is local and does not ripple captions or the video storyline', () => {
  const original = document(); original.subtitles = [subtitle('caption', 50, 100)];
  const audio = mediaClip('music', 20, 100, 20); audio.track_id = 'a'; original.clips.push(audio);
  // Use the actual video asset stream here as an audio source fixture.
  const audioAsset = structuredClone(asset); audioAsset.metadata.streams[0].type = 'audio';
  const next = trimClip(original, [audioAsset], 'music', 'in', 10);
  assert.equal(next.clips.at(-1)!.timeline_start_frame, 30);
  assert.equal(next.clips.at(-1)!.duration_frames, 90);
  assert.deepEqual(next.subtitles, original.subtitles);
  assert.deepEqual(next.clips.slice(0, 3), original.clips.slice(0, 3));
  assert.equal(next.duration_frames, 300);
});

test('time fields use seconds with correct minute rollover and NTSC frame snapping', () => {
  assert.equal(formatTime(0, fps), '00:00.00');
  assert.equal(formatTime(1800, fps), '01:00.06');
  assert.equal(formatTime(1500, {num: 25, den: 1}), '01:00.00');
  assert.equal(formatSeconds(100, fps), '3.34');
  assert.equal(secondsToFrames(1.001, fps), 30);
  assert.equal(secondsToFrames(0.02, {num: 25, den: 1}), 1);
  assert.throws(() => secondsToFrames(-1, fps), /非负秒数/);
  assert.throws(() => formatTime(Number.NaN, fps), /整数帧/);
});


test('source clock preserves sub-second PTS above the JS safe-integer boundary', () => {
  assert.equal(formatSourceTime('9007199254740993', {num: 1, den: 1000}), '150119987579:00.993');
  assert.equal(formatSourceTime('599999', {num: 1, den: 10000}), '01:00.000');
});

test('source clock handles signed and rational stream clocks without changing the source', () => {
  assert.equal(formatSourceTime('-90001', {num: 1, den: 90000}), '-00:01.000');
  assert.equal(formatSourceTime('1800', {num: 1001, den: 30000}), '01:00.060');
  assert.equal(formatSourceTime('-1', {num: 1, den: 48000}), '00:00.000');
  assert.throws(() => formatSourceTime('1', {num: 1, den: 0}));
});
