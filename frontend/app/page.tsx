'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import type {PointerEvent as ReactPointerEvent, ReactNode} from 'react';
import Link from 'next/link';
import CompositionPreview from '@/components/CompositionPreview';
import AssetAnalysisPanel from '@/components/AssetAnalysisPanel';
import ProjectLibrary from '@/components/ProjectLibrary';
import ImportQueue from '@/components/ImportQueue';
import AssetDetails from '@/components/AssetDetails';
import SubtitleBindingControls, {ClipSubtitleBindingControls} from '@/components/SubtitleBindingControls';
import {reconcileSubtitleBindings, retimeSubtitle} from '@/lib/subtitle-binding';
import {useImportQueue} from '@/lib/use-import-queue';
import {mediaUrl} from '@/lib/api';
import {replaceSource, seconds} from '@/lib/editor';
import {appendAsset, reorderVideo, removeClip, trimClip, formatTime, formatSourceTime, splitSelection} from '@/lib/timeline-editing';
import type {TimelineSelection} from '@/lib/timeline-editing';
import {useStudio} from '@/lib/use-studio';
import type {Asset, Clip, ProjectDocument, Subtitle} from '@/lib/types';

type Panel = 'media' | 'subtitles' | 'exports';
type Modal = 'projects' | 'help' | 'analysis' | 'imports' | {kind: 'source'; asset: Asset; start?: number; fromAnalysis?: boolean} | {kind: 'output'; url: string; revision: number} | null;
type Gesture = {id: string; kind: 'move' | 'in' | 'out' | 'subtitle'; x: number; original: ProjectDocument; delta: number; candidate: ProjectDocument | null; problem: string};
const clone = <T,>(value: T): T => structuredClone(value);
const message = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';

function Icon({name, size = 18}: {name: string; size?: number}) {
  const paths: Record<string, ReactNode> = {
    film: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4m-4 6h4m10-6h4m-4 6h4"/></>,
    text: <><path d="M4 5h16M12 5v15M8 20h8M4 5v3m16-3v3"/></>,
    export: <><path d="M12 15V3m-4 4 4-4 4 4M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/></>,
    upload: <><path d="M12 16V4m-4 4 4-4 4 4M4 16v4h16v-4"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
    split: <><circle cx="5" cy="6" r="3"/><circle cx="5" cy="18" r="3"/><path d="m8 8 12 12M8 16 20 4M12 12l-4-4"/></>,
    trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></>,
    undo: <><path d="M8 5 3 10l5 5M3 10h11a6 6 0 0 1 0 12"/></>,
    redo: <><path d="m16 5 5 5-5 5m5-5H10a6 6 0 0 0 0 12"/></>,
    close: <path d="m6 6 12 12M6 18 18 6"/>,
    search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
    audio: <><path d="M4 9v6m4-10v14m4-16v18m4-16v14m4-10v6"/></>,
    check: <path d="m5 12 4 4L20 5"/>,
    eye: <><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>,
    history: <><path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l4 2"/></>,
    help: <><circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 1 1 4 3v3m-1 3h.01"/></>,
    magnet: <><path d="M5 4v10a7 7 0 0 0 14 0V4h-5v10a2 2 0 0 1-4 0V4ZM5 8h5m4 0h5"/></>,
    minus: <path d="M5 12h14"/>,
    fit: <path d="M9 3H3v6m12-6h6v6M3 15v6h6m6 0h6v-6"/>,
    play: <path d="m8 4 12 8-12 8Z"/>,
    folder: <path d="M3 7V4h7l2 3h9v13H3Z"/>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] ?? paths.film}</svg>;
}

function NumberField({id, label, value, min = 0, max, step = .04, suffix = '秒', onCommit, onDraft, onError}: {
  id: string; label: string; value: number; min?: number; max?: number; step?: number; suffix?: string;
  onCommit: (value: number) => void; onDraft: (id: string, dirty: boolean) => void; onError: (value: string) => void;
}) {
  const initial = String(Math.round(value * 1000) / 1000);
  const [draft, setDraft] = useState(initial);
  const [invalid, setInvalid] = useState(false);
  useEffect(() => () => onDraft(id, false), [id, onDraft]);
  function apply() {
    if (draft === initial) { onDraft(id, false); return; }
    const parsed = Number(draft);
    if (!draft.trim() || !Number.isFinite(parsed) || parsed < min || (max !== undefined && parsed > max)) {
      setInvalid(true); onError(`${label}需在 ${min}${max === undefined ? ' 以上' : `–${max}`} ${suffix}范围内。`); return;
    }
    try { onCommit(parsed); onDraft(id, false); setDraft(initial); setInvalid(false); }
    catch (error) { setInvalid(true); onError(message(error)); }
  }
  return <label className="number-field"><span>{label}</span><div><input aria-label={label} inputMode="decimal" type="number" step={step} min={min} max={max} value={draft} aria-invalid={invalid}
    onChange={event => {setDraft(event.target.value); setInvalid(false); onDraft(id, event.target.value !== initial);}}
    onBlur={apply} onKeyDown={event => {if (event.key === 'Enter') event.currentTarget.blur(); if (event.key === 'Escape') {setDraft(initial); onDraft(id, false); setInvalid(false); event.stopPropagation();}}}/><small>{suffix}</small></div></label>;
}

function Dialog({title, onClose, children}: {title: string; onClose: () => void; children: ReactNode}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {const dialog = ref.current; dialog?.showModal(); return () => dialog?.close();}, []);
  return <dialog ref={ref} className="studio-dialog" onCancel={event => {event.preventDefault(); onClose();}} onClick={event => {if (event.target === event.currentTarget) onClose();}}>
    <header><h2>{title}</h2><button className="icon-button" aria-label="关闭弹窗" onClick={onClose}><Icon name="close"/></button></header><div className="dialog-body">{children}</div>
  </dialog>;
}

export default function Studio() {
  const studio = useStudio();
  const {doc, record, assets, jobs: workspaceJobs, loading, working, dirty, error, notice, saveState} = studio;
  const [selection, setSelection] = useState<TimelineSelection>(null);
  const [panel, setPanel] = useState<Panel>('media');
  const [inspector, setInspector] = useState<'properties' | 'history' | 'checks'>('properties');
  const [frame, setFrame] = useState(0), [playing, setPlaying] = useState(false);
  const [query, setQuery] = useState(''), [filter, setFilter] = useState('all');
  const [modal, setModal] = useState<Modal>(null);
  const [libraryDraftActive, setLibraryDraftActive] = useState(false);
  const [assetScope, setAssetScope] = useState<'project' | 'shared'>('project');
  const importQueue = useImportQueue(studio.libraryContext?.production.id, studio.receiveImportedAsset, studio.reconcileImports);
  const [transcriptDraftActive, setTranscriptDraftActive] = useState(false);
  const [zoom, setZoom] = useState(1), [magnetic, setMagnetic] = useState(true);
  const [timelineHeight, setTimelineHeight] = useState(280);
  const [mobile, setMobile] = useState<'none' | 'left' | 'right'>('none');
  const [gestureDoc, setGestureDoc] = useState<ProjectDocument | null>(null);
  const [gestureText, setGestureText] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const pendingRef = useRef(new Set<string>()), gesture = useRef<Gesture | null>(null);
  const liveSelection = useRef({projectId: record?.id, selection});
  useEffect(() => {liveSelection.current = {projectId: record?.id, selection};}, [record?.id, selection]);
  const ruler = useRef<HTMLDivElement>(null), upload = useRef<HTMLInputElement>(null);
  const view = gestureDoc ?? doc;
  const selectedClip = view?.clips.find(clip => selection?.kind === 'clip' && clip.id === selection.id);
  const selectedClipAsset = assets.find(asset => asset.id === selectedClip?.source.asset_id);
  const selectedSub = view?.subtitles.find(subtitle => selection?.kind === 'subtitle' && subtitle.id === selection.id);
  const selectedAsset = assets.find(asset => selection?.kind === 'asset' && asset.id === selection.id);
  const lastFrame = Math.max(0, (view?.duration_frames ?? 1) - 1);
  const position = Math.min(frame, lastFrame);
  const fps = view ? view.fps.num / view.fps.den : 25;
  const busy = working || actionBusy;
  const jobs = workspaceJobs.filter(job => job.project_id === record?.id);
  const runningJobs = jobs.filter(job => job.status === 'queued' || job.status === 'running').length;
  const libraryAssets = assetScope === 'shared' ? assets : studio.libraryContext?.assets ?? [];
  const allAssets = libraryAssets.filter(asset => asset.filename.toLowerCase().includes(query.toLowerCase()) && (filter === 'all' || (filter === 'video' ? asset.metadata.video_stream_index != null : asset.metadata.video_stream_index == null)));

  const markDraft = useCallback((id: string, value: boolean) => {
    const next = new Set(pendingRef.current); if (value) next.add(id); else next.delete(id);
    pendingRef.current = next; setPending(next);
  }, []);
  useEffect(() => {const warn = (event: BeforeUnloadEvent) => {if (pendingRef.current.size) {event.preventDefault(); event.returnValue = '';}}; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);}, []);

  function chooseClip(clip: Clip) {if (pendingRef.current.size) return; setSelection({kind: 'clip', id: clip.id}); setFrame(clip.timeline_start_frame); setPlaying(false); setInspector('properties');}
  function chooseSub(subtitle: Subtitle) {if (pendingRef.current.size) return; setSelection({kind: 'subtitle', id: subtitle.id}); setFrame(subtitle.start_frame); setPlaying(false); setInspector('properties'); setMobile('right');}
  function updateClip(patch: Partial<Clip>, group = 'property') {
    if (!doc || !selectedClip) return;
    const next = clone(doc), clip = next.clips.find(value => value.id === selectedClip.id)!;
    Object.assign(clip, patch);
    if (clip.fade_in_frames + clip.fade_out_frames > clip.duration_frames) throw new Error('淡入与淡出合计不能超过片段时长。');
    studio.commit(next, `${group}:${clip.id}`);
  }
  function updateSub(patch: Partial<Subtitle>) {
    if (!doc || !selectedSub) return;
    const next = patch.start_frame !== undefined || patch.end_frame !== undefined
      ? retimeSubtitle(doc, selectedSub.id, patch.start_frame ?? selectedSub.start_frame, patch.end_frame ?? selectedSub.end_frame)
      : clone(doc);
    const subtitle = next.subtitles.find(value => value.id === selectedSub.id)!;
    Object.assign(subtitle, patch);
    if (subtitle.start_frame >= subtitle.end_frame || subtitle.end_frame > next.duration_frames) throw new Error('字幕结束需晚于开始，且不能超过剪辑时长。');
    studio.commit(next, `subtitle:${subtitle.id}`);
  }
  function addSubtitle() {
    if (!doc) return;
    const next = clone(doc), track = next.tracks.find(value => value.kind === 'subtitle');
    if (!track) {studio.setError('工程中没有字幕轨道。'); return;}
    const subtitle: Subtitle = {id: crypto.randomUUID(), track_id: track.id, text: '在这里输入字幕', start_frame: position, end_frame: Math.min(next.duration_frames, position + Math.round(fps * 2)), font_size: 28, color: '#FFFFFF', bottom_percent: 8};
    next.subtitles.push(subtitle); studio.commit(next); setPanel('subtitles'); chooseSub(subtitle);
  }
  function addAsset(asset: Asset, at?: number) {
    if (!doc) return;
    try {
      let next = appendAsset(doc, asset);
      const old = new Set(doc.clips.map(clip => clip.id));
      let added = next.clips.find(clip => !old.has(clip.id))!;
      if (at !== undefined && next.tracks.find(track => track.id === added.track_id)?.kind === 'video') {next = reorderVideo(next, added.id, at); added = next.clips.find(clip => clip.id === added.id)!;}
      studio.commit(next); chooseClip(added); studio.setNotice('素材已加入时间线。');
    } catch (failure) {studio.setError(message(failure));}
  }
  async function split() {
    if (!doc || busy || gesture.current) return;
    setActionBusy(true); setPlaying(false);
    try {
      const result = await splitSelection(doc, assets, selection, position);
      studio.commit(result.document); setSelection(result.selection);
      const crossing = result.document.subtitles.filter(subtitle => (subtitle.binding?.spans.length ?? 0) > 1
        && (doc.subtitles.find(old => old.id === subtitle.id)?.binding?.spans.length ?? 0) < (subtitle.binding?.spans.length ?? 0));
      if (crossing.length) studio.setNotice(`所选片段已拆分；${crossing.length} 条跨切点字幕保持完整，单独移开片段前可拆字幕或解除联动。`);
    }
    catch (failure) {studio.setNotice(message(failure));} finally {setActionBusy(false);}
  }
  function remove() {
    if (!doc) return;
    try {
      if (selectedClip) {
        const next = removeClip(doc, selectedClip.id, magnetic);
        const removed = doc.subtitles.filter(subtitle => subtitle.binding && !next.subtitles.some(item => item.id === subtitle.id)).length;
        studio.commit(next); setSelection(null);
        if (removed) studio.setNotice(`已移除片段及 ${removed} 条关联字幕，可一次撤销；原素材保留。`);
      }
      else if (selectedSub) {const next = clone(doc); next.subtitles = next.subtitles.filter(subtitle => subtitle.id !== selectedSub.id); studio.commit(next); setSelection(null);}
      else studio.setNotice('先选中需要移除的片段或字幕。');
      setPlaying(false);
    } catch (failure) {studio.setError(message(failure));}
  }
  async function exportProject() {
    if (pendingRef.current.size) {studio.setError('请先完成当前时间输入，按 Enter 或点输入框外应用。'); return;}
    setPanel('exports'); setMobile('left');
    await studio.exportProject();
  }
  function openModal(value: Modal) {setPlaying(false); setModal(value);}
  async function importFiles(files: File[]) {
    if (!files.length) return;
    try {
      const target = await studio.ensureProduction();
      importQueue.enqueue(files, target); openModal('imports');
    } catch (failure) {studio.setError(message(failure));}
  }
  async function beforeLibraryAction() {
    if (gesture.current || pendingRef.current.size) {studio.setError('请先结束拖动并应用当前数值输入。'); return false;}
    return !record || !!await studio.saveNow();
  }
  function frameAt(clientX: number) {const rect = ruler.current?.getBoundingClientRect(); return !rect || !view ? 0 : Math.max(0, Math.min(view.duration_frames - 1, Math.round((clientX - rect.left) / rect.width * view.duration_frames)));}
  function begin(event: ReactPointerEvent<HTMLElement>, id: string, kind: Gesture['kind']) {
    if (!doc || busy) return;
    if (pendingRef.current.size) {
      // A captured pointer suppresses the normal blur. Apply a numeric draft
      // first, then let a fresh gesture read the resulting document.
      (window.document.activeElement as HTMLElement | null)?.blur();
      event.preventDefault(); event.stopPropagation();
      if (pendingRef.current.size) studio.setError('请先修正当前数值，或按 Esc 取消该输入。');
      else studio.setNotice('数值已应用，可以继续拖动片段。');
      return;
    }
    event.preventDefault(); event.stopPropagation();
    event.currentTarget.focus({preventScroll: true});
    event.currentTarget.setPointerCapture(event.pointerId); setPlaying(false);
    gesture.current = {id, kind, x: event.clientX, original: doc, delta: 0, candidate: null, problem: ''};
    setSelection({kind: kind === 'subtitle' ? 'subtitle' : 'clip', id});
  }
  function drag(event: ReactPointerEvent<HTMLElement>) {
    const current = gesture.current, rect = ruler.current?.getBoundingClientRect(); if (!current || !rect) return;
    const source = current.original, clip = source.clips.find(value => value.id === current.id);
    let delta = Math.round((event.clientX - current.x) / rect.width * source.duration_frames);
    if (magnetic && clip && current.kind === 'move') {
      const proposed = clip.timeline_start_frame + delta;
      const points = [position, 0, ...source.clips.filter(value => value.id !== clip.id).flatMap(value => [value.timeline_start_frame, value.timeline_start_frame + value.duration_frames])];
      const nearest = points.sort((a, b) => Math.abs(a - proposed) - Math.abs(b - proposed))[0];
      if (Math.abs(nearest - proposed) <= Math.max(1, source.duration_frames / rect.width * 7)) delta = nearest - clip.timeline_start_frame;
    }
    if (!delta && !current.delta) return;
    current.delta = delta;
    try {
      let next: ProjectDocument;
      if (current.kind === 'subtitle') {
        const item = source.subtitles.find(value => value.id === current.id)!;
        const duration = item.end_frame - item.start_frame, start = Math.max(0, Math.min(source.duration_frames - duration, item.start_frame + delta));
        next = retimeSubtitle(source, item.id, start, start + duration);
      } else if (current.kind === 'in' || current.kind === 'out') next = trimClip(source, assets, current.id, current.kind, delta);
      else if (magnetic && source.tracks.find(track => track.id === clip?.track_id)?.kind === 'video') next = reorderVideo(source, current.id, Math.max(0, clip!.timeline_start_frame + delta));
      else {
        next = clone(source); const item = next.clips.find(value => value.id === current.id)!;
        item.timeline_start_frame = Math.max(0, Math.min(source.duration_frames - item.duration_frames, item.timeline_start_frame + delta));
        next = reconcileSubtitleBindings(source, next);
      }
      current.candidate = next; current.problem = ''; setGestureDoc(next);
      setGestureText(current.kind === 'move' ? '拖动重排 · 松开保存' : current.kind === 'subtitle' ? '移动字幕 · 松开保存' : '裁剪片段 · 松开保存');
    } catch (failure) {current.problem = message(failure); current.candidate = null; setGestureDoc(null); setGestureText(current.problem);}
  }
  function finishGesture(cancel = false) {
    const current = gesture.current; gesture.current = null; setGestureDoc(null); setGestureText('');
    if (!current || cancel) return;
    if (JSON.stringify(doc) !== JSON.stringify(current.original)) {
      studio.setNotice('拖动期间工程已变化，本次拖动已取消，请重新操作。');
      return;
    }
    if (current.problem) studio.setError(current.problem);
    else if (current.candidate) {
      studio.commit(current.candidate);
      const review = current.candidate.subtitles.filter(subtitle => subtitle.binding?.review_required
        && !current.original.subtitles.find(old => old.id === subtitle.id)?.binding?.review_required).length;
      if (review) studio.setNotice(`${review} 条关联字幕的显示区间随裁剪调整，文字保留，请试听核对；可撤销。`);
      else if (current.kind === 'move' && magnetic) studio.setNotice('画面已重排，关联字幕已跟随；独立字幕和音频请复核。');
    }
  }
  function toggleTrack(id: string) {if (!doc) return; const next = clone(doc); const track = next.tracks.find(value => value.id === id)!; track.enabled = !track.enabled; studio.commit(next);}

  useEffect(() => {
    function keyboard(event: KeyboardEvent) {
      const target = event.target as HTMLElement;
      if (event.key === 'Escape') {finishGesture(true); setMobile('none'); return;}
      if (gesture.current) {event.preventDefault(); return;}
      if (target.closest('dialog')) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {event.preventDefault(); (target as HTMLInputElement).blur?.(); void studio.saveNow(); return;}
      const timelineSurface = Boolean(target.closest('.time-ruler, .track-lane'));
      if (target.isContentEditable || target.closest('input, textarea, select, dialog') || (target.closest('[role="slider"]') && !target.closest('.time-ruler'))) return;
      if (event.code === 'Space' && !event.metaKey && !event.ctrlKey && !event.altKey) {
        if (!timelineSurface && target.closest('button, a, [role="button"]')) return;
        // Timeline buttons use Space for transport, not their native click
        // (which would reselect the item and jump back to its start).
        event.preventDefault(); if (!event.repeat) setPlaying(value => !value); return;
      }
      if (event.repeat && !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') {event.preventDefault(); if (event.shiftKey) studio.redo(); else studio.undo(); return;}
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key.toLowerCase() === 's') {event.preventDefault(); void split();}
      if (event.key === 'Delete' || event.key === 'Backspace') {event.preventDefault(); remove();}
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {event.preventDefault(); setPlaying(false); setFrame(value => Math.max(0, Math.min(lastFrame, value + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 10 : 1))));}
      if (event.key === '?') openModal('help');
    }
    window.addEventListener('keydown', keyboard); return () => window.removeEventListener('keydown', keyboard);
  });

  const number = (id: string, label: string, value: number, onCommit: (value: number) => void, options: {min?: number; max?: number; suffix?: string; step?: number} = {}) =>
    <NumberField key={`${id}:${value}`} id={id} label={label} value={value} onCommit={onCommit} onDraft={markDraft} onError={studio.setError} {...options}/>;
  const saveLabel = !doc ? '准备就绪' : pending.size ? '正在编辑…' : saveState === 'saving' ? '正在保存…' : saveState === 'conflict' ? '版本冲突' : saveState === 'error' ? '保存失败' : dirty ? '等待保存…' : '已自动保存';
  const status = gestureText || (pending.size ? '输入后按 Enter 或移出输入框即可应用' : selectedClip || selectedSub ? 'S 拆分选中内容 · 空格播放 / 暂停 · 点击尺子定位播放头' : '先选中片段或字幕，再在播放头处拆分 · 空格播放 / 暂停');

  return <main className="studio-shell" data-mobile-panel={mobile} style={{'--timeline-height': `${timelineHeight}px`} as React.CSSProperties}>
    <header className="studio-header">
      <button className="brand-button" title="切换或新建项目" onClick={() => openModal('projects')}><span className="brand-mark">序</span><span>序场<span className="brand-en"> STUDIO</span></span></button>
      <span className="header-divider"/>
      <button className="project-context" onClick={() => openModal('projects')} title="管理项目与剧集">{studio.libraryContext?.production.name ?? '项目与剧集'}</button>
      <input className="project-name" aria-label="剧集名称" value={doc?.name ?? ''} placeholder="我的剪辑" disabled={!doc} onChange={event => {if (doc) studio.commit({...doc, name: event.target.value}, 'project-name');}}/>
      <span className={`save-status ${dirty || pending.size ? 'is-dirty' : ''}`} data-save-state={saveState}><i/>{saveLabel}</span>
      <div className="header-actions">
        <Link href="/settings/ai" target="_blank" rel="noopener noreferrer" className="chatgpt-header-link" title="在新页面连接 ChatGPT，保留当前剪辑">连接 ChatGPT</Link>
        <button className="icon-button history-top" aria-label="查看版本历史" title="版本历史" onClick={() => {setInspector('history'); setMobile('right');}}><Icon name="history"/></button>
        <button className="icon-button" aria-label="快捷键与使用帮助" title="快捷键与使用帮助" onClick={() => openModal('help')}><Icon name="help"/></button>
        {(saveState === 'error' || saveState === 'conflict') && <button onClick={() => saveState === 'conflict' ? openModal('projects') : void studio.saveNow()}>处理保存</button>}
        <button className="export-button" onClick={() => void exportProject()} disabled={!doc || busy}><Icon name="export" size={16}/>{working ? '处理中…' : '导出成片'}{runningJobs > 0 && <b>{runningJobs}</b>}</button>
      </div>
    </header>
    {(!studio.online || error || notice) && <div className={`studio-banner ${error || !studio.online ? 'is-error' : ''}`} role={error || !studio.online ? 'alert' : 'status'}>
      <span>{!studio.online ? '服务连接中断。本页修改尚未同步，请勿关闭；正在重连。' : error || notice}</span>
      {error && <button onClick={() => {setInspector('checks'); setMobile('right');}}>查看检查</button>}
      {saveState === 'conflict' && <button onClick={() => {if (window.confirm('载入服务端版本将放弃本页未保存修改。继续吗？')) void studio.bootstrap();}}>载入最新版本</button>}
      <button aria-label="关闭提示" className="icon-button" onClick={() => {studio.setError(''); studio.setNotice('');}}><Icon name="close" size={15}/></button>
    </div>}
    {loading ? <div className="studio-loading"><span className="loading-dot"/>正在打开剪辑工作台…</div> : !view ? <section className="start-screen">
      <div className="start-icon"><Icon name="film" size={40}/></div><p className="eyebrow">把故事，剪得更好。</p><h1>开始你的下一段剪辑</h1><p>导入素材，在时间线上剪辑，随时预览。</p><div className="start-actions"><button className="primary" disabled={busy} onClick={() => {void studio.createProject().then(value => {if (value) upload.current?.click();});}}><Icon name="upload"/>导入素材开始</button><button onClick={() => void studio.createDemo()}>体验演示项目</button></div><button className="text-button" onClick={() => openModal('projects')}>打开已有项目</button>
    </section> : <>
      <div className="editor-workspace">
        <nav className="tool-rail" aria-label="工作区工具">
          {([['media', 'film', '素材'], ['subtitles', 'text', '字幕'], ['exports', 'export', '成片']] as const).map(([value, icon, label]) => <button key={value} className={panel === value ? 'active' : ''} onClick={() => {setPanel(value); setMobile('left');}} title={label}><Icon name={icon} size={21}/><span>{label}</span>{value === 'exports' && runningJobs > 0 && <i>{runningJobs}</i>}</button>)}
          <button className="rail-bottom" onClick={() => openModal('projects')} title="项目管理"><Icon name="folder"/><span>项目</span></button>
        </nav>
        <aside className="library-panel" aria-label={panel === 'media' ? '素材库' : panel === 'subtitles' ? '字幕列表' : '导出任务'}>
          <header className="panel-heading"><h2>{panel === 'media' ? '我的素材' : panel === 'subtitles' ? '字幕' : '导出与成片'}<span>{panel === 'media' ? libraryAssets.length : panel === 'subtitles' ? view.subtitles.length : jobs.length}</span></h2><button className="icon-button mobile-close" aria-label="关闭素材面板" onClick={() => setMobile('none')}><Icon name="close"/></button></header>
          {panel === 'media' ? <>
            <div className="library-controls"><button className="import-button" onClick={() => upload.current?.click()} disabled={busy}><Icon name="upload" size={16}/>导入素材</button><button className="wide-button" disabled={!libraryAssets.length} onClick={() => openModal('analysis')}><Icon name="search" size={16}/>镜头与检索</button><button className="wide-button" onClick={() => openModal('imports')}>导入进度与继续{importQueue.activeCount ? ` · ${importQueue.activeCount}` : ''}</button><label className="asset-library-scope">素材范围<select aria-label="素材库范围" value={assetScope} onChange={event => setAssetScope(event.target.value as typeof assetScope)}><option value="project">本项目素材</option><option value="shared">共享素材库</option></select></label><label className="asset-search"><Icon name="search" size={15}/><input aria-label="搜索素材文件名" placeholder="搜索素材" value={query} onChange={event => setQuery(event.target.value)}/></label><div className="filter-tabs">{[['all', '全部'], ['video', '视频'], ['audio', '音频']].map(([value, label]) => <button className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div></div>
            <div className="asset-grid" onDragOver={event => {if (event.dataTransfer.types.includes('Files')) event.preventDefault();}} onDrop={event => {if (event.dataTransfer.files.length) {event.preventDefault(); void importFiles(Array.from(event.dataTransfer.files));}}}>
              {allAssets.map(asset => <article className={`asset-card ${selectedAsset?.id === asset.id ? 'selected' : ''}`} key={asset.id} draggable onDragStart={event => {event.dataTransfer.setData('application/x-studio-asset', asset.id); event.dataTransfer.effectAllowed = 'copy';}}>
                <button className="asset-main" aria-label={`选择素材 ${asset.filename}`} onClick={() => {setSelection({kind: 'asset', id: asset.id}); setInspector('properties');}} onDoubleClick={() => openModal({kind: 'source', asset})}>
                  <span className="asset-image">{asset.metadata.video_stream_index != null ? <video preload="metadata" muted playsInline src={`${mediaUrl(asset.media_url)}#t=0.1`} aria-hidden="true"/> : <Icon name="audio" size={30}/>}<span className="asset-duration">{formatTime(Math.round(Number(asset.metadata.duration_seconds ?? 0) * fps), view.fps)}</span></span><strong title={asset.filename}>{asset.filename}</strong>
                </button><button className="asset-add" aria-label={`加入时间线 ${asset.filename}`} title="加入时间线末尾" onClick={() => addAsset(asset)}><Icon name="plus" size={16}/></button>
              </article>)}
              {!allAssets.length && <div className="panel-empty"><Icon name="film" size={30}/><p>{query ? '没有找到这个素材' : '把视频或音频拖到这里'}</p><small>也可以点击上方“导入素材”</small></div>}
            </div><div className="library-tip">双击预览 · 拖入时间线 · 单文件 ≤100 MiB</div>
          </> : panel === 'subtitles' ? <><div className="library-controls"><button className="import-button" onClick={addSubtitle}><Icon name="plus" size={16}/>在播放头添加字幕</button></div><div className="subtitle-list">{view.subtitles.map((subtitle, index) => <button key={subtitle.id} className={`subtitle-item ${selectedSub?.id === subtitle.id ? 'selected' : ''}`} onClick={() => chooseSub(subtitle)}><span>{String(index + 1).padStart(2, '0')}<small>{formatTime(subtitle.start_frame, view.fps)}</small></span><p>{subtitle.text || '请输入字幕'}</p></button>)}{!view.subtitles.length && <div className="panel-empty"><Icon name="text" size={30}/><p>给画面加一句字幕</p><small>定位播放头后，点击上方添加</small></div>}</div></> : <div className="export-list">{jobs.map(job => <article className="export-card" key={job.id}><div><span className={`job-state ${job.status}`}>{({queued: '排队中', running: '正在导出', succeeded: '已完成', failed: '导出失败', cancelled: '已取消'})[job.status]}</span><small>版本 {job.revision}</small></div>{(job.status === 'running' || job.status === 'queued') && <progress max={100} value={job.progress ?? 0}/>}<p>{typeof job.error === 'string' ? job.error : job.error?.message ?? (job.status === 'succeeded' ? '成片已就绪，可预览或下载。' : '可继续剪辑，任务会在后台运行。')}</p><div className="export-card-actions">{job.status === 'succeeded' ? <><button onClick={() => openModal({kind: 'output', url: mediaUrl(job.output_url ?? `/api/v1/jobs/${job.id}/output`), revision: Number(job.revision)})}><Icon name="play" size={13}/>预览</button><a href={mediaUrl(job.output_url ?? `/api/v1/jobs/${job.id}/output`)} download>下载</a></> : job.status === 'queued' || job.status === 'running' ? <button onClick={() => void studio.cancelJob(job.id)}>取消导出</button> : <button onClick={() => void studio.retryJob(job.id)}>重新导出</button>}</div></article>)}{!jobs.length && <div className="panel-empty"><Icon name="export" size={30}/><p>这里会保存你的成片</p><small>完成剪辑后，点击右上角导出</small></div>}</div>}
        </aside>
        <section className="preview-panel"><header className="preview-heading"><h2>剪辑预览</h2><span>{view.canvas.width} × {view.canvas.height}<i/> {Math.round(fps * 100) / 100} fps</span><button className="icon-button" aria-label="取消选中并查看项目" onClick={() => {setSelection(null); setInspector('properties');}}><Icon name="fit" size={15}/></button></header><CompositionPreview document={view} assets={assets} frame={position} playing={playing} onFrame={setFrame} onPlayingChange={setPlaying}/></section>
        <aside className="inspector-panel" aria-label="属性面板"><header className="inspector-tabs">{([['properties', '属性'], ['history', '历史'], ['checks', '检查']] as const).map(([value, label]) => <button className={inspector === value ? 'active' : ''} key={value} onClick={() => setInspector(value)}>{label}{value === 'checks' && studio.qc && !studio.qc.valid && <i/>}</button>)}<button className="icon-button mobile-close" aria-label="关闭属性面板" onClick={() => setMobile('none')}><Icon name="close"/></button></header><div className="inspector-content">
          {inspector === 'history' ? <><div className="inspector-title"><Icon name="history"/><h3>版本历史</h3></div><p className="muted">恢复会生成新版本，保留已有历史。</p>{studio.snapshots.map(snapshot => <div className="version-row" key={snapshot.revision}><span>版本 {snapshot.revision}<small>{new Date(snapshot.created_at).toLocaleString('zh-CN')}</small></span><button disabled={busy || snapshot.revision === record?.revision} onClick={() => void studio.restore(snapshot.revision)}>{snapshot.revision === record?.revision ? '当前' : '恢复'}</button></div>)}</> : inspector === 'checks' ? <><div className="inspector-title"><Icon name="check"/><h3>导出前检查</h3></div><p className="muted">检查素材、片段衔接和字幕范围。</p><button className="wide-button" disabled={busy || pending.size > 0} onClick={() => void studio.checkProject()}>保存并检查</button>{studio.qc ? <><div className={`check-result ${studio.qc.valid ? 'pass' : ''}`}>{studio.qc.valid ? '当前版本可以导出' : `需要处理 ${studio.qc.issues.length} 项问题`}</div>{studio.qc.issues.map((issue, index) => <button className="issue-item" key={index} onClick={() => {const clip = doc?.clips.find(value => value.id === issue.clip_id); const subtitle = doc?.subtitles.find(value => value.id === issue.subtitle_id); if (clip) chooseClip(clip); if (subtitle) chooseSub(subtitle);}}><span>{issue.message}</span>{Boolean(issue.clip_id || issue.subtitle_id) && <small>点击定位 →</small>}</button>)}</> : <p className="inspector-hint">修改后检查会自动失效。导出时也会自动检查。</p>}</> : selectedClip ? <>
            <div className="inspector-title"><Icon name={view.tracks.find(track => track.id === selectedClip.track_id)?.kind === 'audio' ? 'audio' : 'film'}/><h3>{view.tracks.find(track => track.id === selectedClip.track_id)?.kind === 'audio' ? '音频片段' : '视频片段'}</h3></div><p className="selected-filename">{assets.find(asset => asset.id === selectedClip.source.asset_id)?.filename}</p>
            <section className="property-group" aria-label="所选片段源范围"><h4>原素材范围</h4><div className="property-summary"><span>源入点</span><b title={`PTS ${selectedClip.source.in_pts}`}>{formatSourceTime(selectedClip.source.in_pts, selectedClip.source.time_base)}</b><span>源出点</span><b title={`PTS ${selectedClip.source.out_pts}`}>{formatSourceTime(selectedClip.source.out_pts, selectedClip.source.time_base)}</b></div><p className="field-help" title={`源时间基 ${selectedClip.source.time_base.num}/${selectedClip.source.time_base.den}；显示至毫秒，原始 PTS 保留`}>素材版本 {selectedClip.source.version} · 原素材时码</p><button className="wide-button" disabled={!selectedClipAsset || selectedClipAsset.metadata.audio_stream_index == null} onClick={() => openModal('analysis')}>生成 / 校对素材对白</button></section>
            <ClipSubtitleBindingControls document={view!} clip={selectedClip} disabled={busy} onCommit={studio.commit} onNotice={studio.setNotice} onError={studio.setError}/>
            <section className="property-group"><h4>时长与裁剪</h4><div className="property-summary"><span>位置</span><b>{formatTime(selectedClip.timeline_start_frame, view.fps)}</b><span>时长</span><b>{formatTime(selectedClip.duration_frames, view.fps)}</b></div><div className="field-pair">{number(`${selectedClip.id}-in`, '裁掉开头', 0, value => studio.commit(trimClip(doc!, assets, selectedClip.id, 'in', Math.round(value * fps))), {max: Math.max(0, seconds(selectedClip.duration_frames - 1, view.fps))})}{number(`${selectedClip.id}-out`, '裁掉结尾', 0, value => studio.commit(trimClip(doc!, assets, selectedClip.id, 'out', -Math.round(value * fps))), {max: Math.max(0, seconds(selectedClip.duration_frames - 1, view.fps))})}</div><p className="field-help">可直接拖动片段两侧裁剪。裁剪后自动衔接。</p></section>
            {view.tracks.find(track => track.id === selectedClip.track_id)?.kind === 'video' && <section className="property-group"><h4>画面适配</h4><div className="segmented"><button className={selectedClip.fit === 'contain' ? 'active' : ''} onClick={() => updateClip({fit: 'contain'})}>完整显示</button><button className={selectedClip.fit === 'cover' ? 'active' : ''} onClick={() => updateClip({fit: 'cover'})}>填满画面</button></div></section>}
            <section className="property-group"><h4>声音</h4><label className="switch-row"><span>片段静音</span><input type="checkbox" role="switch" checked={selectedClip.muted} onChange={event => updateClip({muted: event.target.checked})}/></label><label className="gain-control"><span>音量<b>{selectedClip.gain_db} dB</b></span><input aria-label="片段音量" type="range" min={-60} max={12} step={.5} value={selectedClip.gain_db} onChange={event => updateClip({gain_db: Number(event.target.value)}, 'gain')}/></label><div className="field-pair">{number(`${selectedClip.id}-fadein`, '淡入', seconds(selectedClip.fade_in_frames, view.fps), value => updateClip({fade_in_frames: Math.round(value * fps)}), {max: seconds(selectedClip.duration_frames, view.fps)})}{number(`${selectedClip.id}-fadeout`, '淡出', seconds(selectedClip.fade_out_frames, view.fps), value => updateClip({fade_out_frames: Math.round(value * fps)}), {max: seconds(selectedClip.duration_frames, view.fps)})}</div>{view.tracks.find(track => track.id === selectedClip.track_id)?.kind === 'video' && <label className="switch-row"><span>保留原声</span><input type="checkbox" role="switch" checked={selectedClip.include_source_audio} onChange={event => updateClip({include_source_audio: event.target.checked})}/></label>}</section>
            <section className="property-group"><h4>替换素材</h4><select aria-label="替换当前片段素材" value="" onChange={event => {try {const asset = assets.find(value => value.id === event.target.value); if (asset) studio.commit(replaceSource(doc!, selectedClip.id, asset));} catch (failure) {studio.setError(message(failure));}}}><option value="" disabled>选择同类型素材</option>{assets.filter(asset => view.tracks.find(track => track.id === selectedClip.track_id)?.kind === 'video' ? asset.metadata.video_stream_index != null : asset.metadata.audio_stream_index != null).map(asset => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}</select><p className="field-help">保留当前片段的位置和长度。</p></section>
          </> : selectedSub ? <><div className="inspector-title"><Icon name="text"/><h3>字幕编辑</h3></div><textarea className="subtitle-editor" aria-label="字幕文字" value={selectedSub.text} maxLength={1000} onChange={event => updateSub({text: event.target.value})}/><p className="field-help">修改即时预览，停下输入后自动保存。</p><SubtitleBindingControls document={view!} subtitle={selectedSub} assets={assets} disabled={busy} onCommit={studio.commit} onNotice={studio.setNotice} onError={studio.setError}/><section className="property-group"><h4>显示时间</h4><div className="field-pair">{number(`${selectedSub.id}-start`, '字幕开始', seconds(selectedSub.start_frame, view.fps), value => updateSub({start_frame: Math.round(value * fps)}), {max: seconds(view.duration_frames - 1, view.fps)})}{number(`${selectedSub.id}-end`, '字幕结束', seconds(selectedSub.end_frame, view.fps), value => updateSub({end_frame: Math.round(value * fps)}), {max: seconds(view.duration_frames, view.fps)})}</div></section><section className="property-group"><h4>文字样式</h4><div className="field-pair">{number(`${selectedSub.id}-size`, '字号', selectedSub.font_size, value => updateSub({font_size: Math.round(value)}), {min: 10, max: 120, step: 1, suffix: 'px'})}{number(`${selectedSub.id}-bottom`, '距底部', selectedSub.bottom_percent, value => updateSub({bottom_percent: value}), {min: 0, max: 30, step: 1, suffix: '%'})}</div><label className="color-field"><span>文字颜色</span><input aria-label="字幕颜色" type="color" value={selectedSub.color} onChange={event => updateSub({color: event.target.value})}/></label></section><button className="danger-button wide-button" onClick={remove}><Icon name="trash" size={15}/>移除这条字幕</button></> : selectedAsset ? <><div className="inspector-title"><Icon name={selectedAsset.metadata.video_stream_index != null ? 'film' : 'audio'}/><h3>素材信息</h3></div><p className="selected-filename">{selectedAsset.filename}</p><div className="asset-inspector-preview">{selectedAsset.metadata.video_stream_index != null ? <video muted preload="metadata" src={`${mediaUrl(selectedAsset.media_url)}#t=0.1`}/> : <Icon name="audio" size={45}/>}</div><div className="property-summary"><span>时长</span><b>{Number(selectedAsset.metadata.duration_seconds ?? 0).toFixed(2)} 秒</b><span>尺寸</span><b>{selectedAsset.metadata.width ? `${selectedAsset.metadata.width} × ${selectedAsset.metadata.height}` : '音频素材'}</b></div><button className="primary wide-button" onClick={() => addAsset(selectedAsset)}><Icon name="plus" size={16}/>加入时间线</button><button className="wide-button" onClick={() => openModal({kind: 'source', asset: selectedAsset})}><Icon name="play" size={15}/>预览源素材</button><p className="field-help">点击加入会顺接到同类轨道末尾；拖到时间线可以调整插入位置。共享素材加入并保存后，会关联到本项目。</p><AssetDetails key={`${selectedAsset.id}:${studio.libraryContext?.production.id}`} asset={selectedAsset} productionId={studio.libraryContext?.production.id} linked={!!studio.libraryContext?.assets.some(asset => asset.id === selectedAsset.id)} onBeforeRemove={beforeLibraryAction} onChanged={async () => {await studio.refreshLibrary(); const live = liveSelection.current; if (live.projectId === record?.id && live.selection?.kind === 'asset' && live.selection.id === selectedAsset.id) {setSelection(null); studio.setNotice('已从本项目移除素材关联，原片和共享分析保留。');}}} onAnalyze={() => openModal('analysis')}/></> : <><div className="inspector-title"><Icon name="film"/><h3>我的剪辑</h3></div><div className="property-summary"><span>画幅</span><b>{view.canvas.width} × {view.canvas.height}</b><span>总时长</span><b>{formatTime(view.duration_frames, view.fps)}</b><span>片段</span><b>{view.clips.length} 段</b><span>字幕</span><b>{view.subtitles.length} 条</b></div><div className="selection-hint"><Icon name="eye" size={25}/><p>选中一个片段或字幕<br/>在这里调整它的属性</p></div><button className="wide-button" onClick={() => {setInspector('checks'); void studio.checkProject();}}>检查当前剪辑</button></>}
        </div></aside>
      </div>
      <section className="timeline-panel" aria-label="剪辑时间线">
        <div className="timeline-resizer" role="separator" aria-label="调整时间线高度" aria-orientation="horizontal" tabIndex={0} onKeyDown={event => {if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {event.preventDefault(); event.stopPropagation(); setTimelineHeight(value => Math.max(190, Math.min(500, value + (event.key === 'ArrowUp' ? 20 : -20))));}}} onPointerDown={event => {event.currentTarget.setPointerCapture(event.pointerId); event.currentTarget.dataset.y = String(event.clientY); event.currentTarget.dataset.height = String(timelineHeight);}} onPointerMove={event => {if (event.currentTarget.hasPointerCapture(event.pointerId)) setTimelineHeight(Math.max(190, Math.min(window.innerHeight * .55, Number(event.currentTarget.dataset.height) + Number(event.currentTarget.dataset.y) - event.clientY)));}}><span/></div>
        <header className="timeline-toolbar"><div className="timeline-tools"><button className="icon-button" aria-label="撤销" title="撤销 ⌘/Ctrl Z" disabled={!studio.canUndo} onClick={studio.undo}><Icon name="undo" size={17}/></button><button className="icon-button" aria-label="重做" title="重做 ⌘/Ctrl Shift Z" disabled={!studio.canRedo} onClick={studio.redo}><Icon name="redo" size={17}/></button><span className="tool-divider"/><button className="tool-button" onClick={() => void split()} disabled={busy || (!selectedClip && !selectedSub)} title="在播放头拆分选中的片段或字幕 S"><Icon name="split" size={17}/><span>拆分</span><kbd>S</kbd></button><button className="icon-button" aria-label="移除选中内容" title={magnetic ? '删除并自动合拢 Delete' : '删除并保留空隙 Delete'} disabled={!selectedClip && !selectedSub} onClick={remove}><Icon name="trash" size={17}/></button><span className="tool-divider"/><button className={`tool-button magnetic-button ${magnetic ? 'active' : ''}`} aria-pressed={magnetic} title="启用后视频拖动顺接重排，删除自动合拢；字幕及独立音频需复核" onClick={() => setMagnetic(value => !value)}><Icon name="magnet" size={16}/><span>自动衔接</span></button></div><span className="timeline-current">{formatTime(position, view.fps)}<small> / {formatTime(view.duration_frames, view.fps)}</small></span><div className="timeline-zoom"><button className="icon-button" aria-label="缩小时间线" disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - .5))}><Icon name="minus" size={16}/></button><span>{Math.round(zoom * 100)}%</span><button className="icon-button" aria-label="放大时间线" disabled={zoom >= 8} onClick={() => setZoom(value => Math.min(8, value + .5))}><Icon name="plus" size={16}/></button><button className="icon-button" aria-label="时间线适合全部" title="适合全部" onClick={() => setZoom(1)}><Icon name="fit" size={15}/></button></div></header>
        <div className="timeline-scroll"><div className="timeline-content" style={{width: `${zoom * 100}%`}}><div className="ruler-row"><div className="track-spacer">轨道</div><div ref={ruler} className="time-ruler" role="slider" aria-label="时间线播放头" aria-valuemin={0} aria-valuemax={lastFrame} aria-valuenow={position} tabIndex={0} onPointerDown={event => {event.currentTarget.focus({preventScroll: true}); event.currentTarget.setPointerCapture(event.pointerId); setFrame(frameAt(event.clientX)); setPlaying(false);}} onPointerMove={event => {if (event.currentTarget.hasPointerCapture(event.pointerId)) setFrame(frameAt(event.clientX));}}>{Array.from({length: 11}, (_, index) => <span className="ruler-tick" key={index} style={{left: `${index * 10}%`}}>{formatTime(Math.round(view.duration_frames * index / 10), view.fps)}</span>)}</div></div>
          {view.tracks.map(track => <div className={`timeline-track ${!track.enabled ? 'track-disabled' : ''}`} key={track.id}><div className="track-heading"><Icon name={track.kind === 'video' ? 'film' : track.kind === 'audio' ? 'audio' : 'text'} size={15}/><span>{track.kind === 'video' ? '画面' : track.kind === 'audio' ? '音频' : '字幕'}</span><button className={`track-toggle ${track.enabled ? '' : 'off'}`} aria-label={`${track.kind === 'video' ? '画面' : track.kind === 'audio' ? '音频' : '字幕'}轨道${track.enabled ? '关闭' : '开启'}`} onClick={() => toggleTrack(track.id)}><Icon name="eye" size={14}/></button></div><div className="track-lane" data-track-kind={track.kind} tabIndex={0} role="group" aria-label={`${track.kind === 'video' ? '画面' : track.kind === 'audio' ? '音频' : '字幕'}轨道`} onPointerDown={event => {if (event.target === event.currentTarget) {event.currentTarget.focus({preventScroll: true}); setFrame(frameAt(event.clientX)); setPlaying(false);}}} onDragOver={event => {if (event.dataTransfer.types.includes('application/x-studio-asset')) event.preventDefault();}} onDrop={event => {event.preventDefault(); const asset = assets.find(value => value.id === event.dataTransfer.getData('application/x-studio-asset')); if (!asset) return; const kind = asset.metadata.video_stream_index != null ? 'video' : 'audio'; if (kind !== track.kind) {studio.setError('请把这个素材放到对应的视频或音频轨道。'); return;} addAsset(asset, frameAt(event.clientX));}}>
            {track.kind === 'subtitle' ? view.subtitles.filter(subtitle => subtitle.track_id === track.id).map(subtitle => <button className={`timeline-subtitle ${selectedSub?.id === subtitle.id ? 'selected' : ''}`} key={subtitle.id} data-subtitle-id={subtitle.id} style={{left: `${subtitle.start_frame / view.duration_frames * 100}%`, width: `${(subtitle.end_frame - subtitle.start_frame) / view.duration_frames * 100}%`}} onClick={() => chooseSub(subtitle)} onPointerDown={event => begin(event, subtitle.id, 'subtitle')} onPointerMove={drag} onPointerUp={() => finishGesture()} onPointerCancel={() => finishGesture(true)}><Icon name="text" size={13}/><span>{subtitle.text || '请输入字幕'}</span></button>) : view.clips.filter(clip => clip.track_id === track.id).map((clip, index) => <div role="button" tabIndex={0} aria-label={`选择片段 ${index + 1} ${assets.find(asset => asset.id === clip.source.asset_id)?.filename ?? '缺失素材'}`} className={`timeline-clip ${track.kind} ${selectedClip?.id === clip.id ? 'selected' : ''}`} key={clip.id} data-clip-id={clip.id} style={{left: `${clip.timeline_start_frame / view.duration_frames * 100}%`, width: `${clip.duration_frames / view.duration_frames * 100}%`}} onClick={() => chooseClip(clip)} onKeyDown={event => {if (event.key === 'Enter') chooseClip(clip);}} onPointerDown={event => begin(event, clip.id, 'move')} onPointerMove={drag} onPointerUp={() => finishGesture()} onPointerCancel={() => finishGesture(true)}>
              <span className="trim-handle trim-in" role="button" tabIndex={0} aria-label={`裁剪片段${index + 1}开头`} title="拖动裁剪开头；精确调整请用右侧属性" onPointerDown={event => begin(event, clip.id, 'in')} onPointerMove={drag} onPointerUp={() => finishGesture()} onPointerCancel={() => finishGesture(true)}/><div className="clip-title"><Icon name={track.kind === 'audio' ? 'audio' : 'film'} size={13}/><span>{assets.find(asset => asset.id === clip.source.asset_id)?.filename}</span></div><small>{formatTime(clip.duration_frames, view.fps)}{clip.muted ? ' · 静音' : ''}</small><span className="trim-handle trim-out" role="button" tabIndex={0} aria-label={`裁剪片段${index + 1}结尾`} title="拖动裁剪结尾；精确调整请用右侧属性" onPointerDown={event => begin(event, clip.id, 'out')} onPointerMove={drag} onPointerUp={() => finishGesture()} onPointerCancel={() => finishGesture(true)}/>
            </div>)}{track.kind === 'video' && !view.clips.some(clip => clip.track_id === track.id) && <span className="empty-track">把素材拖到这里开始剪辑</span>}</div></div>)}<div className="playhead-layer"><div className="timeline-playhead" style={{left: `${position / view.duration_frames * 100}%`}}><i/></div></div>
        </div></div><footer className="timeline-status"><span>{status}</span><span>{view.clips.length} 个片段 · {view.subtitles.length} 条字幕{record ? ` · v${record.revision}` : ''}</span></footer>
      </section>
      <nav className="mobile-toolbar" aria-label="移动工作区"><button onClick={() => {setPanel('media'); setMobile(mobile === 'left' && panel === 'media' ? 'none' : 'left');}}><Icon name="film"/>素材</button><button onClick={() => {setPanel('subtitles'); setMobile('left');}}><Icon name="text"/>字幕</button><button onClick={() => setMobile(mobile === 'right' ? 'none' : 'right')}><Icon name="eye"/>属性</button><button onClick={() => {setPanel('exports'); setMobile('left');}}><Icon name="export"/>成片</button></nav>
    </>}
    <input className="file-input" ref={upload} type="file" accept="video/*,audio/*" multiple aria-label="导入视频或音频" onChange={event => {const files = Array.from(event.target.files ?? []); if (files.length) void importFiles(files); event.target.value = '';}}/>
    {modal && <Dialog title={typeof modal === 'string' ? modal === 'projects' ? '项目与剧集' : modal === 'imports' ? '素材导入' : modal === 'analysis' ? '素材对白、镜头与检索' : '剪辑快捷键' : modal.kind === 'source' ? '源素材预览' : `成片预览 · 版本 ${modal.revision}`} onClose={() => {
      if ((modal === 'analysis' && transcriptDraftActive) || (modal === 'projects' && libraryDraftActive)) return;
      setModal(typeof modal === 'object' && modal.kind === 'source' && modal.fromAnalysis ? 'analysis' : null);
    }}>
      {modal === 'projects' && libraryDraftActive && <p role="status" className="help-note">请先保存或取消项目表单，再关闭面板。</p>}
      {modal === 'analysis' && transcriptDraftActive && <p role="status" className="help-note">有未完成的对白编辑或字幕预览，请先保存、应用或取消，再关闭面板。试听后可返回继续校对。</p>}
      {(modal === 'analysis' || (typeof modal === 'object' && modal.kind === 'source' && modal.fromAnalysis)) && <div hidden={modal !== 'analysis'}><AssetAnalysisPanel assets={libraryAssets} onPreview={(asset, start) => openModal({kind: 'source', asset, start, fromAnalysis: true})}
        transcriptTarget={selectedClip ? {clipId: selectedClip.id, assetId: selectedClip.source.asset_id, label: `${selectedClipAsset?.filename ?? '所选片段'} · ${formatTime(selectedClip.timeline_start_frame, view!.fps)}`} : undefined}
        onTranscriptDraftChange={setTranscriptDraftActive}
        onAddTranscript={async (asset, generation, confirmed, mode) => {
          if (gesture.current || pendingRef.current.size) throw new Error('请先结束拖动或应用当前数值输入。');
          if (!selectedClip || selectedClip.source.asset_id !== asset.id) throw new Error('请先选择此素材在时间线上的目标片段。');
          return studio.prepareTranscriptSubtitles(generation.id, generation.revision, selectedClip.id, confirmed.map(segment => segment.id), mode);
        }} onApplyTranscript={async preview => {
          if (gesture.current || pendingRef.current.size) throw new Error('请先结束拖动或应用当前数值输入。');
          const live = liveSelection.current;
          if (live.projectId !== preview.proposal.project_id || live.selection?.kind !== 'clip'
            || live.selection.id !== preview.proposal.clip_id) {
            throw new Error('所选片段已变化，请取消预览并重新生成。');
          }
          return studio.applyTranscriptSubtitles(preview);
        }}/></div>}
      {modal === 'projects' ? <ProjectLibrary currentProjectId={record?.id} onBeforeAction={beforeLibraryAction}
        onDraftChange={setLibraryDraftActive} onChanged={studio.refreshLibrary} onOpen={async value => {
          const opened = await studio.openProject(value.id);
          if (!opened) return false;
          setModal(null); setSelection(null); setFrame(0); setAssetScope('project'); return true;
        }}/>
        : modal === 'imports' ? <ImportQueue queue={importQueue} productionName={studio.libraryContext?.production.name} onAdd={() => upload.current?.click()}/> : modal === 'analysis' ? null : modal === 'help' ? <><p className="muted">导入素材 → 加入时间线 → 播放定位 → 拆分或裁剪 → 导出成片</p><div className="shortcut-grid">{[['空格', '播放 / 暂停'], ['← / →', '前后移动一帧'], ['S', '在播放头拆分选中的片段或字幕'], ['Delete', '移除选中片段或字幕'], ['⌘ / Ctrl Z', '撤销'], ['⌘ / Ctrl ⇧ Z', '重做'], ['⌘ / Ctrl S', '立即保存'], ['Esc', '取消拖动 / 关闭弹窗']].map(([key, label]) => <div key={key}><kbd>{key}</kbd><span>{label}</span></div>)}</div><p className="help-note">原视频和音轨保留在本机；ChatGPT 只接收已明确告知范围的必要抽帧和文字。完整 AI 分析与自动粗剪尚未开放。字幕字体、精确切点和混音以服务端导出为准。</p></> : typeof modal === 'object' ? <><video key={modal.kind === 'source' ? `${modal.asset.id}:${modal.start ?? 0}` : modal.url} className="dialog-video" controls autoPlay onLoadedMetadata={event => {if (modal.kind === 'source' && modal.start !== undefined) event.currentTarget.currentTime = Math.max(0, Math.min(modal.start, event.currentTarget.duration));}} src={modal.kind === 'source' ? mediaUrl(modal.asset.media_url) : modal.url}/><p className="muted">{modal.kind === 'source' ? modal.asset.filename : '这是该保存版本的完整导出成片。'}</p>{modal.kind === 'source' && <button className="primary" onClick={() => {addAsset(modal.asset); setModal(modal.fromAnalysis ? 'analysis' : null);}}>加入时间线</button>}</> : null}
    </Dialog>}
  </main>;
}
