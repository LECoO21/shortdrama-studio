import {segmentLine} from '../vendor/openfilm/subtitle-segments.mjs';
import type {Rational, Subtitle, SubtitleProposal} from './types';
import {sliceSubtitleBinding, validateSubtitleBindings} from './subtitle-binding';

const MAX_SUBTITLES = 500;

function problem(message: string): never {
  throw new Error(`字幕分句未应用：${message}`);
}

function requireFrame(frame: number) {
  if (!Number.isSafeInteger(frame) || frame < 0) problem('字幕时间必须是非负整数工程帧。');
}

function validateCaptions(subtitles: Subtitle[], duration: number) {
  const ids = new Set<string>();
  for (const subtitle of subtitles) {
    if (!subtitle.id || subtitle.id.length > 100 || ids.has(subtitle.id)) problem('字幕 ID 无效或重复，请重新生成提案。');
    ids.add(subtitle.id);
    requireFrame(subtitle.start_frame);
    requireFrame(subtitle.end_frame);
    if (subtitle.start_frame >= subtitle.end_frame || subtitle.end_frame > duration) {
      problem('有字幕不足一帧或超出工程范围，请校对时间。');
    }
    if (!subtitle.text.trim() || subtitle.text.length > 1000) problem('字幕文字须为 1–1000 个字符。');
  }
  const ordered = [...subtitles].sort((a, b) => a.start_frame - b.start_frame);
  for (let index = 1; index < ordered.length; index++) {
    if (ordered[index].start_frame < ordered[index - 1].end_frame) {
      problem('字幕时间重叠，请先调整原有字幕或对白区间。');
    }
  }
}

function estimatedFrame(milliseconds: number, fps: Rational): number {
  // Interior times emitted by the upstream segmenter are integer milliseconds.
  // Convert only a local offset: absolute source PTS never enters a JS number.
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 0) problem('分句时间无效，请校对原句区间。');
  const numerator = BigInt(milliseconds) * BigInt(fps.num);
  const denominator = 1000n * BigInt(fps.den);
  return Number((2n * numerator + denominator) / (2n * denominator));
}

function partId(parentId: string, part: number, used: Set<string>): string {
  // Stable browser-side FNV-1a, used for identity only; this is not a content hash.
  let hash = 0xcbf29ce484222325n;
  for (const character of parentId) {
    hash = BigInt.asUintN(64, (hash ^ BigInt(character.codePointAt(0)!)) * 0x100000001b3n);
  }
  const base = `caption-part-${hash.toString(16).padStart(16, '0')}-${part}`;
  let id = base, collision = 1;
  while (used.has(id)) id = `${base}-${collision++}`;
  used.add(id);
  return id;
}

/** Split only newly proposed captions. Internal times are text-derived estimates;
 * first/last project frames, original styles and existing captions are retained.
 * The original first ID remains the backend's idempotent re-import anchor.
 */
export function segmentSubtitleProposal(proposal: SubtitleProposal): SubtitleProposal {
  const {fps, duration_frames: duration, subtitles} = proposal.document;
  if (![fps.num, fps.den].every(value => Number.isSafeInteger(value) && value > 0 && value <= 1_000_000_000)
    || fps.num / fps.den < 1 || fps.num / fps.den > 120) {
    problem('工程帧率无效。');
  }
  requireFrame(duration);
  if (!duration || duration > 108_000 || duration * fps.den / fps.num > 600 || subtitles.length > MAX_SUBTITLES) {
    problem('工程时长或字幕数量无效。');
  }
  validateCaptions(subtitles, duration);
  const requested = new Set(proposal.added_ids);
  if (requested.size !== proposal.added_ids.length || proposal.added_ids.some(id => !subtitles.some(subtitle => subtitle.id === id))) {
    problem('提案中的新增字幕 ID 重复或不存在，请重新生成。');
  }
  const used = new Set(subtitles.map(subtitle => subtitle.id));
  const remainingBudget = MAX_SUBTITLES - (subtitles.length - requested.size);
  const generated = new Map<string, Subtitle[]>();
  let generatedCount = 0;
  for (const subtitle of subtitles) {
    if (!requested.has(subtitle.id)) continue;
    const durationFrames = subtitle.end_frame - subtitle.start_frame;
    const durationMs = durationFrames * (1000 * fps.den / fps.num);
    // No raw word marks are reused: edits may have invalidated their alignment.
    const pieces = segmentLine({text: subtitle.text, startMs: 0, endMs: durationMs});
    if (!pieces.length) problem('分句没有产生有效文字。');
    generatedCount += pieces.length;
    if (generatedCount > remainingBudget) problem('分句后超过 500 条字幕，请减少所选句子。');
    const captions = pieces.map((piece, index): Subtitle => {
      if (!Number.isFinite(piece.startMs) || !Number.isFinite(piece.endMs)
        || piece.startMs < 0 || piece.endMs > durationMs || piece.startMs >= piece.endMs) {
        problem('原句时间不足以容纳分句，请校对时间或按原句加入。');
      }
      const start = index === 0 ? 0 : estimatedFrame(piece.startMs, fps);
      const finish = index === pieces.length - 1 ? durationFrames : estimatedFrame(piece.endMs, fps);
      if (start >= finish || finish > durationFrames) {
        problem('有分句不足一个工程帧，请校对时间或按原句加入。');
      }
      const caption: Subtitle = {...structuredClone(subtitle), id: index === 0 ? subtitle.id : partId(subtitle.id, index, used),
        text: piece.text, start_frame: subtitle.start_frame + start, end_frame: subtitle.start_frame + finish};
      if(subtitle.binding)caption.binding=sliceSubtitleBinding(proposal.document,subtitle,caption.start_frame,caption.end_frame);
      return caption;
    });
    generated.set(subtitle.id, captions);
  }
  const result = structuredClone(proposal);
  result.document.subtitles = result.document.subtitles.flatMap(subtitle => generated.get(subtitle.id) ?? [subtitle]);
  result.added_ids = proposal.added_ids.flatMap(id => generated.get(id)!.map(subtitle => subtitle.id));
  validateCaptions(result.document.subtitles, duration);
  validateSubtitleBindings(result.document);
  return result;
}
