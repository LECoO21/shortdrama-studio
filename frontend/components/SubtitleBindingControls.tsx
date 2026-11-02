'use client';

import type {Asset, Clip, ProjectDocument, Subtitle} from '../lib/types';
import {bindSubtitleToClip, detachSubtitleBinding} from '../lib/subtitle-binding';
import {formatTime} from '../lib/timeline-editing';

type Actions = {
  document: ProjectDocument; disabled?: boolean;
  onCommit: (document: ProjectDocument) => void;
  onNotice: (message: string) => void; onError: (message: string) => void;
};

export default function SubtitleBindingControls({document, subtitle, assets, disabled, onCommit, onNotice, onError}: Actions & {
  subtitle: Subtitle; assets: Asset[];
}) {
  const binding = subtitle.binding;
  const candidates = document.clips.filter(clip => clip.timeline_start_frame <= subtitle.start_frame
    && clip.timeline_start_frame + clip.duration_frames >= subtitle.end_frame);
  const multiple = (binding?.spans.length ?? 0) > 1;
  const value = multiple ? '__multiple' : binding?.spans[0]?.clip_id ?? '';
  function change(clipId: string) {
    try {
      const next = clipId ? bindSubtitleToClip(document, subtitle.id, clipId) : detachSubtitleBinding(document, subtitle.id);
      onCommit(next);
      onNotice(clipId ? '字幕已关联到这个片段，移动和裁剪时会跟随，可撤销。' : '字幕已改为独立时间，可自由调整；原文字和时间保留，可撤销。');
    } catch (error) {onError(error instanceof Error ? error.message : '字幕关联未完成。');}
  }
  return <section className="property-group" aria-label="字幕联动设置">
    <h4>字幕联动</h4>
    <label className="field-help binding-select">跟随片段
      <select aria-label="字幕跟随片段" value={value} disabled={disabled} onChange={event => change(event.target.value)}>
        <option value="">独立字幕 · 保持自己的时间</option>
        {multiple && <option value="__multiple" disabled>跟随 {binding!.spans.length} 个相邻片段</option>}
        {candidates.map(clip => <option value={clip.id} key={clip.id}>
          {document.tracks.find(track => track.id === clip.track_id)?.kind === 'audio' ? '音频' : '视频'} · {assets.find(asset => asset.id === clip.source.asset_id)?.filename ?? '来源素材'} · {formatTime(clip.timeline_start_frame, document.fps)}
        </option>)}
      </select>
    </label>
    <p className="field-help">{binding ? '已按片段实例关联，来源片段移动或裁剪时，本条字幕随之调整。' : '旧字幕默认保留独立时间。选择覆盖本条字幕的片段后才建立关联。'}</p>
    {multiple && <p className="field-help">这条字幕跨越切点，拆分视频不会拆开它。要单独移开其中一段，请先拆分字幕或改为独立字幕。</p>}
    {binding?.review_required && <>
      <p className="field-help" role="status">字幕显示区间已被裁剪，文字保留，请试听核对。</p>
      <button className="wide-button" disabled={disabled} onClick={() => {
        const next = structuredClone(document), target = next.subtitles.find(item => item.id === subtitle.id)!;
        target.binding!.review_required = false;
        onCommit(next); onNotice('已标记这条裁剪字幕为已核对，可撤销。');
      }}>已核对裁剪后的字幕</button>
    </>}
    <p className="field-help">片段联动保持工程时间对应，仍需核对实际对白和内部断句时间。</p>
  </section>;
}

export function ClipSubtitleBindingControls({document, clip, disabled, onCommit, onNotice}: Actions & {clip: Clip}) {
  const linked = document.subtitles.filter(subtitle => subtitle.binding?.spans.some(span => span.clip_id === clip.id));
  if (!linked.length) return null;
  return <section className="property-group" aria-label="片段关联字幕">
    <h4>关联字幕 · {linked.length} 条</h4>
    {linked.some(subtitle => subtitle.binding?.review_required) && <p className="field-help" role="status">部分字幕的显示区间已被裁剪，请选中对应字幕试听复核。</p>}
    <p className="field-help">这些字幕会跟随本片段。替换原素材或单独移动跨切点片段前，可以将字幕改为独立时间。</p>
    <button className="wide-button" disabled={disabled} onClick={() => {
      const next = structuredClone(document);
      for (const subtitle of next.subtitles) {
        if (subtitle.binding?.spans.some(span => span.clip_id === clip.id)) delete subtitle.binding;
      }
      onCommit(next); onNotice(`已将 ${linked.length} 条字幕改为独立时间，文字和位置保留，可撤销。`);
    }}>解除该片段的字幕联动</button>
  </section>;
}
