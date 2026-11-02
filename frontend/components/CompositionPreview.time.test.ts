import assert from 'node:assert/strict';
import test from 'node:test';
import type {Asset, Clip, ProjectDocument} from '../lib/types';
import {clipAtFrame, clipGain, enabledClips, previewClockTick, ptsSeconds, sourceTime} from './CompositionPreview.time';

const clip: Clip = {id: 'c1', track_id: 'v1', source: {asset_id: 'a1', version: 1, stream_index: 0,
  time_base: {num: 1, den: 1000}, in_pts: '3000', out_pts: '5000'}, timeline_start_frame: 50,
  duration_frames: 50, gain_db: 0, muted: false, fade_in_frames: 0, fade_out_frames: 0,
  fit: 'contain', include_source_audio: true};
const asset: Asset = {id: 'a1', version: 1, sha256: 'fixture', filename: 'delayed.mp4', media_url: '/fixture',
  metadata: {start_seconds: 2, video_stream_index: 0, audio_stream_index: 1, streams: [
    {index: 0, type: 'video', time_base: {num: 1, den: 1000}, start_pts: '2000', end_pts: '6000'},
    {index: 1, type: 'audio', time_base: {num: 1, den: 1000}, start_pts: '2300', end_pts: '6000'},
  ]}};

test('source seek preserves the common media origin and the trimmed video offset', () => {
  assert.equal(sourceTime(clip, asset, 50, {num: 25, den: 1}), 1);
  assert.equal(sourceTime(clip, asset, 75, {num: 25, den: 1}), 2);
});

test('selecting a delayed audio stream retains its 300 ms delay', () => {
  const audio = {...clip, source: {...clip.source, stream_index: 1, in_pts: '2300'}};
  assert.ok(Math.abs(sourceTime(audio, asset, 50, {num: 25, den: 1}) - 0.3) < 1e-12);
});

test('fractional FPS and negative PTS are converted without an average-fps assumption', () => {
  assert.equal(ptsSeconds('-24', {num: 1, den: 48}), -0.5);
  assert.equal(sourceTime(clip, asset, 80, {num: 30000, den: 1001}), 2.001);
});

test('a hard cut uses half-open frame intervals and selects the next source', () => {
  const second = {...clip, id: 'c2', timeline_start_frame: 100};
  assert.equal(clipAtFrame([clip, second], 99)?.id, 'c1');
  assert.equal(clipAtFrame([clip, second], 100)?.id, 'c2');
  assert.equal(clipAtFrame([clip, second], 150), undefined);
});

test('source mute, positive dB and fade envelopes share the project frame clock', () => {
  const fading = {...clip, gain_db: 6, fade_in_frames: 10, fade_out_frames: 10};
  assert.equal(clipGain(fading, 50), 0);
  assert.ok(Math.abs(clipGain(fading, 55) - Math.pow(10, 6 / 20) * 0.5) < 1e-12);
  assert.ok(Math.abs(clipGain(fading, 95) - Math.pow(10, 6 / 20) * 0.5) < 1e-12);
  assert.equal(clipGain({...fading, muted: true}, 70), 0);
  assert.equal(clipGain(fading, 100), 0);
});

test('disabled tracks and audio clips never become the video preview source', () => {
  const document: ProjectDocument = {schema_version: 1, project_id: 'p1', name: 'fixture',
    canvas: {width: 640, height: 360}, fps: {num: 25, den: 1}, sample_rate: 48000,
    duration_frames: 150, tracks: [{id: 'v1', kind: 'video', enabled: false},
      {id: 'a1', kind: 'audio', enabled: true}], clips: [clip, {...clip, id: 'audio', track_id: 'a1'}], subtitles: []};
  assert.deepEqual(enabledClips(document, 'video'), []);
  assert.equal(enabledClips(document, 'audio')[0]?.id, 'audio');
});

test('the first rAF timestamp preceding performance.now never emits a negative frame', () => {
  assert.deepEqual(previewClockTick(0, 100.8, 100.1, 25, 150), {frame: 0, elapsed: 0, ended: false});
  assert.deepEqual(previewClockTick(40, 100.8, 100.1, 25, 150), {frame: 40, elapsed: 0, ended: false});
});

test('playback advances at frame boundaries and clamps a delayed tick to the final frame', () => {
  assert.equal(previewClockTick(0, 100, 139.9, 25, 150).frame, 0);
  assert.equal(previewClockTick(0, 100, 140, 25, 150).frame, 1);
  assert.deepEqual(previewClockTick(0, 100, 12100, 25, 150), {frame: 149, elapsed: 12000, ended: true});
});

test('fractional frame rate keeps its clock and a one-frame project ends after one frame duration', () => {
  assert.equal(previewClockTick(0, 0, 1000, 30000 / 1001, 150).frame, 29);
  assert.equal(previewClockTick(0, 0, 1001, 30000 / 1001, 150).frame, 30);
  assert.deepEqual(previewClockTick(0, 100, 139, 25, 1), {frame: 0, elapsed: 39, ended: false});
  assert.deepEqual(previewClockTick(0, 100, 140, 25, 1), {frame: 0, elapsed: 40, ended: true});
});
