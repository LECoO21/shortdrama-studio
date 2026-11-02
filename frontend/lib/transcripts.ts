import {API_BASE, ApiError} from './api';
import type {Asset, Rational, SubtitleProposal} from './types';

export type TranscriptReview = 'pending' | 'confirmed' | 'rejected';
export type TranscriptBinding = {
  asset_id: string; asset_version: number; source_sha256: string; audio_sha256: string;
  start_pts: number | string; time_base_num: number; time_base_den: number;
  sample_count: number; sample_rate: number;
};
export type TranscriptSegment = {
  id: string; text: string; start_ms: number; end_ms: number;
  raw_text: string; raw_start_ms: number; raw_end_ms: number;
  source_start: Rational; source_end: Rational;
  status: TranscriptReview; human_confirmed: boolean; uncertainty: string[];
  timestamp_precision?: string;
};
export type TranscriptGeneration = {
  id: string; generation: number; revision: number; model: string;
  binding: TranscriptBinding; segments: TranscriptSegment[];
  quality_passed: false; human_confirmed: false; warnings?: string[];
};
export type TranscriptHistory = {
  id: string; generation: number; revision: number; model: string;
  created_at: string; confirmed_count: number; segment_count?: number;
};
export type TranscriptJob = {
  id: string; status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  mode?: string; progress: number; attempt: number; cancel_requested?: number | boolean;
  error: string | null; steps?: Record<string, string>;
};
export type TranscriptionRecord = {
  asset_id: string; version: number; current: TranscriptGeneration | null;
  history: TranscriptHistory[]; latest_job: TranscriptJob | null;
  local_models: {id: 'small' | 'medium'; ready: boolean; reason?: string}[];
};
export type TranscriptTarget = {clipId: string; label: string; assetId?: string};
export type TranscriptSubtitleMode = 'segmented' | 'original';
export type TranscriptSubtitlePreview = {
  proposal: SubtitleProposal; baseline_signature: string; original_count: number; segment_ids: string[];
  mode: TranscriptSubtitleMode;
};
export type TranscriptImport = (asset: Asset, generation: TranscriptGeneration,
  confirmed: TranscriptSegment[], mode?: TranscriptSubtitleMode) => Promise<TranscriptSubtitlePreview | string>;
export type TranscriptApply = (preview: TranscriptSubtitlePreview) => Promise<string>;
export type TranscriptDraft = {text: string; start: string; end: string};
export type TranscriptEdit = {text: string; start_ms: number; end_ms: number; status: TranscriptReview};

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

export const transcripts = {
  read: (assetId: string, transcriptId?: string, signal?: AbortSignal) =>
    request<TranscriptionRecord>(`/assets/${encodeURIComponent(assetId)}/transcription${transcriptId ? `?${new URLSearchParams({transcript_id: transcriptId})}` : ''}`, {method: 'GET', signal}),
  start: (assetId: string, requestId: string) => request<TranscriptJob>(`/assets/${encodeURIComponent(assetId)}/transcription`, {
    method: 'POST', body: JSON.stringify({request_id: requestId, model: 'medium', language: 'zh'}),
  }),
  cancel: (jobId: string) => request<TranscriptJob>(`/analysis-jobs/${encodeURIComponent(jobId)}/cancel`, {method: 'POST', body: '{}'}),
  retry: (jobId: string) => request<TranscriptJob>(`/analysis-jobs/${encodeURIComponent(jobId)}/retry`, {method: 'POST', body: '{}'}),
  save: (transcriptId: string, segmentId: string, expectedRevision: number, edit: TranscriptEdit) =>
    request<TranscriptGeneration>(`/transcripts/${encodeURIComponent(transcriptId)}/segments/${encodeURIComponent(segmentId)}`, {
      method: 'PATCH', body: JSON.stringify({expected_revision: expectedRevision, ...edit}),
    }),
};

export function transcriptJobActive(job?: TranscriptJob | null) {
  return job?.status === 'queued' || job?.status === 'running';
}

export function transcriptJobLabel(job: TranscriptJob) {
  if (job.cancel_requested && transcriptJobActive(job)) return '正在停止';
  return ({queued: '排队中', running: '本地识别中', succeeded: '候选已生成 · 待校对',
    failed: '识别未完成', cancelled: '已取消'})[job.status];
}

export function confirmedTranscriptSegments(generation?: TranscriptGeneration | null): TranscriptSegment[] {
  return generation?.segments.filter(segment => segment.status === 'confirmed' && segment.human_confirmed === true) ?? [];
}

export function transcriptDraft(segment: TranscriptSegment): TranscriptDraft {
  return {text: segment.text, start: (segment.start_ms / 1000).toFixed(3), end: (segment.end_ms / 1000).toFixed(3)};
}

function parseSeconds(value: string): number {
  if (!/^\d+(?:\.\d{1,3})?$/.test(value.trim())) throw new Error('时间请填写非负秒数，最多保留三位小数。');
  const [whole, fraction = ''] = value.trim().split('.');
  const milliseconds = Number(whole) * 1000 + Number(fraction.padEnd(3, '0'));
  if (!Number.isSafeInteger(milliseconds)) throw new Error('时间超出可编辑范围。');
  return milliseconds;
}

export function validateTranscriptEdit(draft: TranscriptDraft, binding: TranscriptBinding, status: TranscriptReview): TranscriptEdit {
  const text = draft.text.trim();
  if (!text || text.length > 1000) throw new Error('每句文字需为 1–1000 个字符。');
  const start_ms = parseSeconds(draft.start), end_ms = parseSeconds(draft.end);
  if (end_ms <= start_ms) throw new Error('结束时间必须晚于开始时间。');
  if (!Number.isFinite(binding.sample_count) || !Number.isFinite(binding.sample_rate) || binding.sample_rate <= 0
    || start_ms < 0 || end_ms > Math.floor(binding.sample_count * 1000 / binding.sample_rate)) {
    throw new Error('时间必须位于这份素材的可用音轨内。');
  }
  return {text, start_ms, end_ms, status};
}

export function transcriptPreviewSeconds(asset: Asset, binding: TranscriptBinding, startMs: number): number {
  // Preview uses media-relative seconds; persisted edits and subtitle import
  // retain the backend's exact source fractions and source binding.
  const metadata = asset.metadata;
  const origin = typeof metadata.media_origin_seconds === 'number' ? metadata.media_origin_seconds
    : typeof metadata.start_seconds === 'number' ? metadata.start_seconds : 0;
  const stream = metadata.streams.find(value => value.type === 'audio'
    && value.time_base.num === binding.time_base_num && value.time_base.den === binding.time_base_den);
  const anchor = BigInt(stream?.start_pts ?? binding.start_pts);
  const relative = Number(BigInt(binding.start_pts) - anchor) * binding.time_base_num / binding.time_base_den;
  return Math.max(0, relative + (Number(anchor) * binding.time_base_num / binding.time_base_den - origin) + startMs / 1000);
}

const uncertaintyText: Record<string, string> = {
  requires_listen_review: '需对照原音试听', estimated_timestamps: '句子时间为模型估计',
  low_decoder_probability: '存在低概率解码词', token_evidence_unavailable: '缺少词级证据',
  token_time_unavailable_or_out_of_segment: '部分词级时间不可用',
  model_non_dialogue_marker_requires_audio_review: '可能是音乐或其他非对白标记',
};
export function transcriptUncertaintyLabel(values: string[]): string {
  const labels = values.map(value => uncertaintyText[value] ?? '另有模型不确定信号');
  return [...new Set(labels)].join(' · ');
}
