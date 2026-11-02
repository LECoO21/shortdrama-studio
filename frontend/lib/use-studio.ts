'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {api, ApiError} from './api';
import type {Asset, Job, ProjectDocument, ProjectRecord, QcReport, Snapshot} from './types';
import type {LibraryContext} from './library';
import type {TranscriptSubtitleMode, TranscriptSubtitlePreview} from './transcripts';
import {segmentSubtitleProposal} from './subtitle-segmentation';

export type SaveState = 'saved' | 'waiting' | 'saving' | 'error' | 'conflict';
type ProjectSummary = {id: string; name: string; revision: number; updated_at: string};
type PendingSave = {projectId: string; revision: number; document: ProjectDocument; signature: string; requestId: string};
const clone = <T,>(value: T): T => structuredClone(value);
const serialize = (value: ProjectDocument | null) => value ? JSON.stringify(value) : '';
const message = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试';

function selectInUrl(id: string) {
  const url = new URL(window.location.href);
  url.searchParams.set('project', id);
  window.history.replaceState(window.history.state, '', url);
}

export function useStudio() {
  const [record, setRecord] = useState<ProjectRecord | null>(null);
  const [doc, setDoc] = useState<ProjectDocument | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [libraryContext, setLibraryContext] = useState<LibraryContext | null>(null);
  const contextRef = useRef<LibraryContext | null>(null);
  const contextSequence = useRef(0);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>('saved');
  const [saved, setSaved] = useState('');
  const [pending, setPending] = useState(false);
  const [online, setOnline] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [qc, setQc] = useState<QcReport | null>(null);
  const [past, setPast] = useState<ProjectDocument[]>([]);
  const [future, setFuture] = useState<ProjectDocument[]>([]);
  const recordRef = useRef<ProjectRecord | null>(null);
  const docRef = useRef<ProjectDocument | null>(null);
  const savedRef = useRef('');
  const pendingSave = useRef<PendingSave | null>(null);
  const saving = useRef<Promise<ProjectRecord | null> | null>(null);
  const retryAfterEdit = useRef(false);
  const jobOperations = useRef(new Set<string>());
  const history = useRef({past: [] as ProjectDocument[], future: [] as ProjectDocument[], group: '', time: 0});
  const tasks = useRef(0);
  const transitioning = useRef(false);
  const loadSequence = useRef(0);
  const snapshotSequence = useRef(0);
  const dirty = doc !== null && (serialize(doc) !== saved || pending);

  const begin = useCallback(() => {tasks.current += 1; setWorking(true);}, []);
  const end = useCallback(() => {tasks.current = Math.max(0, tasks.current - 1); setWorking(tasks.current > 0);}, []);
  const refreshSnapshots = useCallback(async (id: string) => {
    const sequence = ++snapshotSequence.current;
    try {
      const response = await api.snapshots(id);
      if (recordRef.current?.id === id && sequence === snapshotSequence.current) setSnapshots(response.snapshots);
    } catch { /* A saved document remains saved if the history panel cannot refresh. */ }
  }, []);

  const refreshContext = useCallback(async (id: string) => {
    const sequence = ++contextSequence.current;
    const value = await api.context(id);
    if (recordRef.current?.id === id && sequence === contextSequence.current) {
      contextRef.current = value; setLibraryContext(value);
      setAssets(items => [...items.filter(item => !value.assets.some(asset => asset.id === item.id)), ...value.assets]);
    }
    return value;
  }, []);

  const openRecord = useCallback((value: ProjectRecord) => {
    recordRef.current = value;
    docRef.current = clone(value.document);
    savedRef.current = serialize(value.document);
    pendingSave.current = null;
    retryAfterEdit.current = false;
    history.current = {past: [], future: [], group: '', time: 0};
    setRecord(value);
    setDoc(clone(value.document));
    setSaved(savedRef.current);
    setPending(false);
    setPast([]);
    setFuture([]);
    setQc(null);
    setSnapshots([]);
    setSaveState('saved');
    selectInUrl(value.id);
    void refreshSnapshots(value.id);
    contextRef.current = null; setLibraryContext(null);
    void refreshContext(value.id).catch(() => {});
  }, [refreshSnapshots, refreshContext]);

  const refreshProjects = useCallback(async () => {
    const response = await api.projects();
    setProjects(response.projects);
  }, []);

  const refreshJobs = useCallback(async () => {
    try {
      const response = await api.jobs();
      setJobs(response.jobs);
      setOnline(true);
    } catch {
      // Polling is deliberately quiet and recovers on the next regular tick.
      setOnline(false);
    }
  }, []);

  const bootstrap = useCallback(async () => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    try {
      const [state, list] = await Promise.all([api.bootstrap(), api.projects()]);
      const requested = new URL(window.location.href).searchParams.get('project') ?? state.project?.id;
      let selected = state.project;
      let selectionError = '';
      if (requested && requested !== selected?.id) {
        try {
          const [candidate, context] = await Promise.all([api.project(requested), api.context(requested)]);
          if (context.production.archived || context.episode.archived) throw new Error('这个项目或剧集已归档，请在项目管理中恢复。');
          selected = candidate;
        }
        catch (failure) { selectionError = `无法载入上次工程：${message(failure)}`; }
      }
      if (sequence !== loadSequence.current) return;
      setAssets(state.assets);
      setJobs(state.jobs);
      setProjects(list.projects);
      if (selected) openRecord(selected);
      setOnline(true);
      setError(selectionError);
    } catch (failure) {
      if (sequence === loadSequence.current) {
        setOnline(false);
        setError(message(failure));
      }
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, [openRecord]);

  const saveNow = useCallback((): Promise<ProjectRecord | null> => {
    if (saving.current) return saving.current;
    const operation = (async () => {
      try {
        // Resolve an uncertain previous request with its ORIGINAL receipt ID
        // before flushing newer edits. A lost HTTP response cannot duplicate
        // the revision or incorrectly advance this client's CAS revision.
        while (recordRef.current && docRef.current && (pendingSave.current || serialize(docRef.current) !== savedRef.current)) {
          const current = recordRef.current;
          if (!pendingSave.current) {
            const value = clone(docRef.current);
            pendingSave.current = {projectId: current.id, revision: current.revision,
              document: value, signature: serialize(value), requestId: crypto.randomUUID()};
            retryAfterEdit.current = false;
            setPending(true);
          }
          const attempt = pendingSave.current;
          setSaveState('saving');
          const updated = await api.save(attempt.projectId, attempt.document, attempt.revision, attempt.requestId);
          if (recordRef.current?.id !== attempt.projectId) return null;
          recordRef.current = updated;
          savedRef.current = attempt.signature;
          pendingSave.current = null;
          retryAfterEdit.current = false;
          setRecord(updated);
          setSaved(attempt.signature);
          setPending(false);
          setProjects(values => values.map(project => project.id === updated.id
            ? {...project, name: updated.document.name, revision: updated.revision} : project));
          setOnline(true);
          void refreshSnapshots(updated.id);
          void refreshContext(updated.id).catch(() => {});
          // Never setDoc(updated.document): inputs entered during the request
          // live in docRef and are flushed by the next loop iteration.
        }
        setSaveState('saved');
        setError('');
        return recordRef.current;
      } catch (failure) {
        const rejected = failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && failure.status !== 409;
        // A deterministic rejection did not save this body. Let the next edit
        // submit corrected content with a fresh receipt, rather than replaying
        // an invalid request forever. Network / server failures remain unknown
        // and must retry the original receipt; conflicts require a reload.
        if (rejected) {
          pendingSave.current = null;
          setPending(false);
        }
        retryAfterEdit.current = rejected;
        setSaveState(failure instanceof ApiError && failure.status === 409 ? 'conflict' : 'error');
        if (!(failure instanceof ApiError)) setOnline(false);
        setError(message(failure));
        return null;
      }
    })();
    saving.current = operation;
    void operation.finally(() => {if (saving.current === operation) saving.current = null;});
    return operation;
  }, [refreshSnapshots, refreshContext]);

  const commit = useCallback((next: ProjectDocument, groupKey?: string) => {
    const previous = docRef.current;
    if (!previous || transitioning.current || serialize(next) === serialize(previous)) return;
    if (next.project_id !== previous.project_id) {setError('工程标识不一致，修改未应用。'); return;}
    const now = Date.now();
    const grouped = Boolean(groupKey && history.current.group === groupKey && now - history.current.time <= 800);
    const previousHistory = history.current;
    const nextPast = grouped ? previousHistory.past : [...previousHistory.past, clone(previous)].slice(-100);
    history.current = {past: nextPast, future: [], group: groupKey ?? '', time: now};
    docRef.current = clone(next);
    setDoc(docRef.current);
    setPast(nextPast);
    setFuture([]);
    setQc(null);
    setNotice('');
    // Only a corrected deterministic rejection resumes autosave on an edit.
    // Uncertain failures and version conflicts require explicit retry / reload.
    const correctedRejectedInput = retryAfterEdit.current;
    retryAfterEdit.current = false;
    if (correctedRejectedInput) setError('');
    setSaveState(state => state === 'conflict' || state === 'saving' || (state === 'error' && !correctedRejectedInput) ? state : 'waiting');
  }, []);

  const travel = useCallback((direction: 'undo' | 'redo') => {
    const current = docRef.current;
    if (!current || transitioning.current) return;
    const state = history.current;
    const stack = direction === 'undo' ? state.past : state.future;
    const target = stack.at(-1);
    if (!target) return;
    const nextPast = direction === 'undo' ? state.past.slice(0, -1) : [...state.past, clone(current)].slice(-100);
    const nextFuture = direction === 'undo' ? [...state.future, clone(current)].slice(-100) : state.future.slice(0, -1);
    history.current = {past: nextPast, future: nextFuture, group: '', time: 0};
    docRef.current = clone(target);
    setDoc(docRef.current);
    setPast(nextPast);
    setFuture(nextFuture);
    setQc(null);
    const correctedRejectedInput = retryAfterEdit.current;
    retryAfterEdit.current = false;
    if (correctedRejectedInput) setError('');
    setSaveState(value => value === 'conflict' || value === 'saving' || (value === 'error' && !correctedRejectedInput) ? value : 'waiting');
  }, []);
  const undo = useCallback(() => travel('undo'), [travel]);
  const redo = useCallback(() => travel('redo'), [travel]);

  const prepareTranscriptSubtitles = useCallback(async (transcriptId: string, transcriptRevision: number,
    clipId: string, segmentIds: string[], mode: TranscriptSubtitleMode = 'segmented'): Promise<TranscriptSubtitlePreview | string> => {
    begin();
    try {
      const current = await saveNow();
      if (!current) throw new Error('工程尚未保存，请先处理保存状态。');
      // The API normalizes object key order. Compare the live client snapshot,
      // whose signature was acknowledged by saveNow, rather than response order.
      const signature = serialize(docRef.current);
      if (signature !== savedRef.current || pendingSave.current) throw new Error('工程仍有未保存修改，请保存后重试。');
      const proposal = await api.subtitleProposal(current.id, current.revision,
        transcriptId, transcriptRevision, clipId, segmentIds);
      if (recordRef.current?.id !== current.id || recordRef.current.revision !== current.revision
        || serialize(docRef.current) !== signature || proposal.base_revision !== current.revision) {
        throw new Error('生成字幕期间工程已修改，本次候选未应用，请重新加入。');
      }
      if (!proposal.added_ids.length) {
        return proposal.skipped_count ? '这些句子已经加入此片段，保留现有字幕修改。' : '已确认句子不在所选片段的源范围内。';
      }
      return {proposal: mode === 'segmented' ? segmentSubtitleProposal(proposal) : proposal, baseline_signature: signature,
        original_count: proposal.added_ids.length, segment_ids: [...segmentIds], mode};
    } finally {end();}
  }, [begin, end, saveNow]);

  const applyTranscriptSubtitles = useCallback(async (preview: TranscriptSubtitlePreview): Promise<string> => {
    begin();
    try {
      const proposal = preview.proposal;
      const assertCurrent = () => {
        if (transitioning.current || recordRef.current?.id !== proposal.project_id
          || recordRef.current.revision !== proposal.base_revision || pendingSave.current
          || serialize(docRef.current) !== preview.baseline_signature
          || savedRef.current !== preview.baseline_signature) {
          throw new Error('工程已变化，这份字幕预览已失效。请取消预览并重新生成。');
        }
      };
      assertCurrent();
      // Revalidate the saved transcript and project immediately before the one
      // normal engineering transaction; this local read performs no inference.
      const fresh = await api.subtitleProposal(proposal.project_id,
        proposal.base_revision, proposal.transcript_id, proposal.transcript_revision,
        proposal.clip_id, preview.segment_ids);
      const checked = preview.mode === 'segmented' ? segmentSubtitleProposal(fresh) : fresh;
      assertCurrent();
      if (JSON.stringify(checked) !== JSON.stringify(proposal)) {
        throw new Error('字幕来源或分句结果已变化，请取消预览并重新生成。');
      }
      commit(checked.document);
      const persisted = await saveNow();
      const text = `已加入 ${checked.added_ids.length} 条字幕，可一次撤销。`
        + (checked.outside_count ? ` ${checked.outside_count} 句在片段范围外，未加入。` : '')
        + (!persisted ? ' 本页修改尚未保存成功，请处理顶栏保存状态。' : '');
      setNotice(text);
      return text;
    } finally {end();}
  }, [begin, commit, end, saveNow]);

  const openProject = useCallback(async (id: string): Promise<ProjectRecord | null> => {
    begin();
    try {
      if (recordRef.current && !await saveNow()) return null;
      transitioning.current = true;
      const sequence = ++loadSequence.current;
      const [value, context] = await Promise.all([api.project(id), api.context(id)]);
      if (context.production.archived || context.episode.archived) throw new Error('请先在项目管理中恢复归档的项目或剧集。');
      if (sequence !== loadSequence.current) return null;
      openRecord(value);
      setError('');
      setNotice('');
      setOnline(true);
      return value;
    } catch (failure) {setError(message(failure)); return null;}
    finally {transitioning.current = false; end();}
  }, [begin, end, openRecord, saveNow]);

  const refreshLibrary = useCallback(async () => {
    const id = recordRef.current?.id;
    await refreshProjects();
    const state = await api.bootstrap();
    setAssets(state.assets); setJobs(state.jobs);
    if (!id || recordRef.current?.id !== id) return;
    const context = await refreshContext(id);
    if (recordRef.current?.id !== id) return;
    if (context.production.archived || context.episode.archived) {
      if (pendingSave.current || serialize(docRef.current) !== savedRef.current) {
        setError('工程已归档，本页尚有未保存修改。请恢复后保存；当前内容仍保留。');
        return;
      }
      recordRef.current = null; docRef.current = null; savedRef.current = '';
      contextRef.current = null; setLibraryContext(null); setRecord(null); setDoc(null);
      setSaved(''); setPast([]); setFuture([]); setSnapshots([]); setQc(null);
      history.current = {past: [], future: [], group: '', time: 0};
      const url = new URL(window.location.href); url.searchParams.delete('project');
      window.history.replaceState(window.history.state, '', url);
      setNotice('已归档，原素材和工程历史保留。可在项目管理中恢复或打开其他剧集。');
      return;
    }
    const latest = await api.project(id);
    if (recordRef.current?.id === id && !pendingSave.current && serialize(docRef.current) === savedRef.current
      && latest.revision !== recordRef.current.revision) openRecord(latest);
  }, [openRecord, refreshContext, refreshProjects]);

  const receiveImportedAsset = useCallback((asset: Asset, productionId: string) => {
    setAssets(items => [...items.filter(value => value.id !== asset.id), asset]);
    const id = recordRef.current?.id;
    if (id && contextRef.current?.production.id === productionId) void refreshContext(id).catch(() => {});
  }, [refreshContext]);

  const reconcileImports = useCallback(async (productionId: string) => {
    const id = recordRef.current?.id;
    if (id && contextRef.current?.production.id === productionId) await refreshContext(id);
  }, [refreshContext]);

  const createProject = useCallback(async (name?: string): Promise<ProjectRecord | null> => {
    begin();
    try {
      if (recordRef.current && !await saveNow()) return null;
      transitioning.current = true;
      ++loadSequence.current;
      const created = await api.createProject(name);
      openRecord(created);
      await refreshProjects();
      setOnline(true);
      setError('');
      setNotice('已创建空白工程，可以导入素材开始剪辑。');
      return created;
    } catch (failure) {setError(message(failure)); return null;}
    finally {transitioning.current = false; end();}
  }, [begin, end, openRecord, refreshProjects, saveNow]);

  const ensureProduction = useCallback(async () => {
    const current = recordRef.current ?? await createProject();
    if (!current) throw new Error('请先创建或打开一个剧集。');
    const context = await refreshContext(current.id);
    if (context.production.archived || context.episode.archived) throw new Error('项目或剧集已归档，请先恢复再导入。');
    return context.production.id;
  }, [createProject, refreshContext]);

  const createDemo = useCallback(async (): Promise<ProjectRecord | null> => {
    begin();
    try {
      if (recordRef.current && !await saveNow()) return null;
      transitioning.current = true;
      ++loadSequence.current;
      const demo = await api.demo();
      const [value, state] = await Promise.all([api.project(demo.project_id), api.bootstrap()]);
      setAssets(state.assets);
      setJobs(state.jobs);
      openRecord(value);
      await refreshProjects();
      setOnline(true);
      setError('');
      setNotice('演示工程已就绪，可直接播放或剪辑。');
      return value;
    } catch (failure) {setError(message(failure)); return null;}
    finally {transitioning.current = false; end();}
  }, [begin, end, openRecord, refreshProjects, saveNow]);

  const checkProject = useCallback(async (): Promise<QcReport | null> => {
    begin();
    try {
      const current = await saveNow();
      if (!current) return null;
      const checkedSignature = serialize(current.document);
      const report = await api.qc(current.id);
      if (recordRef.current?.id !== current.id) return null;
      if (recordRef.current.revision !== current.revision || report.revision !== current.revision
        || serialize(docRef.current) !== checkedSignature || savedRef.current !== checkedSignature || pendingSave.current) {
        setNotice('检查期间工程已修改，请重新检查当前版本。');
        return null;
      }
      setQc(report);
      return report;
    } catch (failure) {setError(message(failure)); return null;}
    finally {end();}
  }, [begin, end, saveNow]);

  const exportProject = useCallback(async (): Promise<Job | null> => {
    begin();
    setError('');
    try {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const current = await saveNow();
        if (!current) return null;
        const report = await api.qc(current.id);
        if (recordRef.current?.id !== current.id) return null;
        // Edits made while QC was in flight must be saved and checked too.
        if (serialize(docRef.current) !== savedRef.current || report.revision !== recordRef.current.revision) continue;
        setQc(report);
        if (!report.valid) {
          setError(`导出前发现 ${report.issues.length} 项问题，请按检查结果调整工程。`);
          return null;
        }
        const job = await api.render(current.id, current.revision);
        setJobs(values => [job, ...values.filter(value => value.id !== job.id)]);
        setNotice('已开始后台导出，完成后可预览和下载成片。');
        return job;
      }
      setNotice('工程仍在持续修改，请结束当前调整后再次导出。');
      return null;
    } catch (failure) {setError(message(failure)); return null;}
    finally {end();}
  }, [begin, end, saveNow]);

  const restore = useCallback(async (revision: number) => {
    begin();
    try {
      const current = await saveNow();
      if (!current) return;
      transitioning.current = true;
      const restored = await api.restore(current.id, revision, current.revision);
      openRecord(restored);
      await refreshProjects();
      setError('');
      setNotice(`已恢复历史版本；恢复前的修改也已保存，可在版本历史中找回。`);
    } catch (failure) {setError(message(failure));}
    finally {transitioning.current = false; end();}
  }, [begin, end, openRecord, refreshProjects, saveNow]);

  const cancelJob = useCallback(async (id: string) => {
    if (jobOperations.current.has(id)) return;
    jobOperations.current.add(id);
    try {await api.cancel(id); await refreshJobs();}
    catch (failure) {setError(message(failure));}
    finally {jobOperations.current.delete(id);}
  }, [refreshJobs]);
  const retryJob = useCallback(async (id: string) => {
    if (jobOperations.current.has(id)) return;
    jobOperations.current.add(id);
    try {
      const job = await api.retry(id);
      setJobs(values => [job, ...values.filter(value => value.id !== job.id)]);
      setNotice('已创建新的导出任务。');
    } catch (failure) {setError(message(failure));}
    finally {jobOperations.current.delete(id);}
  }, []);

  useEffect(() => {
    let active = true;
    const invalidateLoad = () => {++loadSequence.current;};
    void Promise.resolve().then(() => {if (active) return bootstrap();});
    return () => {active = false; invalidateLoad();};
  }, [bootstrap]);
  useEffect(() => {
    const timer = window.setInterval(() => {void refreshJobs();}, 2500);
    return () => window.clearInterval(timer);
  }, [refreshJobs]);
  useEffect(() => {
    if (!dirty || saveState === 'saving' || saveState === 'error' || saveState === 'conflict' || loading) return;
    const timer = window.setTimeout(() => {void saveNow();}, 1000);
    return () => window.clearTimeout(timer);
  }, [dirty, doc, saveState, loading, saveNow]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (pendingSave.current || serialize(docRef.current) !== savedRef.current) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);

  return {record, doc, assets, jobs, snapshots, projects, libraryContext, loading, working, saveState, dirty, online, error, notice,
    setError, setNotice, bootstrap, openProject, createProject, createDemo, commit, undo, redo,
    canUndo: past.length > 0, canRedo: future.length > 0, saveNow, exportProject, restore, checkProject, qc,
    refreshJobs, cancelJob, retryJob, prepareTranscriptSubtitles, applyTranscriptSubtitles, refreshLibrary, ensureProduction, receiveImportedAsset, reconcileImports};
}

export default useStudio;
