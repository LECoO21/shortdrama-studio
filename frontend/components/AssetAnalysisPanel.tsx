'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import Link from 'next/link';
import {API_BASE, mediaUrl} from '../lib/api';
import type {Asset, Rational} from '../lib/types';
import type {TranscriptApply, TranscriptImport, TranscriptTarget} from '../lib/transcripts';
import TranscriptPanel from './TranscriptPanel';
import styles from './AssetAnalysisPanel.module.css';

type AnalysisJob = {
  id: string; asset_id: string; mode: 'local' | 'cloud' | 'local_asr';
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  attempt: number; progress: number; cancel_requested?: number; error: string | null;
  steps?: Record<string, string>;
};
type Shot = {
  id: string; start_pts: string; end_pts: string; description: string | null;
  tags: string[]; manual: boolean; thumbnail_url: string; transcript?: string;
  model_description?: string | null; boundary_status?: string;
  transcript_precision?: string;
  visual_evidence?: {scene: string; people_candidates: {description: string; confidence: number}[];
    actions: string[]; tags: string[]; unknowns: string[]};
};
type Manifest = {
  analysis_state?: string; method?: string;
  proxy: {url: string; width: number; height: number} | null;
  video: {time_base: Rational; start_pts: string; end_pts: string} | null;
  audio: {time_base: Rational; start_pts: string; end_pts: string} | null;
  shots: Shot[]; waveform: number[]; waveform_bin_seconds?: number;
  unmatched_annotations?: number;
  transcript?: {status: string; segments: {start_ms: number; end_ms: number; text: string; speaker_id?: number | null}[]};
};
type Analysis = {
  asset_id: string; version: number; revision: number; manifest: Manifest | null;
  latest_job: AnalysisJob | null;
  cloud: {enabled: boolean; configured: boolean; asr_audio_ready: boolean; provider: string;
    models: {asr: string; vision: string; embedding: string}; data_flow: string; error?: string | null};
};
type SearchResult = {
  asset_id: string; shot_id: string; start_pts: string; end_pts: string; time_base: Rational;
  description: string | null; tags: string[]; transcript: string; thumbnail_url: string;
  score: number; match_kind: string;
};
type SearchResponse = {results: SearchResult[]; mode: 'keyword' | 'semantic'; warnings: string[]};
type EditDraft = {assetId: string; shotId: string; revision: number; description: string; tags: string};

async function analysisRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}/api/v1${path}`, {
    ...options, cache: 'no-store',
    headers: {'X-Studio-Client': '1', 'Content-Type': 'application/json', ...options.headers},
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.error?.message ?? `请求失败（${response.status}）`);
  return body as T;
}

const active = (job?: AnalysisJob | null) => job?.status === 'queued' || job?.status === 'running';
const errorMessage = (failure: unknown) => failure instanceof Error ? failure.message : '请求未完成，请重试。';
const stamp = (value: number) => `${Math.floor(Math.max(0, value) / 60).toString().padStart(2, '0')}:${(Math.max(0, value) % 60).toFixed(2).padStart(5, '0')}`;

function sourceSeconds(asset: Asset, pts: string, base: Rational, kind: 'video' | 'audio' = 'video') {
  const metadata = asset.metadata;
  const origin = typeof metadata.media_origin_seconds === 'number' ? metadata.media_origin_seconds
    : typeof metadata.start_seconds === 'number' ? metadata.start_seconds : 0;
  const stream = metadata.streams.find(value => value.type === kind && value.time_base.num === base.num && value.time_base.den === base.den);
  // Subtract the integer anchor before converting so long source clocks do
  // not lose the small seek offset in Number(large_pts) - Number(large_pts).
  const anchor = stream?.start_pts ?? '0';
  return Math.max(0, Number(BigInt(pts) - BigInt(anchor)) * base.num / base.den
    + (Number(BigInt(anchor)) * base.num / base.den - origin));
}

function jobText(job: AnalysisJob) {
  if (job.cancel_requested && active(job)) return '正在停止';
  return ({queued: '排队中', running: '处理中', failed: '未完成', cancelled: '已取消',
    succeeded: job.mode === 'local' ? '本地准备完成' : job.mode === 'local_asr' ? '本地对白候选完成' : '云分析完成'})[job.status];
}

function waveformPath(values: number[]) {
  if (!values.length) return '';
  const count = Math.min(240, values.length);
  return Array.from({length: count}, (_, index) => {
    const start = Math.floor(index * values.length / count), end = Math.ceil((index + 1) * values.length / count);
    const height = Math.max(1, Math.min(1, Math.max(...values.slice(start, end))) * 23);
    const x = (index + 0.5) * 480 / count;
    return `M${x.toFixed(2)} ${(25 - height).toFixed(2)}V${(25 + height).toFixed(2)}`;
  }).join(' ');
}

export default function AssetAnalysisPanel({assets, onPreview, transcriptTarget, onAddTranscript, onApplyTranscript, onTranscriptDraftChange}: {
  assets: Asset[]; onPreview: (asset: Asset, sourceSeconds: number) => void;
  transcriptTarget?: TranscriptTarget; onAddTranscript?: TranscriptImport; onApplyTranscript?: TranscriptApply;
  onTranscriptDraftChange?: (editing: boolean) => void;
}) {
  const [selectedId, setSelectedId] = useState(transcriptTarget?.assetId ?? assets[0]?.id ?? '');
  const asset = assets.find(value => value.id === selectedId) ?? assets[0];
  const assetId = asset?.id ?? '';
  const [record, setRecord] = useState<{assetId: string; data: Analysis} | null>(null);
  const data = record?.assetId === assetId ? record.data : null;
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState('');
  const [failure, setFailure] = useState('');
  const [readFailure, setReadFailure] = useState('');
  const [working, setWorking] = useState(false);
  const [transcriptEditing, setTranscriptEditing] = useState(false);
  const markTranscriptDraft = useCallback((editing: boolean) => {
    setTranscriptEditing(editing);
    onTranscriptDraftChange?.(editing);
  }, [onTranscriptDraftChange]);
  const [draft, setDraft] = useState<EditDraft | null>(null);
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'keyword' | 'semantic'>('keyword');
  const [searchState, setSearch] = useState<(SearchResponse & {scope: string}) | null>(null);
  const [searching, setSearching] = useState(false);
  const operation = useRef(false);
  const currentAsset = useRef(assetId);
  const searchGeneration = useRef(0);
  const searchController = useRef<AbortController | null>(null);
  const searchPending = useRef(false);
  const readGeneration = useRef(0);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {mounted.current = false; searchController.current?.abort();};
  }, []);

  useEffect(() => {
    currentAsset.current = assetId;
    if (!assetId) return;
    const controller = new AbortController(), generation = ++readGeneration.current;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const result = await analysisRequest<Analysis>(`/assets/${encodeURIComponent(assetId)}/analysis`, {signal: controller.signal});
        if (stopped || generation !== readGeneration.current) return;
        setRecord({assetId, data: result});
        setLoading(false);
        setReadFailure('');
        // Poll while idle too: a second tab or a recovered worker may change
        // this asset. Do not replace a user's annotation draft on refresh.
        timer = setTimeout(poll, active(result.latest_job) ? 1000 : 5000);
      } catch (error) {
        if (stopped || generation !== readGeneration.current) return;
        setLoading(false);
        setReadFailure(errorMessage(error));
        timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {stopped = true; controller.abort(); if (timer) clearTimeout(timer);};
  }, [assetId, refresh]);

  const manifest = data?.manifest;
  const job = data?.latest_job;
  const canCloud = !!data?.cloud.enabled && !!data.cloud.configured && !!data.cloud.asr_audio_ready;
  const cloudMissing = !data ? '正在读取接入状态。' : data.cloud.error
    || 'ChatGPT 素材分析正在接入验证中；账号连接成功后，仍需验证分析能力。';
  const assetScope = assets.map(value => value.id).join(',');
  const search = searchState?.scope === assetScope ? searchState : null;

  function chooseAsset(id: string) {
    ++readGeneration.current;
    setSelectedId(id); setFailure(''); setReadFailure(''); setNotice(''); setLoading(true); setDraft(null);
  }

  async function jobAction(path: string, body: unknown, message: string) {
    if (operation.current || !asset) return;
    const id = asset.id;
    operation.current = true; ++readGeneration.current;
    setWorking(true); setFailure(''); setNotice('');
    try {
      const next = await analysisRequest<AnalysisJob>(path, {method: 'POST', body: JSON.stringify(body)});
      if (!mounted.current || currentAsset.current !== id) return;
      setRecord(previous => previous?.assetId === id ? {...previous, data: {...previous.data, latest_job: next}} : previous);
      setNotice(message);
    } catch (error) {
      if (mounted.current && currentAsset.current === id) setFailure(errorMessage(error));
    } finally {operation.current = false; if (mounted.current) {setWorking(false); setRefresh(value => value + 1);}}
  }

  async function saveAnnotation() {
    if (operation.current || !draft || draft.assetId !== assetId) return;
    const input = draft;
    operation.current = true; ++readGeneration.current;
    setWorking(true); setFailure(''); setNotice('');
    try {
      const tags = input.tags.split(/[,，、\n]/).map(tag => tag.trim()).filter(Boolean);
      const result = await analysisRequest<Analysis>(`/assets/${encodeURIComponent(input.assetId)}/analysis/annotations`, {
        method: 'PATCH', body: JSON.stringify({expected_revision: input.revision, shot_id: input.shotId, description: input.description, tags}),
      });
      if (!mounted.current || currentAsset.current !== input.assetId) return;
      setRecord({assetId: input.assetId, data: result}); setDraft(null);
      setNotice('人工修正已保存。后台重跑会保留此源区间的修正。');
      // A saved annotation changes keyword matches and invalidates old
      // embedding text; keep previously displayed search hits from implying
      // they already reflect this edit.
      setSearch(null);
    } catch (error) {
      if (mounted.current && currentAsset.current === input.assetId) setFailure(`${errorMessage(error)}。输入草稿仍保留；可复制后重新载入此镜头。`);
    } finally {operation.current = false; if (mounted.current) {setWorking(false); setRefresh(value => value + 1);}}
  }

  async function searchAssets(event: React.FormEvent) {
    event.preventDefault();
    if (!query.trim() || !assets.length || searchPending.current) return;
    searchPending.current = true;
    searchController.current?.abort();
    const controller = new AbortController(), generation = ++searchGeneration.current;
    searchController.current = controller;
    setSearching(true); setFailure(''); setNotice(''); setSearch(null);
    try {
      const result = await analysisRequest<SearchResponse>(`/search?${new URLSearchParams({query: query.trim(), asset_ids: assetScope, mode})}`, {signal: controller.signal});
      if (mounted.current && generation === searchGeneration.current) setSearch({...result, scope: assetScope});
    } catch (error) {
      if (mounted.current && generation === searchGeneration.current && !controller.signal.aborted) setFailure(errorMessage(error));
    } finally {
      if (generation === searchGeneration.current) {
        searchPending.current = false;
        if (mounted.current) setSearching(false);
      }
    }
  }

  const openShot = (shot: Shot) => {
    if (asset && manifest?.video) onPreview(asset, sourceSeconds(asset, shot.start_pts, manifest.video.time_base));
  };

  if (!asset) return <div className={styles.empty}>先导入一份视频或音频，再生成素材索引。</div>;

  return <section className={styles.panel} aria-label="素材镜头与检索">
    <div className={styles.toolbar}>
      <label className={styles.assetField}>当前素材
        <select aria-label="选择分析素材" value={assetId} disabled={transcriptEditing} title={transcriptEditing ? '请先保存或取消对白编辑，再切换素材。' : undefined} onChange={event => chooseAsset(event.target.value)}>
          {assets.map(value => <option key={value.id} value={value.id}>{value.filename}</option>)}
        </select>
      </label>
      <button type="button" disabled={working || active(job)} onClick={() => void jobAction(`/assets/${encodeURIComponent(assetId)}/analysis`, {request_id: crypto.randomUUID(), mode: 'local'}, '本地预处理已提交；不调用云模型。')}>生成本地镜头</button>
      <button type="button" disabled={working || active(job) || !canCloud} title={!canCloud ? cloudMissing : '会按下方服务与数据流说明提交分析'} onClick={() => void jobAction(`/assets/${encodeURIComponent(assetId)}/analysis`, {request_id: crypto.randomUUID(), mode: 'cloud'}, 'AI 分析已提交，可关闭面板后再查看。')}>{canCloud ? 'AI 素材分析' : 'AI 分析待接入'}</button>
    </div>

    <details className={styles.cloud} open={!canCloud}>
      <summary>AI 接入与数据流{canCloud ? ' · 已配置' : ' · 验证中'}</summary>
      <p>{data?.cloud.provider ?? 'ChatGPT（接入验证中）'}。{canCloud ? data?.cloud.data_flow : '当前未启用新的 AI 素材分析。本地镜头、人工标签及关键词检索仍可使用，连接账号不会自动发送素材。'}</p>
      {data && canCloud && <p className={styles.modelNames}>ASR {data.cloud.models.asr} · 视觉 {data.cloud.models.vision} · 向量 {data.cloud.models.embedding}</p>}
      {!canCloud && <p className={styles.warning}>{cloudMissing}</p>}
      <p><Link href="/settings/ai" target="_blank" rel="noopener noreferrer">连接 ChatGPT →</Link></p>
      <p>主动分析时计划发送选定帧与必要文字至 OpenAI，原视频和音轨保留本地。使用现有账号可用额度，不自动购买额外额度；数据处理与留存以账号设置及适用条款为准。识别结果仍需人工复核。</p>
    </details>

    {(failure || readFailure) && <div role="alert" className={styles.error}>{failure || readFailure}</div>}
    {notice && <div role="status" className={styles.notice}>{notice}</div>}

    {job && <div className={styles.job} aria-label="素材分析任务">
      <div className={styles.jobTitle}><strong>{job.mode === 'local' ? '本地预处理' : job.mode === 'local_asr' ? '本地对白转写' : '云端素材分析'}</strong><span>{jobText(job)} · {job.progress ?? 0}%</span>
        {active(job) && <button type="button" disabled={working || !!job.cancel_requested} onClick={() => void jobAction(`/analysis-jobs/${job.id}/cancel`, {}, '已申请取消；正在停止后续处理。')}>取消任务</button>}
        {(job.status === 'failed' || job.status === 'cancelled') && <button type="button" disabled={working} onClick={() => void jobAction(`/analysis-jobs/${job.id}/retry`, {}, '重试已提交，已完成步骤会按任务记录恢复。')}>重试任务</button>}
      </div>
      <progress aria-label="分析进度" max={100} value={job.progress ?? 0}/>
      {job.error && <p className={styles.warning}>{job.error}</p>}
      <small>任务 {job.id.slice(0, 10)} · 第 {job.attempt} 次执行{job.steps && Object.keys(job.steps).length > 0 ? ` · ${Object.entries(job.steps).map(([step, value]) => `${({prepare: '预处理', asr: '对白', vision: '视觉', embedding: '向量'} as Record<string, string>)[step] ?? step}：${({succeeded: '完成', not_requested: '未请求', no_audio: '无音轨', no_video: '无画面'} as Record<string, string>)[value] ?? value}`).join(' / ')}` : ''}</small>
    </div>}

    <TranscriptPanel key={`${asset.id}:${asset.version}`} asset={asset} onPreview={onPreview}
      target={transcriptTarget} onAddTranscript={onAddTranscript} onApplyTranscript={onApplyTranscript} onDraftChange={markTranscriptDraft}/>

    <form className={styles.search} onSubmit={event => void searchAssets(event)}>
      <label className={styles.queryField}>在当前素材库中检索
        <input aria-label="素材检索内容" value={query} maxLength={200} placeholder="输入台词、人物外观、场景、动作或人工标签" onChange={event => setQuery(event.target.value)}/>
      </label>
      <select aria-label="素材检索方式" value={mode} onChange={event => {setMode(event.target.value as 'keyword' | 'semantic'); setSearch(null);}}>
        <option value="keyword">关键词 · 本地</option><option value="semantic" disabled={!data?.cloud.enabled || !data.cloud.configured}>语义{canCloud ? '' : ' · 待验证'}</option>
      </select>
      <button type="submit" disabled={!query.trim() || searching || !assets.length}>{searching ? '检索中…' : '检索素材'}</button>
      <small>{mode === 'semantic' ? '语义检索需要已验证的索引和查询模型；尚未接入时不会改用关键词冒充语义结果。相关度不代表正确概率。' : '搜索已保存的中文对白、描述及标签；没有模型时可先添加人工标签。'}</small>
    </form>

    {search && <section className={styles.results} aria-label="素材检索结果">
      <h3>{search.mode === 'keyword' ? '关键词' : '语义'}结果 · {search.results.length} 条</h3>
      {search.warnings.map((warning, index) => <p className={styles.warning} key={index}>{warning}</p>)}
      {!search.results.length && <p className={styles.empty}>没有命中。可以换一种表达，或先为镜头补充描述／标签。</p>}
      {search.results.map(result => {
        const source = assets.find(value => value.id === result.asset_id);
        if (!source) return null;
        const start = sourceSeconds(source, result.start_pts, result.time_base);
        return <button type="button" key={`${result.asset_id}:${result.shot_id}`} className={styles.searchHit} onClick={() => onPreview(source, start)} aria-label={`预览检索结果 ${source.filename} ${stamp(start)}`}>
          {/* Local controlled thumbnails are immutable analysis artifacts. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img alt="检索命中镜头" src={mediaUrl(result.thumbnail_url)} loading="lazy"/>
          <span><strong>{source.filename} · {stamp(start)}</strong><span>{result.description || '描述未知'}</span>{result.transcript && <em>对白：{result.transcript}</em>}<small>{result.tags.join(' · ') || '暂无标签'}{search.mode === 'semantic' ? ` · 相关度 ${result.score.toFixed(3)}` : ''}</small></span>
        </button>;
      })}
    </section>}

    {loading && !data && <p role="status" className={styles.empty}>正在读取素材分析记录…</p>}
    {!manifest && !loading && <div className={styles.empty}>尚无已发布的镜头索引。先点“生成本地镜头”，再查看画面并添加标签。<small>本地处理不会生成 AI 对白或视觉描述。</small></div>}

    {manifest && <>
      <div className={styles.sectionTitle}><h3>素材证据</h3><span>{manifest.analysis_state === 'cloud_ready' ? '云分析候选 · 请复核' : '本地准备完成 · 尚无 AI 理解'}</span></div>
      {!!manifest.unmatched_annotations && <p className={styles.warning}>镜头边界有变化，{manifest.unmatched_annotations} 项旧区间人工修正已保留，尚未应用到新镜头，请复核。</p>}
      {manifest.proxy && <details className={styles.proxy}><summary>查看低码率代理 · {manifest.proxy.width} × {manifest.proxy.height}</summary><video key={manifest.proxy.url} aria-label="素材代理预览" src={mediaUrl(manifest.proxy.url)} controls preload="metadata" playsInline/><small>代理仅用于浏览；源时间和导出继续以原素材为准。</small></details>}
      {manifest.waveform.length > 0 ? <button type="button" className={styles.waveform} aria-label="预览素材音频波形" onClick={() => {
        if (asset && manifest.audio) onPreview(asset, sourceSeconds(asset, manifest.audio.start_pts, manifest.audio.time_base, 'audio'));
      }}><svg viewBox="0 0 480 50" role="img" aria-label="音频峰值波形"><path d={waveformPath(manifest.waveform)}/></svg><span>音频波形 · 点击试听源素材</span></button> : <p className={styles.hint}>无可用音轨；不会生成虚构转写。</p>}
      <div className={styles.sectionTitle}><h3>镜头候选 · {manifest.shots.length}</h3><span>0.5 秒采样候选，切点需人工复核</span></div>
      {manifest.shots.length === 0 && <p className={styles.hint}>该素材没有视频镜头。音频转写结果在下方显示。</p>}
      <div className={styles.shots}>
        {manifest.shots.map((shot, index) => {
          const start = manifest.video ? sourceSeconds(asset, shot.start_pts, manifest.video.time_base) : 0;
          const end = manifest.video ? sourceSeconds(asset, shot.end_pts, manifest.video.time_base) : 0;
          const editing = draft?.assetId === assetId && draft.shotId === shot.id;
          return <article className={styles.shot} key={`${assetId}:${shot.start_pts}:${shot.end_pts}`}>
            <button type="button" className={styles.thumbnail} aria-label={`预览镜头 ${index + 1}`} onClick={() => openShot(shot)}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img alt={`候选镜头 ${index + 1} 缩略图`} src={mediaUrl(shot.thumbnail_url)} loading="lazy"/>
              <span>{stamp(start)} – {stamp(end)}</span>
            </button>
            <div className={styles.shotContent}>
              <div className={styles.shotHeading}><strong>镜头 {index + 1}</strong><span>{shot.manual ? '人工修正' : shot.description ? '模型候选 · 待确认' : '视觉描述未知'}</span></div>
              {editing ? <div className={styles.editor}>
                <label>镜头描述<textarea aria-label={`镜头 ${index + 1} 人工描述`} value={draft.description} maxLength={2000} rows={3} onChange={event => setDraft({...draft, description: event.target.value})}/></label>
                <label>检索标签<input aria-label={`镜头 ${index + 1} 人工标签`} value={draft.tags} placeholder="用逗号分隔，如：客厅、红衣人物、开门" onChange={event => setDraft({...draft, tags: event.target.value})}/></label>
                {draft.revision !== data?.revision && <p className={styles.warning}>后台结果已更新，草稿仍保留。保存可能提示版本冲突，可复制草稿后重新载入。</p>}
                <div><button type="button" disabled={working} onClick={() => void saveAnnotation()}>保存人工修正</button><button type="button" disabled={working} onClick={() => setDraft(null)}>取消编辑</button>{draft.revision !== data?.revision && <button type="button" disabled={working} onClick={() => setDraft({assetId, shotId: shot.id, revision: data?.revision ?? 0, description: shot.description ?? '', tags: shot.tags.join('，')})}>重新载入此镜头</button>}</div>
              </div> : <><p>{shot.description || '尚未进行视觉理解。可以先添加人工描述与标签。'}</p><div className={styles.tags}>{shot.tags.map(tag => <span key={tag}>{tag}</span>)}</div>{shot.transcript && <blockquote>对白：{shot.transcript}{shot.transcript_precision === 'sentence_overlap_candidate' && <small>整句与镜头重叠的候选，句子可能跨切点；不是逐词对齐。</small>}</blockquote>}
                {shot.visual_evidence && <details className={styles.evidence}><summary>查看单帧视觉候选与未知项</summary>
                  <p>仅来自本镜头缩略图，不代表完整动作或跨镜头人物身份；模型自报置信度未经校准。</p>
                  <dl><dt>人物外观候选</dt><dd>{shot.visual_evidence.people_candidates.map(person => person.description).join('；') || '没有可确认的人物外观证据'}</dd>
                    <dt>动作／姿态候选</dt><dd>{shot.visual_evidence.actions.join('；') || '未知'}</dd>
                    <dt>未知／待确认</dt><dd>{shot.visual_evidence.unknowns.join('；') || '模型未额外列出，仍需人工复核'}</dd></dl>
                </details>}
                <div className={styles.shotActions}><button type="button" onClick={() => openShot(shot)}>定位源素材</button><button type="button" disabled={working} onClick={() => setDraft({assetId, shotId: shot.id, revision: data?.revision ?? 0, description: shot.description ?? '', tags: shot.tags.join('，')})}>编辑描述与标签</button></div></>}
            </div>
          </article>;
        })}
      </div>
      {manifest.transcript?.status === 'succeeded' && <section className={styles.transcript} aria-label="素材对白参考"><h3>素材对白参考</h3>
        {manifest.transcript.segments.length ? manifest.transcript.segments.map((segment, index) => <button key={index} type="button" onClick={() => {
          if (manifest.audio) onPreview(asset, sourceSeconds(asset, manifest.audio.start_pts, manifest.audio.time_base, 'audio') + segment.start_ms / 1000);
        }}><time>{stamp(segment.start_ms / 1000)}</time><span>{segment.text}</span></button>) : <p className={styles.hint}>该分析记录未识别到对白。可在上方进行本地识别与校对。</p>}
      </section>}
    </>}
  </section>;
}
