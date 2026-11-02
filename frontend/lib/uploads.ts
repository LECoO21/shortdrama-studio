import {request} from './api';
import type {Asset} from './types';

export const MAX_IMPORT_BYTES = 100 * 1024 * 1024;
export type UploadState = {
  id: string; request_id: string; filename: string; size: number; sha256: string;
  last_modified: number; production_id: string | null; offset: number; chunk_size: number;
  status: 'uploading' | 'validating' | 'completed' | 'failed' | 'cancelled';
  asset: Asset | null; duplicate: boolean | null; error: {code: string; message: string} | null;
  created_at: string; updated_at: string;
};
export type UploadInput = {request_id: string; filename: string; size: number; sha256: string; last_modified: number; production_id?: string};

export const imports = {
  list: (productionId: string, signal?: AbortSignal) => request<{uploads: UploadState[]}>(`/uploads?production_id=${encodeURIComponent(productionId)}`, {signal}),
  start: (body: UploadInput, signal?: AbortSignal) => request<UploadState>('/uploads', {method: 'POST', body: JSON.stringify(body), signal}),
  read: (id: string, signal?: AbortSignal) => request<UploadState>(`/uploads/${id}`, {signal}),
  chunk: (id: string, offset: number, body: Blob, signal?: AbortSignal) => request<UploadState>(`/uploads/${id}/chunks?offset=${offset}`, {
    method: 'PUT', body, headers: {'Content-Type': 'application/octet-stream'}, signal,
  }),
  complete: (id: string, signal?: AbortSignal) => request<UploadState>(`/uploads/${id}/complete`, {method: 'POST', body: '{}', signal}),
  retry: (id: string, signal?: AbortSignal) => request<UploadState>(`/uploads/${id}/retry`, {method: 'POST', body: '{}', signal}),
  cancel: (id: string) => request<UploadState>(`/uploads/${id}/cancel`, {method: 'POST', body: '{}'}),
};

export function validateImport(file: Pick<File, 'name' | 'size'>) {
  if (!/\.(mp4|mov|mkv|webm|wav|mp3|m4a)$/i.test(file.name)) throw new Error('支持 MP4、MOV、MKV、WebM、WAV、MP3、M4A；请重新选择视频或音频。');
  if (!file.size) throw new Error('文件为空，请重新选择完整的媒体文件。');
  if (file.size > MAX_IMPORT_BYTES) throw new Error('当前本地工作台单文件上限为 100 MiB，请先压缩或分段。');
}

export async function importFingerprint(file: File) {
  validateImport(file);
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
}

export function validateResume(file: Pick<File, 'size'>, sha256: string, session: UploadState) {
  if (file.size !== session.size || sha256 !== session.sha256) throw new Error('所选文件与这条导入记录的完整指纹不同；已传内容保留，请选择原文件。');
  if (session.status === 'cancelled') throw new Error('这条导入已取消，请将原文件作为新素材重新导入。');
}

export async function transferImport(file: File, initial: UploadState, signal: AbortSignal,
  onState: (value: UploadState) => void, transport = imports): Promise<UploadState> {
  let state = await transport.read(initial.id, signal);
  if (state.sha256 !== initial.sha256 || state.size !== file.size) throw new Error('导入来源发生变化，已停止传输。');
  if (state.status === 'failed') state = await transport.retry(state.id, signal);
  if (state.status === 'cancelled') throw new Error('导入已取消，请重新添加文件。');
  onState(state);
  while (state.status === 'uploading' && state.offset < state.size) {
    signal.throwIfAborted();
    const offset = state.offset;
    state = await transport.chunk(state.id, offset, file.slice(offset, Math.min(state.size, offset + state.chunk_size)), signal);
    if (state.status === 'uploading' && state.offset <= offset) throw new Error('服务器未确认新分片，已暂停；重试时会先读取已传位置。');
    onState(state);
  }
  if (state.status === 'completed') return state;
  if (state.status !== 'uploading' && state.status !== 'validating') throw new Error(state.error?.message || '导入尚未完成，请查看状态后重试。');
  onState({...state, status: 'validating'});
  state = await transport.complete(state.id, signal);
  onState(state);
  if (state.status !== 'completed' || !state.asset) throw new Error(state.error?.message || '媒体尚未校验成功，已传分片保留。');
  return state;
}

export const importStatus = (state: UploadState) => ({
  uploading: '已传部分保留 · 可继续', validating: '正在校验媒体', completed: state.duplicate ? '重复素材 · 已关联' : '导入完成',
  failed: '导入失败 · 可重试', cancelled: '已取消',
})[state.status];
export const formatBytes = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
