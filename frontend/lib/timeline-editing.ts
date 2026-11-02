import {frameToPts, ptsFrame, runCoreAction, seconds} from './editor';
import {reconcileSubtitleBindings, sliceSubtitleBinding, validateSubtitleBindings} from './subtitle-binding';
import type {Asset, Clip, MediaStream, ProjectDocument, Rational, Subtitle} from './types';

const clone = <T>(value: T): T => structuredClone(value);
const end = (clip: Clip) => clip.timeline_start_frame + clip.duration_frames;

export type TimelineSelection = {kind: 'clip' | 'subtitle' | 'asset'; id: string} | null;

function integer(value: number, label: string) {
  if (!Number.isSafeInteger(value)) throw new Error(`${label}必须是整数帧`);
}

function ratio(value: Rational) {
  if (!Number.isSafeInteger(value.num) || !Number.isSafeInteger(value.den) || value.num <= 0 || value.den <= 0) {
    throw new Error('媒体时间基或工程帧率无效');
  }
}

function ticks(value: string): bigint {
  if (!/^-?\d{1,20}$/.test(value)) throw new Error('素材时间码无效');
  return BigInt(value);
}

function trackKind(document: ProjectDocument, clip: Clip) {
  const kind = document.tracks.find(track => track.id === clip.track_id)?.kind;
  if (kind !== 'video' && kind !== 'audio') throw new Error('片段所在媒体轨道不存在');
  return kind;
}

function selected(document: ProjectDocument, clipId: string) {
  const clip = document.clips.find(value => value.id === clipId);
  if (!clip) throw new Error('没有找到要编辑的片段');
  return clip;
}

function videos(document: ProjectDocument) {
  return document.clips.filter(clip => trackKind(document, clip) === 'video')
    .sort((a, b) => a.timeline_start_frame - b.timeline_start_frame);
}

function requirePackedVideo(document: ProjectDocument) {
  let cursor = 0;
  for (const clip of videos(document)) {
    if (clip.timeline_start_frame !== cursor) {
      throw new Error('视频轨有空洞或重叠，请先修复后再使用波纹编辑');
    }
    cursor = end(clip);
  }
}

function validateResult(document: ProjectDocument) {
  ratio(document.fps);
  integer(document.duration_frames, '工程时长');
  if (document.duration_frames < 1 || document.duration_frames > 108_000 || seconds(document.duration_frames, document.fps) > 600) {
    throw new Error('本地工程最长 10 分钟，且不可超过 108000 帧');
  }
  if (document.clips.length > 200) throw new Error('本阶段每个工程最多支持 200 个媒体片段');
  for (const clip of document.clips) {
    integer(clip.timeline_start_frame, '片段起点');
    integer(clip.duration_frames, '片段长度');
    if (clip.timeline_start_frame < 0 || clip.duration_frames < 1 || end(clip) > document.duration_frames) {
      throw new Error('片段超出工程范围');
    }
    if (clip.fade_in_frames < 0 || clip.fade_out_frames < 0 || clip.fade_in_frames + clip.fade_out_frames > clip.duration_frames) {
      throw new Error('裁剪后无法保留当前淡入淡出，请先调整声音淡化');
    }
  }
  for (const sub of document.subtitles) {
    if (sub.start_frame < 0 || sub.start_frame >= sub.end_frame || sub.end_frame > document.duration_frames) {
      throw new Error('字幕时间超出工程范围');
    }
  }
  validateSubtitleBindings(document);
  return document;
}

function streamFor(clip: Clip, assets: Asset[], document: ProjectDocument): MediaStream {
  const asset = assets.find(value => value.id === clip.source.asset_id && value.version === clip.source.version);
  const stream = asset?.metadata.streams.find(value => value.index === clip.source.stream_index);
  if (!stream || stream.type !== trackKind(document, clip)) throw new Error('素材版本或对应媒体流不可用');
  ratio(stream.time_base);
  if (stream.time_base.num !== clip.source.time_base.num || stream.time_base.den !== clip.source.time_base.den) {
    throw new Error('素材时间基与工程引用不一致');
  }
  return stream;
}

function validateSource(clip: Clip, stream: MediaStream, fps: Rational) {
  const first = ticks(clip.source.in_pts), last = ticks(clip.source.out_pts);
  if (first < ticks(stream.start_pts) || last > ticks(stream.end_pts) || last <= first) {
    throw new Error('裁剪位置超出源素材范围');
  }
  // Match the server's half-frame + one source tick tolerance without converting
  // large absolute PTS to floating point. VFR in-points keep their original phase.
  const clock = BigInt(stream.time_base.num) * BigInt(fps.num);
  const denominator = BigInt(stream.time_base.den) * BigInt(fps.den);
  const difference = (last - first) * clock - BigInt(clip.duration_frames) * denominator;
  const absolute = difference < 0n ? -difference : difference;
  if (absolute * 2n > denominator + clock * 2n) throw new Error('源范围与片段时长不相容，请重新选择裁剪位置');
}

function uniqueId(prefix: string, existing: string[]) {
  const used = new Set(existing);
  let index = 1;
  while (used.has(`${prefix}-${index}`)) index += 1;
  return `${prefix}-${index}`;
}

/** Append the full real source range. Video assets use their video stream and
 * attached source audio; audio-only assets use the independent audio track.
 * Independent audio may extend the project and require further video coverage.
 */
export function appendAsset(document: ProjectDocument, asset: Asset): ProjectDocument {
  ratio(document.fps);
  const kind = asset.metadata.video_stream_index != null ? 'video' : 'audio';
  const streamIndex = kind === 'video' ? asset.metadata.video_stream_index : asset.metadata.audio_stream_index;
  const stream = asset.metadata.streams.find(value => value.index === streamIndex && value.type === kind);
  if (!stream) throw new Error('素材不包含可加入时间线的视频或音频流');
  ratio(stream.time_base);
  const first = ticks(stream.start_pts), last = ticks(stream.end_pts);
  if (last <= first) throw new Error('素材没有有效时长');
  const duration = ptsFrame((last - first).toString(), stream.time_base, document.fps);
  if (!Number.isSafeInteger(duration) || duration < 1) throw new Error('素材过短，不足一个工程帧');
  const next = clone(document);
  let track = next.tracks.find(value => value.kind === kind);
  if (!track) {
    if (next.tracks.length >= 3) throw new Error('本阶段每类轨道只支持一条');
    track = {id: uniqueId(`track-${kind}`, next.tracks.map(value => value.id)), kind, enabled: true};
    next.tracks.push(track);
  }
  const start = Math.max(0, ...next.clips.filter(value => value.track_id === track.id).map(end));
  const clip: Clip = {
    id: uniqueId(`clip-${asset.id.slice(0, 32)}`, next.clips.map(value => value.id)),
    track_id: track.id,
    source: {asset_id: asset.id, version: asset.version, stream_index: stream.index,
      time_base: clone(stream.time_base), in_pts: stream.start_pts, out_pts: stream.end_pts},
    timeline_start_frame: start, duration_frames: duration, gain_db: 0, muted: false,
    fade_in_frames: 0, fade_out_frames: 0, fit: 'contain', include_source_audio: kind === 'video',
  };
  validateSource(clip, stream, next.fps);
  next.clips.push(clip);
  // An otherwise empty document may contain a one-frame canvas placeholder.
  const previousLength = document.clips.length || document.subtitles.length ? document.duration_frames : 1;
  next.duration_frames = Math.max(previousLength, start + duration);
  return validateResult(next);
}

/** Drop at the target frame: the first half of a clip inserts before it, the
 * second half inserts after. Bound captions follow their explicit clip instances;
 * independent audio and unbound captions keep their absolute times.
 */
export function reorderVideo(document: ProjectDocument, clipId: string, targetFrame: number): ProjectDocument {
  integer(targetFrame, '放置位置');
  if (targetFrame < 0) throw new Error('放置位置不能早于工程起点');
  if (trackKind(document, selected(document, clipId)) !== 'video') throw new Error('重排只适用于视频片段');
  const next = clone(document);
  const ordered = videos(next);
  const moving = ordered.find(clip => clip.id === clipId)!;
  const remaining = ordered.filter(clip => clip.id !== clipId);
  let index = remaining.findIndex(clip => targetFrame < clip.timeline_start_frame + clip.duration_frames / 2);
  if (index < 0) index = remaining.length;
  remaining.splice(index, 0, moving);
  let cursor = 0;
  for (const clip of remaining) {
    clip.timeline_start_frame = cursor;
    cursor += clip.duration_frames;
  }
  const independentEnd = Math.max(0,
    ...next.clips.filter(clip => trackKind(next, clip) === 'audio').map(end),
    ...next.subtitles.map(sub => sub.end_frame));
  next.duration_frames = Math.max(1, cursor, independentEnd);
  return validateResult(reconcileSubtitleBindings(document, next));
}

function cutSubtitles(subtitles: Subtitle[], start: number, finish: number): Subtitle[] {
  const length = finish - start;
  const position = (frame: number) => frame <= start ? frame : frame >= finish ? frame - length : start;
  return subtitles.map(sub => ({...sub, start_frame: position(sub.start_frame), end_frame: position(sub.end_frame)}))
    .filter(sub => sub.start_frame < sub.end_frame);
}

/** Apply one global ripple interval to independent tracks. An intersecting audio
 * clip cannot be kept source-identical with this schema, so reject atomically.
 */
function ripple(document: ProjectDocument, start: number, finish: number, insertFrames = 0) {
  const deleting = finish > start;
  const delta = deleting ? start - finish : insertFrames;
  if (!delta) return;
  for (const clip of document.clips) {
    if (trackKind(document, clip) !== 'audio') continue;
    const intersects = deleting
      ? clip.timeline_start_frame < finish && end(clip) > start
      : clip.timeline_start_frame < start && end(clip) > start;
    if (intersects) throw new Error('此处有独立音频跨越波纹编辑范围；请先裁开或移开音频，避免音画错位');
    if (clip.timeline_start_frame >= (deleting ? finish : start)) clip.timeline_start_frame += delta;
  }
  document.subtitles = deleting
    ? document.subtitles.flatMap(sub => sub.binding ? [sub] : cutSubtitles([sub], start, finish))
    : document.subtitles.map(sub => sub.binding ? sub : ({
      ...sub,
      start_frame: sub.start_frame >= start ? sub.start_frame + delta : sub.start_frame,
      end_frame: sub.end_frame > start ? sub.end_frame + delta : sub.end_frame,
    }));
  document.duration_frames = Math.max(1, document.duration_frames + delta);
}

/** Video ripple removal closes its global interval, clamps crossing subtitles,
 * removes fully deleted subtitles, and shifts later independent audio intact.
 * Audio removal never changes the global video storyline.
 */
export function removeClip(document: ProjectDocument, clipId: string, useRipple = true): ProjectDocument {
  const original = selected(document, clipId);
  const kind = trackKind(document, original);
  if (useRipple && kind === 'video') requirePackedVideo(document);
  const next = clone(document);
  next.clips = next.clips.filter(clip => clip.id !== clipId);
  if (useRipple && kind === 'video') {
    const first = original.timeline_start_frame, last = end(original);
    ripple(next, first, last);
    for (const clip of next.clips) {
      if (trackKind(next, clip) === 'video' && clip.timeline_start_frame >= last) {
        clip.timeline_start_frame -= original.duration_frames;
      }
    }
  }
  return validateResult(reconcileSubtitleBindings(document, next));
}

/** deltaFrames > 0 moves the named source edge right. Video trims ripple all
 * later video, subtitle and non-intersecting audio times. Audio trims are local:
 * the in edge moves its timeline start, the out edge changes only its duration.
 * An existing fade on the edited edge must be removed first, because truncating
 * its volume curve cannot be represented faithfully by the current schema.
 */
export function trimClip(document: ProjectDocument, assets: Asset[], clipId: string, edge: 'in' | 'out', deltaFrames: number): ProjectDocument {
  integer(deltaFrames, '裁剪偏移');
  const original = selected(document, clipId);
  if (deltaFrames === 0) return clone(document);
  if (edge !== 'in' && edge !== 'out') throw new Error('请选择有效的裁剪边缘');
  const kind = trackKind(document, original);
  if (kind === 'video') requirePackedVideo(document);
  if ((edge === 'in' ? original.fade_in_frames : original.fade_out_frames) > 0) {
    throw new Error('裁剪边缘存在淡入或淡出，请先移除该淡化再裁剪');
  }
  const stream = streamFor(original, assets, document);
  const next = clone(document);
  const clip = selected(next, clipId);
  const durationDelta = edge === 'in' ? -deltaFrames : deltaFrames;
  clip.duration_frames += durationDelta;
  if (clip.duration_frames < 1) throw new Error('裁剪后至少需要保留一帧');
  if (clip.fade_in_frames + clip.fade_out_frames > clip.duration_frames) {
    throw new Error('裁剪会截断淡入淡出，请先调整声音淡化');
  }
  const ptsDelta = frameToPts(deltaFrames, document.fps, clip.source.time_base);
  if (edge === 'in') clip.source.in_pts = (ticks(clip.source.in_pts) + ptsDelta).toString();
  else clip.source.out_pts = (ticks(clip.source.out_pts) + ptsDelta).toString();
  validateSource(clip, stream, document.fps);
  if (kind === 'video') {
    const first = original.timeline_start_frame, last = end(original);
    if (durationDelta < 0) {
      ripple(next, edge === 'in' ? first : last + durationDelta, edge === 'in' ? first - durationDelta : last);
    } else {
      const insertion = edge === 'in' ? first : last;
      ripple(next, insertion, insertion, durationDelta);
    }
    for (const value of next.clips) {
      if (value.id !== clipId && trackKind(next, value) === 'video' && value.timeline_start_frame >= last) {
        value.timeline_start_frame += durationDelta;
      }
    }
  } else {
    if (edge === 'in') clip.timeline_start_frame += deltaFrames;
    if (clip.timeline_start_frame < 0) throw new Error('音频入点不能早于工程起点');
    for (const value of next.clips) {
      if (value.id !== clipId && value.track_id === clip.track_id && value.timeline_start_frame < end(clip) && end(value) > clip.timeline_start_frame) {
        throw new Error('裁剪会使同轨音频重叠，请先移开相邻片段');
      }
    }
    next.duration_frames = Math.max(next.duration_frames, end(clip));
  }
  return validateResult(reconcileSubtitleBindings(document, next,
    {trim: {clipId, shiftFrames: edge === 'in' ? deltaFrames : 0}}));
}

/** Split only the explicit timeline selection; no selection or an invalid
 * selection never falls through to another track. Both halves retain the
 * original content and style.
 */
export async function splitSelection(document: ProjectDocument, assets: Asset[], selection: TimelineSelection, frame: number): Promise<{document: ProjectDocument; selection: TimelineSelection}> {
  integer(frame, '拆分位置');
  ratio(document.fps);
  if (frame < 0 || frame >= document.duration_frames) throw new Error('拆分位置须位于工程范围内，且不能是工程终点');
  if (!selection || selection.kind === 'asset') throw new Error('请先选中时间线上的视频、音频或字幕，再拆分');

  if (selection?.kind === 'subtitle') {
    const index = document.subtitles.findIndex(sub => sub.id === selection.id);
    if (index < 0) throw new Error('选中的字幕已不存在，请重新选择');
    const sub = document.subtitles[index];
    if (frame <= sub.start_frame || frame >= sub.end_frame) throw new Error('请把播放头移到选中字幕的内部，不能在起点或终点拆分');
    if (document.subtitles.length >= 500) throw new Error('本阶段每个工程最多支持 500 段字幕');
    const next = clone(document);
    const left: Subtitle = {...clone(sub), end_frame: frame};
    const right: Subtitle = {...clone(sub), id: crypto.randomUUID(), start_frame: frame};
    if (sub.binding) {
      left.binding = sliceSubtitleBinding(document, sub, sub.start_frame, frame);
      right.binding = sliceSubtitleBinding(document, sub, frame, sub.end_frame);
    }
    next.subtitles.splice(index, 1, left, right);
    return {document: validateResult(next), selection: {kind: 'subtitle', id: sub.id}};
  }

  const clip = document.clips.find(value => value.id === selection.id);
  if (!clip) throw new Error('选中的片段已不存在，请重新选择');
  const kind = trackKind(document, clip) === 'audio' ? '音频' : '视频';
  if (frame <= clip.timeline_start_frame || frame >= end(clip)) throw new Error(`请把播放头移到选中${kind}片段的内部，不能在起点或终点拆分`);
  if (document.clips.length >= 200) throw new Error('本阶段每个工程最多支持 200 个媒体片段');
  const next = await runCoreAction(document, assets, 'clip/split', {clipId: clip.id, time: seconds(frame, document.fps)});
  return {document: validateResult(next), selection: {kind: 'clip', id: clip.id}};
}

/** Display hundredths of a second, without implying SMPTE drop-frame timecode. */
export function formatTime(frame: number, fps: Rational): string {
  integer(frame, '时间');
  ratio(fps);
  if (frame < 0) throw new Error('时间不能为负数');
  const hundredths = Math.round(seconds(frame, fps) * 100);
  return `${String(Math.floor(hundredths / 6000)).padStart(2, '0')}:${String(Math.floor(hundredths / 100) % 60).padStart(2, '0')}.${String(hundredths % 100).padStart(2, '0')}`;
}

/** Read-only source clock, rounded to milliseconds without losing large PTS. */
export function formatSourceTime(pts: string, timeBase: Rational): string {
  ratio(timeBase);
  const numerator = ticks(pts) * BigInt(timeBase.num) * 1000n;
  const denominator = BigInt(timeBase.den);
  const magnitude = numerator < 0n ? -numerator : numerator;
  const milliseconds = (magnitude * 2n + denominator) / (denominator * 2n);
  const sign = numerator < 0n && milliseconds !== 0n ? '-' : '';
  return `${sign}${String(milliseconds / 60000n).padStart(2, '0')}:${String(milliseconds / 1000n % 60n).padStart(2, '0')}.${String(milliseconds % 1000n).padStart(3, '0')}`;
}

export function secondsToFrames(value: number, fps: Rational): number {
  ratio(fps);
  if (!Number.isFinite(value) || value < 0) throw new Error('请输入有效的非负秒数');
  const frame = Math.round(value * fps.num / fps.den);
  integer(frame, '时间');
  return frame;
}

export function formatSeconds(frame: number, fps: Rational): string {
  integer(frame, '时间');
  ratio(fps);
  if (frame < 0) throw new Error('时间不能为负数');
  return seconds(frame, fps).toFixed(2);
}
