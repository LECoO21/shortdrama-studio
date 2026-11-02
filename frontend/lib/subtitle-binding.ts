import type {Clip, ProjectDocument, Subtitle, SubtitleBinding} from './types';

type Span = SubtitleBinding['spans'][number];
export type SubtitleBindingEdit = {
  trim?: {clipId: string; shiftFrames: number};
  split?: {clipId: string; rightClipId: string; offset: number};
};

function problem(message: string): never {
  throw new Error(`字幕关联未应用：${message}`);
}

function integer(value: number, label: string, minimum = 0) {
  if (!Number.isSafeInteger(value) || value < minimum) problem(`${label}必须是有效整数帧。`);
}

function identity(clip: Clip, binding: SubtitleBinding) {
  if (clip.source.asset_id !== binding.asset_id || clip.source.version !== binding.asset_version
    || clip.source.stream_index !== binding.stream_index) {
    problem('字幕关联的素材、版本或媒体流已变化，请先解除跟随再替换素材。');
  }
}

function sameSourceIdentity(before: Clip, after: Clip) {
  const left = before.source, right = after.source;
  return left.asset_id === right.asset_id && left.version === right.version && left.stream_index === right.stream_index
    && left.time_base.num === right.time_base.num && left.time_base.den === right.time_base.den;
}

function clipIndex(document: ProjectDocument) {
  const clips = new Map(document.clips.map(clip => [clip.id, clip]));
  if (clips.size !== document.clips.length) problem('片段ID重复，无法确定字幕来源。');
  return clips;
}

function requireClip(clips: Map<string, Clip>, id: string): Clip {
  const clip = clips.get(id);
  if (!clip) problem('字幕关联的片段已不存在，请恢复片段或解除跟随。');
  return clip;
}

function subtitleIndex(document: ProjectDocument, id: string) {
  const index = document.subtitles.findIndex(subtitle => subtitle.id === id);
  if (index < 0) problem('没有找到要编辑的字幕。');
  return index;
}

function interval(document: ProjectDocument, start: number, end: number) {
  integer(start, '字幕起点'); integer(end, '字幕终点', 1);
  if (start >= end || end > document.duration_frames) problem('字幕时间超出工程范围或不足一帧。');
}

function validateOne(document: ProjectDocument, subtitle: Subtitle, clips: Map<string, Clip>) {
  const binding = subtitle.binding;
  if (!binding) return;
  if (typeof binding.asset_id !== 'string' || !binding.asset_id || binding.asset_id.length > 100
    || !Number.isSafeInteger(binding.asset_version) || binding.asset_version < 1
    || !Number.isSafeInteger(binding.stream_index) || binding.stream_index < 0
    || typeof binding.review_required !== 'boolean' || !Array.isArray(binding.spans)
    || !binding.spans.length || binding.spans.length > 200) {
    problem('字幕来源信息无效，请解除跟随后重新关联。');
  }
  const provenance = [binding.transcript_id, binding.transcript_revision, binding.segment_id];
  if (provenance.some(value => value !== undefined)) {
    if (typeof binding.transcript_id !== 'string' || !binding.transcript_id || binding.transcript_id.length > 100
      || !Number.isSafeInteger(binding.transcript_revision) || binding.transcript_revision! < 1
      || typeof binding.segment_id !== 'string' || !binding.segment_id || binding.segment_id.length > 100) {
      problem('对白来源信息须完整保留转写、修订和句子ID。');
    }
  }
  interval(document, subtitle.start_frame, subtitle.end_frame);
  const owners = new Set<string>();
  let cursor = subtitle.start_frame;
  let sourceKind: 'video' | 'audio' | undefined;
  let sourceClock: Clip['source']['time_base'] | undefined;
  for (const span of binding.spans) {
    if (typeof span.clip_id !== 'string' || !span.clip_id || span.clip_id.length > 100 || owners.has(span.clip_id)) {
      problem('字幕的片段关联无效或重复。');
    }
    owners.add(span.clip_id);
    const clip = requireClip(clips, span.clip_id);
    const kind = document.tracks.find(track => track.id === clip.track_id)?.kind;
    if (kind !== 'video' && kind !== 'audio') problem('字幕只能跟随视频或音频片段。');
    if (sourceKind && (kind !== sourceKind || clip.source.time_base.num !== sourceClock!.num
      || clip.source.time_base.den !== sourceClock!.den)) {
      problem('同一句字幕的关联片段必须使用相同媒体类型和源时间基。');
    }
    sourceKind = kind; sourceClock = clip.source.time_base;
    identity(clip, binding);
    integer(clip.timeline_start_frame, '片段起点'); integer(clip.duration_frames, '片段长度', 1);
    integer(span.start_frame, '关联起点'); integer(span.end_frame, '关联终点', 1);
    if (span.start_frame >= span.end_frame || span.end_frame > clip.duration_frames) {
      problem('关联区间超出片段或不足一帧。');
    }
    const first = clip.timeline_start_frame + span.start_frame;
    const last = clip.timeline_start_frame + span.end_frame;
    if (first !== cursor) {
      problem('同一句字幕将变成不连续或倒序区间，请先拆分这条字幕或解除跟随。');
    }
    cursor = last;
  }
  if (cursor !== subtitle.end_frame) problem('字幕显示时间与关联区间不一致。');
}

/** The optional binding uses project-frame offsets within explicit clip instances.
 * Exact source clocks stay on clip.source; no source PTS is converted to Number.
 */
export function validateSubtitleBindings(document: ProjectDocument): void {
  if (!document.subtitles.some(subtitle => subtitle.binding)) return;
  const clips = clipIndex(document);
  for (const subtitle of document.subtitles) validateOne(document, subtitle, clips);
}

function clippedSpans(document: ProjectDocument, subtitle: Subtitle, start: number, end: number): Span[] {
  const clips = clipIndex(document);
  return subtitle.binding!.spans.flatMap(span => {
    const clip = requireClip(clips, span.clip_id);
    const first = Math.max(start, clip.timeline_start_frame + span.start_frame);
    const last = Math.min(end, clip.timeline_start_frame + span.end_frame);
    return first < last ? [{clip_id: clip.id, start_frame: first - clip.timeline_start_frame,
      end_frame: last - clip.timeline_start_frame}] : [];
  });
}

/** Restrict an existing binding to a subtitle split or generated display piece. */
export function sliceSubtitleBinding(document: ProjectDocument, subtitle: Subtitle,
  start: number, end: number): SubtitleBinding | undefined {
  if (!subtitle.binding) return undefined;
  validateOne(document, subtitle, clipIndex(document));
  interval(document, start, end);
  if (start < subtitle.start_frame || end > subtitle.end_frame) problem('分句区间超出原字幕，不能猜测新的来源。');
  const binding = {...structuredClone(subtitle.binding), spans: clippedSpans(document, subtitle, start, end)};
  validateOne(document, {...subtitle, start_frame: start, end_frame: end, binding}, clipIndex(document));
  return binding;
}

function validateSplit(before: ProjectDocument, after: ProjectDocument, split: NonNullable<SubtitleBindingEdit['split']>) {
  const original = requireClip(clipIndex(before), split.clipId);
  const left = requireClip(clipIndex(after), split.clipId), right = requireClip(clipIndex(after), split.rightClipId);
  integer(split.offset, '拆分偏移', 1);
  if (split.offset >= original.duration_frames || clipIndex(before).has(split.rightClipId)
    || !sameSourceIdentity(original, left) || !sameSourceIdentity(original, right)
    || left.timeline_start_frame !== original.timeline_start_frame
    || right.timeline_start_frame !== original.timeline_start_frame + split.offset
    || left.duration_frames !== split.offset || right.duration_frames !== original.duration_frames - split.offset
    || left.source.in_pts !== original.source.in_pts || right.source.out_pts !== original.source.out_pts
    || left.source.out_pts !== right.source.in_pts) {
    problem('拆分未保持原片段的连续来源，不能迁移字幕关联。');
  }
}

function validateTrimShift(before: ProjectDocument, original: Clip, updated: Clip, shift: number) {
  if (!Number.isSafeInteger(shift)) problem('裁剪偏移必须为整数帧。');
  try {
    const clock = BigInt(original.source.time_base.num) * BigInt(before.fps.num);
    const difference = (BigInt(updated.source.in_pts) - BigInt(original.source.in_pts)) * clock
      - BigInt(shift) * BigInt(original.source.time_base.den) * BigInt(before.fps.den);
    if ((difference < 0n ? -difference : difference) * 2n > clock) {
      problem('裁剪源入点与字幕偏移不一致，已保留原工程。');
    }
  } catch (error) {
    if (error instanceof SyntaxError || error instanceof RangeError) problem('源时间基无效，不能迁移字幕关联。');
    throw error;
  }
}

/** Reconcile only bound captions after a media edit. Legacy captions retain the
 * caller's existing absolute/ripple policy; clip edits never split caption text.
 */
export function reconcileSubtitleBindings(before: ProjectDocument, after: ProjectDocument,
  edit: SubtitleBindingEdit = {}): ProjectDocument {
  validateSubtitleBindings(before);
  const next = structuredClone(after);
  if (!next.subtitles.some(subtitle => subtitle.binding)) return next;
  if (before.project_id !== after.project_id || before.fps.num !== after.fps.num || before.fps.den !== after.fps.den) {
    problem('工程或帧率已变化，不能直接迁移字幕关联。');
  }
  if (edit.split) validateSplit(before, after, edit.split);
  const originalClips = clipIndex(before), updatedClips = clipIndex(next);
  next.subtitles = next.subtitles.flatMap(subtitle => {
    if (!subtitle.binding) return [subtitle];
    const binding = structuredClone(subtitle.binding);
    const expanded = binding.spans.flatMap((span): Span[] => {
      if (!edit.split || span.clip_id !== edit.split.clipId) return [span];
      const split = edit.split;
      const spans: Span[] = [];
      if (span.start_frame < split.offset) spans.push({...span, end_frame: Math.min(span.end_frame, split.offset)});
      if (span.end_frame > split.offset) spans.push({clip_id: split.rightClipId,
        start_frame: Math.max(span.start_frame, split.offset) - split.offset, end_frame: span.end_frame - split.offset});
      return spans;
    });
    binding.spans = expanded.flatMap((span): Span[] => {
      const clip = updatedClips.get(span.clip_id);
      if (!clip) {binding.review_required = true; return [];}
      identity(clip, binding);
      const original = originalClips.get(span.clip_id);
      if (!original && span.clip_id !== edit.split?.rightClipId) problem('字幕关联片段不在当前编辑基线中。');
      if (original) {
        if (!sameSourceIdentity(original, clip)) problem('字幕关联的素材、版本或时间基已变化，请先解除跟随。');
        const trimming = edit.trim?.clipId === clip.id;
        const splitting = edit.split?.clipId === clip.id;
        if (!trimming && !splitting && (original.source.in_pts !== clip.source.in_pts
          || original.source.out_pts !== clip.source.out_pts || original.duration_frames !== clip.duration_frames)) {
          problem('片段源区间已改变，请使用受控裁剪或先解除字幕跟随。');
        }
        if (trimming) validateTrimShift(before, original, clip, edit.trim!.shiftFrames);
      }
      const shift = edit.trim?.clipId === clip.id ? edit.trim.shiftFrames : 0;
      const requestedStart = span.start_frame - shift, requestedEnd = span.end_frame - shift;
      const first = Math.max(0, Math.min(clip.duration_frames, requestedStart));
      const last = Math.max(0, Math.min(clip.duration_frames, requestedEnd));
      if (first !== requestedStart || last !== requestedEnd) binding.review_required = true;
      return first < last ? [{clip_id: clip.id, start_frame: first, end_frame: last}] : [];
    });
    if (!binding.spans.length) return [];
    const firstSpan = binding.spans[0], lastSpan = binding.spans.at(-1)!;
    const updated = {...subtitle, binding,
      start_frame: requireClip(updatedClips, firstSpan.clip_id).timeline_start_frame + firstSpan.start_frame,
      end_frame: requireClip(updatedClips, lastSpan.clip_id).timeline_start_frame + lastSpan.end_frame};
    validateOne(next, updated, updatedClips);
    return [updated];
  });
  validateSubtitleBindings(next);
  return next;
}

/** Manual retiming stays within the explicitly linked owners; it never adopts
 * another overlapping copy of the same asset and never clears a pending review.
 */
export function retimeSubtitle(document: ProjectDocument, id: string, start: number, end: number): ProjectDocument {
  validateSubtitleBindings(document);
  interval(document, start, end);
  const next = structuredClone(document), subtitle = next.subtitles[subtitleIndex(next, id)];
  if (subtitle.binding) {
    const clips = clipIndex(document);
    const spans = subtitle.binding.spans.flatMap((span): Span[] => {
      const clip = requireClip(clips, span.clip_id);
      const first = Math.max(start, clip.timeline_start_frame), last = Math.min(end, clip.timeline_start_frame + clip.duration_frames);
      return first < last ? [{clip_id: clip.id, start_frame: first - clip.timeline_start_frame,
        end_frame: last - clip.timeline_start_frame}] : [];
    });
    if (!spans.length) problem('新时间不在关联片段内，请先解除跟随。');
    let cursor = start;
    for (const span of spans) {
      const clip = requireClip(clips, span.clip_id);
      if (clip.timeline_start_frame + span.start_frame !== cursor) problem('新时间超出关联片段覆盖范围，请先解除跟随。');
      cursor = clip.timeline_start_frame + span.end_frame;
    }
    if (cursor !== end) problem('新时间超出关联片段覆盖范围，请先解除跟随。');
    subtitle.binding = {...subtitle.binding, spans};
  }
  subtitle.start_frame = start; subtitle.end_frame = end;
  validateSubtitleBindings(next);
  return next;
}

export function detachSubtitleBinding(document: ProjectDocument, id: string): ProjectDocument {
  const next = structuredClone(document);
  delete next.subtitles[subtitleIndex(next, id)].binding;
  validateSubtitleBindings(next);
  return next;
}

export function detachClipSubtitleBindings(document: ProjectDocument, clipId: string): ProjectDocument {
  const next = structuredClone(document);
  for (const subtitle of next.subtitles) {
    if (subtitle.binding?.spans.some(span => span.clip_id === clipId)) delete subtitle.binding;
  }
  validateSubtitleBindings(next);
  return next;
}

/** Explicit user binding records no guessed transcript provenance. */
export function bindSubtitleToClip(document: ProjectDocument, id: string, clipId: string): ProjectDocument {
  const next = structuredClone(document), subtitle = next.subtitles[subtitleIndex(next, id)];
  const clip = requireClip(clipIndex(next), clipId);
  if (subtitle.start_frame < clip.timeline_start_frame || subtitle.end_frame > clip.timeline_start_frame + clip.duration_frames) {
    problem('字幕必须完整位于所选片段内才能建立跟随。');
  }
  subtitle.binding = {asset_id: clip.source.asset_id, asset_version: clip.source.version,
    stream_index: clip.source.stream_index, review_required: false,
    spans: [{clip_id: clip.id, start_frame: subtitle.start_frame - clip.timeline_start_frame,
      end_frame: subtitle.end_frame - clip.timeline_start_frame}]};
  validateSubtitleBindings(next);
  return next;
}
