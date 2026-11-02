'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {importFingerprint, imports, transferImport, validateResume, type UploadInput, type UploadState} from './uploads';
import type {Asset} from './types';

export type ImportEntry = {key: string; productionId: string; filename: string; size: number; requestId: string;
  file?: File; input?: UploadInput; session?: UploadState; stage: 'queued' | 'hashing' | 'transferring' | 'paused' | 'failed' | 'finished'; error?: string};
const errorText = (error: unknown) => error instanceof Error ? error.message : '导入未完成，已传内容保留。';

export function useImportQueue(productionId: string | undefined,
  onImported: (asset: Asset, productionId: string) => void, onReconcile: (productionId: string) => Promise<void>) {
  const [entries, setEntries] = useState<ImportEntry[]>([]);
  const [listError, setListError] = useState('');
  const entriesRef = useRef(entries), callback = useRef(onImported);
  const reconcile = useRef(onReconcile);
  const active = useRef(new Map<string, AbortController>());
  const attached = useRef(new Set<string>());
  const mounted = useRef(false);
  const pumpRef = useRef<() => void>(() => {});
  useEffect(() => {callback.current = onImported;}, [onImported]);
  useEffect(() => {reconcile.current = onReconcile;}, [onReconcile]);
  const update = useCallback((change: (value: ImportEntry[]) => ImportEntry[]) => {
    entriesRef.current = change(entriesRef.current);
    if (mounted.current) setEntries(entriesRef.current);
  }, []);
  const patch = useCallback((key: string, value: Partial<ImportEntry>) => update(items => items.map(item => item.key === key ? {...item, ...value} : item)), [update]);
  const publish = useCallback((session: UploadState, target: string) => {
    if (session.status === 'completed' && session.asset && !attached.current.has(session.id)) {
      attached.current.add(session.id); callback.current(session.asset, target);
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!productionId) return;
    try {
      const result = await imports.list(productionId);
      update(items => {
        const next = [...items];
        for (const state of result.uploads) {
          const index = next.findIndex(item => item.session?.id === state.id || item.requestId === state.request_id);
          if (index >= 0) {
            const item = next[index];
            if (active.current.has(item.key) || (item.session && item.session.updated_at > state.updated_at)) continue;
            const terminal = state.status === 'completed' || state.status === 'cancelled';
            next[index] = {...item, session: state, stage: terminal ? 'finished' : item.stage === 'queued' ? 'queued' : state.status === 'failed' ? 'failed' : 'paused',
              error: terminal ? '' : item.error, ...(terminal ? {file: undefined} : {})};
          } else next.push({key: state.id, productionId: state.production_id!, filename: state.filename, size: state.size, requestId: state.request_id,
            session: state, stage: state.status === 'completed' || state.status === 'cancelled' ? 'finished' : state.status === 'failed' ? 'failed' : 'paused'});
        }
        return next;
      });
      // Historical completed records survive library removal. Read current
      // server membership once; never infer it from an old upload receipt.
      await reconcile.current(productionId);
      setListError('');
    } catch (error) {if (mounted.current) setListError(errorText(error));}
  }, [productionId, update]);

  const run = useCallback(async (entry: ImportEntry) => {
    const controller = new AbortController(); active.current.set(entry.key, controller);
    let session = entry.session;
    try {
      if (!entry.file) {
        if (!session || session.status !== 'validating') throw new Error('请重新选择原文件；系统会核对完整指纹并从已传位置继续。');
        patch(entry.key, {stage: 'transferring', error: ''});
        session = await imports.complete(session.id, controller.signal);
        patch(entry.key, {session});
        if (session.status !== 'completed' || !session.asset) throw new Error(session.error?.message || '媒体尚未校验成功。');
      } else {
        patch(entry.key, {stage: 'hashing', error: ''});
        const sha = await importFingerprint(entry.file);
        controller.signal.throwIfAborted();
        if (session) validateResume(entry.file, sha, session);
        else {
          const input = entry.input ?? {request_id: entry.requestId, filename: entry.file.name, size: entry.file.size,
            sha256: sha, last_modified: entry.file.lastModified, production_id: entry.productionId};
          if (input.sha256 !== sha) throw new Error('原文件内容已变化，请取消原记录后重新添加。');
          patch(entry.key, {input});
          session = await imports.start(input, controller.signal);
        }
        patch(entry.key, {session, stage: 'transferring'});
        session = await transferImport(entry.file, session, controller.signal, value => patch(entry.key, {session: value}));
      }
      patch(entry.key, {session, stage: 'finished', error: '', file: undefined});
      publish(session, entry.productionId);
    } catch (error) {
      patch(entry.key, {stage: controller.signal.aborted ? 'paused' : 'failed', error: controller.signal.aborted ? '传输已暂停，已确认分片保留。' : errorText(error)});
    } finally {
      active.current.delete(entry.key);
      if (mounted.current) queueMicrotask(() => pumpRef.current());
    }
  }, [patch, publish]);

  const pump = useCallback(() => {
    if (active.current.size) return;
    const next = entriesRef.current.find(item => item.stage === 'queued');
    if (next) void run(next);
  }, [run]);
  useEffect(() => {pumpRef.current = pump;}, [pump]);
  useEffect(() => {
    mounted.current = true;
    const controllers = active.current;
    return () => {mounted.current = false; for (const controller of controllers.values()) controller.abort();};
  }, []);
  useEffect(() => {
    let stopped = false;
    void Promise.resolve().then(() => {if (!stopped) return refresh();});
    return () => {stopped = true;};
  }, [refresh]);

  const enqueue = useCallback((files: File[], target: string) => {
    update(items => [...items, ...files.map(file => ({key: crypto.randomUUID(), productionId: target, filename: file.name,
      size: file.size, requestId: crypto.randomUUID(), file, stage: 'queued' as const}))]);
    pumpRef.current();
  }, [update]);
  const resume = useCallback((key: string, file?: File) => {
    if (active.current.has(key)) return;
    patch(key, {stage: 'queued', error: '', ...(file ? {file} : {})}); pumpRef.current();
  }, [patch]);
  const pause = useCallback((key: string) => {
    const controller = active.current.get(key);
    if (controller) controller.abort(); else patch(key, {stage: 'paused'});
  }, [patch]);
  const cancel = useCallback(async (key: string) => {
    const entry = entriesRef.current.find(value => value.key === key);
    if (!entry || active.current.has(key)) return;
    try {
      // An empty session field can mean the create response was lost. Resolve
      // its original receipt before cancelling so no server upload is orphaned.
      const session = entry.session ?? (entry.input ? await imports.start(entry.input) : null);
      if (session) {
        const result = await imports.cancel(session.id);
        patch(key, {session: result, stage: 'finished', file: undefined, error: ''});
        publish(result, entry.productionId);
      } else update(items => items.filter(value => value.key !== key));
    } catch (error) {patch(key, {error: errorText(error)});}
  }, [patch, update, publish]);
  return {entries: entries.filter(entry => entry.productionId === productionId), allEntries: entries,
    listError, enqueue, resume, pause, cancel, refresh, activeCount: entries.filter(entry => ['queued', 'hashing', 'transferring'].includes(entry.stage)).length};
}
