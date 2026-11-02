'use client';

import {useEffect, useRef, useState} from 'react';
import {api} from '../lib/api';
import {
  canvasPresets, filterProductions, fpsPresets, library, libraryDuration, libraryFps,
  productionDraft, validateLibraryName, validateProductionDraft,
  type EpisodeSummary, type ProductionDetail, type ProductionDraft, type ProductionResult, type ProductionSummary,
} from '../lib/library';
import type {ProjectRecord} from '../lib/types';
import styles from './ProjectLibrary.module.css';

export type ProjectLibraryProps = {
  currentProjectId?: string;
  onBeforeAction: () => Promise<boolean>;
  onOpen: (project: ProjectRecord) => Promise<boolean | void> | boolean | void;
  onChanged?: () => Promise<void> | void;
  onDraftChange?: (active: boolean) => void;
};
type MetadataForm = {kind: 'newProduction' | 'editProduction'; draft: ProductionDraft; initial: ProductionDraft; production?: ProductionDetail};
type NameForm = {kind: 'copyProduction' | 'newEpisode' | 'renameEpisode' | 'copyEpisode'; name: string; initial: string; production: ProductionDetail; episode?: EpisodeSummary};
type Form = MetadataForm | NameForm;
const message = (failure: unknown) => failure instanceof Error ? failure.message : '操作未完成，请重试。';
const updated = (value: string) => new Date(value).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
const formTitle = {newProduction: '新建项目', editProduction: '编辑项目信息', copyProduction: '复制整个项目', newEpisode: '新建剧集', renameEpisode: '重命名剧集', copyEpisode: '复制剧集'};

export default function ProjectLibrary({currentProjectId, onBeforeAction, onOpen, onChanged, onDraftChange}: ProjectLibraryProps) {
  const [productions, setProductions] = useState<ProductionSummary[]>([]);
  const [detail, setDetail] = useState<ProductionDetail | null>(null);
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState('');
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [query, setQuery] = useState(''), [filter, setFilter] = useState<'active' | 'archived' | 'all'>('active');
  const [showArchivedEpisodes, setShowArchivedEpisodes] = useState(false);
  const [form, setForm] = useState<Form | null>(null);
  const [created, setCreated] = useState<ProjectRecord | null>(null);
  const busyRef = useRef(false), mounted = useRef(false), readNumber = useRef(0);
  const initialProjectId = useRef(currentProjectId);
  // A retry with the same submitted form reuses its request receipt, including
  // when the first response was lost after the server committed the operation.
  const receipt = useRef<{key: string; id: string; sourceEpisode?: EpisodeSummary} | null>(null);
  const dirty = !!form && ('draft' in form ? JSON.stringify(form.draft) !== JSON.stringify(form.initial) : form.name !== form.initial);
  const protectedDraft = dirty || !!busy;
  const blocked = !!busy || !!form;

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    async function load() {
      try {
        const result = await library.list(controller.signal);
        let selected: ProductionDetail | null = null;
        if (initialProjectId.current) selected = (await library.context(initialProjectId.current, controller.signal)).production;
        else if (result.productions.length) selected = await library.production((result.productions.find(item => !item.archived) ?? result.productions[0]).id, controller.signal);
        if (controller.signal.aborted) return;
        setProductions(result.productions); setDetail(selected);
        if (selected?.archived) setFilter('all');
      } catch (failure) {if (!controller.signal.aborted) setError(message(failure));}
      finally {if (!controller.signal.aborted) setLoading(false);}
    }
    void load();
    return () => {mounted.current = false; controller.abort();};
  }, []);

  useEffect(() => {onDraftChange?.(protectedDraft); return () => onDraftChange?.(false);}, [onDraftChange, protectedDraft]);
  useEffect(() => {
    if (!protectedDraft) return;
    const protect = (event: BeforeUnloadEvent) => {event.preventDefault(); event.returnValue = '';};
    window.addEventListener('beforeunload', protect);
    return () => window.removeEventListener('beforeunload', protect);
  }, [protectedDraft]);

  function applyProduction(value: ProductionDetail) {
    setDetail(value);
    setProductions(previous => [value, ...previous.filter(item => item.id !== value.id)]);
    if (value.archived) setFilter('all');
  }
  async function selectProduction(id: string) {
    if (blocked) return;
    const requestNumber = ++readNumber.current;
    setLoading(true); setError(''); setNotice(''); setCreated(null);
    try {
      const value = await library.production(id);
      if (mounted.current && requestNumber === readNumber.current) setDetail(value);
    } catch (failure) {if (mounted.current && requestNumber === readNumber.current) setError(message(failure));}
    finally {if (mounted.current && requestNumber === readNumber.current) setLoading(false);}
  }
  async function refresh() {
    if (blocked || loading) return;
    ++readNumber.current;
    setLoading(true); setError('');
    try {
      const result = await library.list();
      const selected = detail ? await library.production(detail.id) : result.productions[0] ? await library.production(result.productions[0].id) : null;
      if (!mounted.current) return;
      setProductions(result.productions); setDetail(selected);
    } catch (failure) {if (mounted.current) setError(message(failure));}
    finally {if (mounted.current) setLoading(false);}
  }
  function startForm(value: Form) {if (blocked || loading) return; setForm(value); setError(''); setNotice(''); setCreated(null); receipt.current = null;}
  function metadataForm(kind: 'newProduction' | 'editProduction') {
    const draft = productionDraft(kind === 'editProduction' ? detail ?? undefined : undefined);
    startForm({kind, draft, initial: {...draft}, production: kind === 'editProduction' ? detail ?? undefined : undefined});
  }
  function nameForm(kind: NameForm['kind'], episode?: EpisodeSummary) {
    if (!detail) return;
    const name = kind === 'copyProduction' ? `${detail.name} 副本`.slice(0, 200) : kind === 'newEpisode' ? ''
      : kind === 'copyEpisode' ? `${episode?.name ?? '剧集'} 副本`.slice(0, 200) : episode?.name ?? '';
    startForm({kind, name, initial: name, production: detail, episode});
  }
  function patchDraft(patch: Partial<ProductionDraft>) {setForm(previous => previous && 'draft' in previous ? {...previous, draft: {...previous.draft, ...patch}} : previous);}
  async function execute(label: string, key: string, operation: (requestId: string) => Promise<ProductionDetail | ProductionResult>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(label); setError(''); setNotice('');
    try {
      if (!await onBeforeAction()) {setError('当前剪辑尚未保存。请先处理工作台的保存提示，再重试。'); return;}
      if (receipt.current?.key !== key) receipt.current = {key, id: crypto.randomUUID()};
      const value = await operation(receipt.current.id);
      if (!mounted.current) return;
      const production = 'production' in value ? value.production : value;
      applyProduction(production); setForm(null); receipt.current = null;
      setCreated('project' in value && ['新建项目', '复制项目', '新建剧集', '复制剧集'].includes(label) ? value.project : null);
      setNotice(`${label}已保存。`);
      try {await onChanged?.();} catch (failure) {if (mounted.current) setError(`操作已保存，但工作台刷新未完成：${message(failure)}`);}
    } catch (failure) {if (mounted.current) setError(message(failure));}
    finally {busyRef.current = false; if (mounted.current) setBusy('');}
  }
  async function freshEpisode(productionId: string, episode: EpisodeSummary, requestId: string): Promise<EpisodeSummary> {
    // Saving the current editor may advance Document revision. Keep the form's
    // metadata revision for CAS, but copy/rename the freshly saved Document.
    if (receipt.current?.id === requestId && receipt.current.sourceEpisode?.project_id === episode.project_id) return receipt.current.sourceEpisode;
    const current = (await library.production(productionId)).episodes.find(item => item.project_id === episode.project_id);
    if (!current) throw new Error('此剧集已不在当前项目中，请取消编辑并刷新列表。');
    const source = {...episode, document_revision: current.document_revision};
    if (receipt.current?.id === requestId) receipt.current.sourceEpisode = source;
    return source;
  }
  function submitForm() {
    if (!form || busyRef.current) return;
    try {
      const key = JSON.stringify(form);
      if ('draft' in form) {
        const metadata = validateProductionDraft(form.draft);
        if (form.kind === 'newProduction') void execute('新建项目', key, id => library.create(metadata, id));
        else if (form.production) void execute('编辑项目信息', key, id => library.update(form.production!.id, form.production!.revision, metadata, id));
      } else {
        const name = validateLibraryName(form.name);
        if (form.kind === 'copyProduction') void execute('复制项目', key, id => library.copy(form.production.id, form.production.revision, name, id));
        else if (form.kind === 'newEpisode') void execute('新建剧集', key, id => library.createEpisode(form.production.id, name, id));
        else if (form.episode) {
          const episode = form.episode;
          if (form.kind === 'renameEpisode') void execute('重命名剧集', key, async id => library.updateEpisode(await freshEpisode(form.production.id, episode, id), {name}, id));
          else void execute('复制剧集', key, async id => library.copyEpisode(await freshEpisode(form.production.id, episode, id), name, id));
        }
      }
    } catch (failure) {setError(message(failure));}
  }
  function archiveProduction() {
    if (!detail) return;
    const value = !detail.archived;
    void execute(value ? '归档项目' : '恢复项目', `production-archive:${detail.id}:${detail.revision}:${value}`, id => library.update(detail.id, detail.revision, {archived: value}, id));
  }
  function archiveEpisode(episode: EpisodeSummary) {
    void execute(episode.archived ? '恢复剧集' : '归档剧集', `episode-archive:${episode.project_id}:${episode.revision}:${!episode.archived}`,
      id => library.updateEpisode(episode, {archived: !episode.archived}, id));
  }
  async function openEpisode(projectId: string) {
    if (busyRef.current || form) return;
    busyRef.current = true; setBusy('打开剧集'); setError('');
    try {
      if (!await onBeforeAction()) {setError('当前剪辑尚未保存，请先处理保存提示。'); return;}
      const value = await api.project(projectId);
      await onOpen(value);
    } catch (failure) {if (mounted.current) setError(message(failure));}
    finally {busyRef.current = false; if (mounted.current) setBusy('');}
  }
  const visible = filterProductions(productions, query, filter);
  const episodes = detail?.episodes.filter(episode => showArchivedEpisodes || !episode.archived) ?? [];
  const createdArchived = !!(detail?.archived || detail?.episodes.find(episode => episode.project_id === created?.id)?.archived);

  return <section className={styles.library} aria-label="项目与剧集管理" aria-busy={!!busy || loading}>
    <div className={styles.intro}><div><strong>一个项目，管理一部短剧</strong><p>项目里存放多集剪辑。选择剧集后，打开时间线继续编辑。</p></div><button className={styles.primary} disabled={blocked || loading} onClick={() => metadataForm('newProduction')}>＋ 新建项目</button></div>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {notice && <p className={styles.notice} role="status">{notice}</p>}
    {busy && <p className={styles.hint} role="status">正在{busy}，请稍候…</p>}
    {created && <div className={styles.created}><span>「{created.document.name}」已保存。{createdArchived ? '恢复此剧集后可打开剪辑。' : ''}</span><button className={styles.primary} disabled={blocked || createdArchived} onClick={() => void openEpisode(created.id)}>打开此剧集</button></div>}
    {form && <form className={styles.form} onSubmit={event => {event.preventDefault(); submitForm();}}>
      <header><h3>{formTitle[form.kind]}</h3><span>填写后保存；取消编辑会丢弃这次表单。</span></header>
      {'draft' in form ? <>
        <label>项目名称<input autoFocus aria-label="项目管理名称" value={form.draft.name} maxLength={200} disabled={!!busy} onChange={event => patchDraft({name: event.target.value})} required/></label>
        <label>项目简介<textarea aria-label="项目简介" value={form.draft.description} rows={3} maxLength={5000} disabled={!!busy} onChange={event => patchDraft({description: event.target.value})}/></label>
        <label>主要语言<input aria-label="项目主要语言" value={form.draft.language} maxLength={50} list="production-languages" disabled={!!busy} onChange={event => patchDraft({language: event.target.value})} required/><datalist id="production-languages"><option value="zh-CN">中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></datalist></label>
        <fieldset disabled={!!busy}><legend>新剧集的默认画幅与帧率</legend><p className={styles.hint}>只用于之后创建的剧集；已有剧集的画幅、帧率和剪辑切点保持原样。</p>
          <div className={styles.presets}>{canvasPresets.map(preset => <button type="button" key={preset.label} aria-pressed={form.draft.width === String(preset.width) && form.draft.height === String(preset.height)} onClick={() => patchDraft({width: String(preset.width), height: String(preset.height)})}>{preset.label}</button>)}</div>
          <div className={styles.dimensions}><label>宽（像素）<input aria-label="默认画幅宽度" type="number" min={64} max={1920} step={2} value={form.draft.width} onChange={event => patchDraft({width: event.target.value})} required/></label><span>×</span><label>高（像素）<input aria-label="默认画幅高度" type="number" min={64} max={1920} step={2} value={form.draft.height} onChange={event => patchDraft({height: event.target.value})} required/></label><label>帧率<select aria-label="默认帧率" value={form.draft.fps} onChange={event => patchDraft({fps: event.target.value})}>{!fpsPresets.some(item => item.value === form.draft.fps) && <option value={form.draft.fps}>沿用 {form.draft.fps} fps</option>}{fpsPresets.map(item => <option value={item.value} key={item.value}>{item.label}</option>)}</select></label></div>
        </fieldset>
      </> : <>
        <label>{form.kind === 'copyProduction' ? '副本项目名称' : '剧集名称'}<input autoFocus aria-label={form.kind === 'copyProduction' ? '副本项目名称' : '剧集名称'} value={form.name} placeholder={form.kind === 'newEpisode' ? '例如 第 2 集' : undefined} maxLength={200} disabled={!!busy} onChange={event => {const name = event.target.value; setForm(previous => previous && 'name' in previous ? {...previous, name} : previous);}} required/></label>
        {form.kind === 'copyProduction' && <p className={styles.hint}>复制各集剪辑与项目资料，共用原素材文件。副本的后续剪辑独立保存。</p>}
        {form.kind === 'copyEpisode' && <p className={styles.hint}>复制当前已保存的剪辑，共用原素材文件。副本的后续剪辑独立保存。</p>}
        {form.kind === 'newEpisode' && <p className={styles.hint}>使用项目默认设置：{form.production.canvas.width} × {form.production.canvas.height} · {libraryFps(form.production.fps)}</p>}
      </>}
      <footer><button type="button" disabled={!!busy} onClick={() => {setForm(null); receipt.current = null; setError('');}}>取消编辑</button><button className={styles.primary} disabled={!!busy} type="submit">{busy ? '保存中…' : '保存'}</button></footer>
    </form>}
    <div className={styles.columns}>
      <aside className={styles.projects} aria-label="项目列表">
        <div className={styles.filters}><input type="search" aria-label="搜索项目" placeholder="搜索项目名称或简介" value={query} onChange={event => setQuery(event.target.value)} disabled={blocked}/><div><select aria-label="项目归档筛选" value={filter} disabled={blocked} onChange={event => setFilter(event.target.value as typeof filter)}><option value="active">进行中的项目</option><option value="archived">已归档项目</option><option value="all">全部项目</option></select><button disabled={blocked || loading} onClick={() => void refresh()}>刷新</button></div></div>
        <div className={styles.projectList}>{visible.map(item => <button key={item.id} disabled={blocked || loading} className={detail?.id === item.id ? styles.selectedProject : undefined} aria-pressed={detail?.id === item.id} onClick={() => void selectProduction(item.id)}><strong>{item.name}</strong><span>{item.active_episode_count} 集进行中 · {item.asset_count} 份素材{item.archived ? ' · 已归档' : ''}</span><small>{updated(item.updated_at)}</small></button>)}</div>
        {!loading && !visible.length && <p className={styles.empty}>{productions.length ? '没有符合条件的项目。试试其他名称或归档筛选。' : '还没有项目，点击“新建项目”开始。'}</p>}
      </aside>
      <section className={styles.details} aria-label="项目详情与剧集列表">
        {loading ? <p className={styles.empty} role="status">正在读取项目与剧集…</p> : detail ? <>
          <header className={styles.productionHeading}><div><h3>{detail.name}</h3><span>{detail.language} · {detail.episode_count} 集 · {detail.asset_count} 份素材{detail.archived ? ' · 已归档' : ''}</span></div><div className={styles.actions}><button disabled={blocked || detail.archived} onClick={() => metadataForm('editProduction')}>编辑信息</button><button disabled={blocked || detail.archived} onClick={() => nameForm('copyProduction')}>复制项目</button><button disabled={blocked} onClick={archiveProduction}>{detail.archived ? '恢复项目' : '归档项目'}</button></div></header>
          {detail.description && <p className={styles.description}>{detail.description}</p>}
          <p className={styles.defaultNote}>新剧集默认：{detail.canvas.width} × {detail.canvas.height} · {libraryFps(detail.fps)}</p>
          <div className={styles.episodeHeading}><h4>剧集</h4><label><input type="checkbox" checked={showArchivedEpisodes} disabled={blocked} onChange={event => setShowArchivedEpisodes(event.target.checked)}/>显示已归档</label><button className={styles.primary} disabled={blocked || detail.archived} onClick={() => nameForm('newEpisode')}>＋ 新建剧集</button></div>
          {detail.archived && <p className={styles.archiveNote}>项目已归档。恢复项目后可打开剪辑、新建或复制剧集。</p>}
          <div className={styles.episodeList}>{episodes.map(episode => <article key={episode.project_id} className={styles.episode} data-current={episode.project_id === currentProjectId}>
            <div className={styles.episodeInfo}><div><strong>{episode.name}</strong>{episode.project_id === currentProjectId && <b>正在编辑</b>}{episode.archived && <em>已归档</em>}</div><p>{libraryDuration(episode.duration_frames, episode.fps)} · {episode.canvas.width} × {episode.canvas.height} · {libraryFps(episode.fps)}</p><small>保存修订 {episode.document_revision} · {updated(episode.updated_at)}</small></div>
            <div className={styles.actions}><button className={styles.primary} disabled={blocked || detail.archived || episode.archived} title={detail.archived || episode.archived ? '恢复项目与剧集后可打开剪辑' : undefined} onClick={() => void openEpisode(episode.project_id)}>打开剪辑</button><button disabled={blocked || detail.archived || episode.archived} onClick={() => nameForm('renameEpisode', episode)}>重命名</button><button disabled={blocked || detail.archived || episode.archived} onClick={() => nameForm('copyEpisode', episode)}>复制</button><button disabled={blocked || detail.archived} onClick={() => archiveEpisode(episode)}>{episode.archived ? '恢复' : '归档'}</button></div>
          </article>)}</div>
          {!episodes.length && <p className={styles.empty}>{detail.episodes.length ? '当前剧集已归档，勾选“显示已归档”可查看和恢复。' : '还没有剧集。点击“新建剧集”创建空白剪辑。'}</p>}
        </> : <p className={styles.empty}>选择一个项目，查看它的剧集。</p>}
      </section>
    </div>
    <p className={styles.footer}>归档可随时恢复，素材文件与已保存剪辑会保留。项目管理不提供永久删除。</p>
  </section>;
}
