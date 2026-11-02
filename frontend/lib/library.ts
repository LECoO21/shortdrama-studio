import {API_BASE, ApiError} from './api';
import type {Asset, ProjectRecord, Rational} from './types';

export type Canvas = {width: number; height: number};
export type ProductionSummary = {
  id: string; name: string; description: string; language: string; canvas: Canvas; fps: Rational;
  revision: number; archived: boolean; created_at: string; updated_at: string;
  episode_count: number; active_episode_count: number; asset_count: number;
};
export type EpisodeSummary = {
  project_id: string; production_id: string; name: string; revision: number; document_revision: number;
  archived: boolean; canvas: Canvas; fps: Rational; duration_frames: number; created_at: string; updated_at: string;
};
export type ProductionDetail = ProductionSummary & {episodes: EpisodeSummary[]};
export type LibraryContext = {production: ProductionDetail; episode: EpisodeSummary; assets: Asset[]};
export type ProductionResult = {production: ProductionDetail; project: ProjectRecord};
export type EpisodeResult = ProductionResult & {episode: EpisodeSummary};
export type ProductionMetadata = {name: string; description: string; language: string; canvas: Canvas; fps: Rational};
export type ProductionDraft = {name: string; description: string; language: string; width: string; height: string; fps: string};

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...options, cache: 'no-store',
    headers: {'X-Studio-Client': '1', 'Content-Type': 'application/json', ...options.headers},
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const details = Array.isArray(body?.error?.details) ? body.error.details
      .map((item: {message?: string; msg?: string}) => item.message ?? item.msg).filter(Boolean).join('；') : '';
    throw new ApiError(response.status, body?.error?.code ?? 'HTTP_ERROR',
      (body?.error?.message ?? `请求失败（${response.status}）`) + (details ? `：${details}` : ''));
  }
  return body as T;
}
const write = <T>(path: string, method: 'POST' | 'PATCH', body: unknown) => request<T>(path, {method, body: JSON.stringify(body)});
const pathId = encodeURIComponent;

export const library = {
  list: (signal?: AbortSignal) => request<{productions: ProductionSummary[]}>('/productions?include_archived=true', {signal}),
  production: (id: string, signal?: AbortSignal) => request<ProductionDetail>(`/productions/${pathId(id)}`, {signal}),
  context: (projectId: string, signal?: AbortSignal) => request<LibraryContext>(`/library/context/${pathId(projectId)}`, {signal}),
  create: (metadata: ProductionMetadata, requestId: string) => write<ProductionResult>('/productions', 'POST', {request_id: requestId, ...metadata}),
  update: (id: string, revision: number, changes: Partial<ProductionMetadata> & {archived?: boolean}, requestId: string) =>
    write<ProductionDetail>(`/productions/${pathId(id)}`, 'PATCH', {request_id: requestId, expected_revision: revision, ...changes}),
  copy: (id: string, revision: number, name: string, requestId: string) => write<ProductionResult>(`/productions/${pathId(id)}/copy`, 'POST', {request_id: requestId, expected_revision: revision, name}),
  createEpisode: (id: string, name: string, requestId: string) => write<ProductionResult>(`/productions/${pathId(id)}/episodes`, 'POST', {request_id: requestId, name}),
  updateEpisode: (episode: EpisodeSummary, changes: {name?: string; archived?: boolean}, requestId: string) =>
    write<EpisodeResult>(`/episodes/${pathId(episode.project_id)}`, 'PATCH', {
      request_id: requestId, expected_revision: episode.revision,
      ...(changes.name === undefined ? {} : {expected_document_revision: episode.document_revision}), ...changes,
    }),
  copyEpisode: (episode: EpisodeSummary, name: string, requestId: string) => write<ProductionResult>(`/episodes/${pathId(episode.project_id)}/copy`, 'POST', {
    request_id: requestId, expected_revision: episode.document_revision, name,
  }),
};

export const canvasPresets = [
  {label: '竖屏 9:16', width: 1080, height: 1920},
  {label: '横屏 16:9', width: 1920, height: 1080},
  {label: '方形 1:1', width: 1080, height: 1080},
] as const;
export const fpsPresets = [{label: '25 fps', value: '25/1'}, {label: '30 fps', value: '30/1'}, {label: '29.97 fps（30000/1001）', value: '30000/1001'}] as const;

export function productionDraft(production?: ProductionSummary): ProductionDraft {
  return production ? {name: production.name, description: production.description, language: production.language,
    width: String(production.canvas.width), height: String(production.canvas.height), fps: `${production.fps.num}/${production.fps.den}`}
    : {name: '', description: '', language: 'zh-CN', width: '1080', height: '1920', fps: '25/1'};
}
export function validateLibraryName(value: string): string {
  const name = value.trim();
  if (!name || name.length > 200) throw new Error('名称需为 1–200 个字符。');
  return name;
}
export function validateProductionDraft(draft: ProductionDraft): ProductionMetadata {
  const name = validateLibraryName(draft.name), description = draft.description.trim(), language = draft.language.trim();
  if (description.length > 5000) throw new Error('项目简介最多 5000 个字符。');
  if (!language || language.length > 50) throw new Error('语言需为 1–50 个字符，例如 zh-CN。');
  if (!/^\d+$/.test(draft.width) || !/^\d+$/.test(draft.height)) throw new Error('画幅宽、高请填写整数像素。');
  const width = Number(draft.width), height = Number(draft.height);
  if (![width, height].every(value => Number.isSafeInteger(value) && value >= 64 && value <= 1920 && value % 2 === 0)) throw new Error('画幅宽、高需为 64–1920 之间的偶整数像素。');
  const match = /^(\d+)\/(\d+)$/.exec(draft.fps);
  if (!match) throw new Error('请选择可用帧率。');
  const num = Number(match[1]), den = Number(match[2]);
  if (![num, den].every(value => Number.isSafeInteger(value) && value > 0 && value <= 1e9) || num / den < 1 || num / den > 120) throw new Error('帧率需在 1–120 fps 之间。');
  return {name, description, language, canvas: {width, height}, fps: {num, den}};
}
export function libraryDuration(frames: number, fps: Rational): string {
  if (!Number.isFinite(frames) || !Number.isFinite(fps.num) || !Number.isFinite(fps.den) || fps.num <= 0 || fps.den <= 0) return '时长未知';
  const seconds = Math.max(0, Math.floor(frames * fps.den / fps.num));
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
}
export function libraryFps(fps: Rational): string {
  return `${Number((fps.num / fps.den).toFixed(3))} fps${fps.den === 1 ? '' : ` (${fps.num}/${fps.den})`}`;
}
export function filterProductions(productions: ProductionSummary[], query: string, archived: 'active' | 'archived' | 'all'): ProductionSummary[] {
  const match = query.trim().toLocaleLowerCase();
  return productions.filter(item => (archived === 'all' || item.archived === (archived === 'archived'))
    && (!match || `${item.name}\n${item.description}`.toLocaleLowerCase().includes(match)));
}
