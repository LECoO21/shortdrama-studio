'use client';

import {useEffect, useRef, useState} from 'react';
import {request} from '../lib/api';
import {formatBytes} from '../lib/uploads';
import type {Asset} from '../lib/types';

type Reference = {kind: string; id: string; project_id?: string; revision?: number; message: string};
type References = {can_remove: boolean; blockers: Reference[]; references: Record<string, unknown[]>;
  removal_scope: string; original_preserved: boolean};
type Analysis = {manifest: {proxy?: unknown; waveform?: unknown[]; shots?: unknown[]; analysis_state?: string} | null;
  latest_job?: {status: string; error?: string | null} | null};

export default function AssetDetails({asset, productionId, linked, onBeforeRemove, onChanged, onAnalyze}: {
  asset: Asset; productionId?: string; linked: boolean; onBeforeRemove: () => Promise<boolean>;
  onChanged: () => Promise<void>; onAnalyze: () => void;
}) {
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [references, setReferences] = useState<References | null>(null);
  const [working, setWorking] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const removeRequest = useRef<string | null>(null);
  const [removed, setRemoved] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const value = await request<Analysis>(`/assets/${asset.id}/analysis`, {signal: controller.signal});
        if (!controller.signal.aborted) setAnalysis(value);
      } catch { /* The next read recovers without hiding reference errors. */ }
      if (!controller.signal.aborted) timer = setTimeout(read, 3000);
    }
    void read();
    return () => {controller.abort(); if (timer) clearTimeout(timer);};
  }, [asset.id]);
  async function checkReferences() {
    if (!productionId) return;
    setWorking(true); setError(''); setNotice('');
    try {
      if (!await onBeforeRemove()) throw new Error('请先保存当前工程，再核对素材引用。');
      setReferences(await request<References>(`/assets/${asset.id}/references?production_id=${encodeURIComponent(productionId)}`));
    } catch (failure) {setError(failure instanceof Error ? failure.message : '引用读取失败');}
    finally {setWorking(false);}
  }
  async function remove() {
    if (!productionId) return;
    setWorking(true); setError('');
    try {
      if (!await onBeforeRemove()) throw new Error('请先保存当前工程。');
      removeRequest.current ??= crypto.randomUUID();
      const result = await request<{removed: boolean}>(`/productions/${productionId}/assets/${asset.id}/remove`, {method: 'POST', body: JSON.stringify({request_id: removeRequest.current})});
      setRemoved(true); setReferences(null);
      setNotice(result.removed ? '已从本项目素材库移除，原件、共享分析和其他项目保留。' : '素材已不在本项目素材库中，原件保留。');
      try {await onChanged();} catch {setError('移除结果已确认，但列表刷新失败，请刷新页面查看。');}
    } catch (failure) {setError(failure instanceof Error ? failure.message : '移除未完成');}
    finally {setWorking(false);}
  }
  const video = asset.metadata.streams.find(stream => stream.index === asset.metadata.video_stream_index);
  const audio = asset.metadata.streams.find(stream => stream.index === asset.metadata.audio_stream_index);
  const manifest = analysis?.manifest;
  const videoSeconds = video ? Number(BigInt(video.end_pts) - BigInt(video.start_pts)) * video.time_base.num / video.time_base.den : 0;
  const averageFps = videoSeconds > 0 && typeof video?.frame_count === 'number' ? video.frame_count / videoSeconds : null;
  return <section className="property-group" aria-label="素材详情与引用">
    <h4>素材详情</h4><div className="property-summary">
      <span>大小</span><b>{asset.size_bytes == null ? '暂未读取' : formatBytes(asset.size_bytes)}</b>
      <span>视频编码</span><b>{String(video?.codec ?? video?.codec_name ?? '无视频')}</b>
      <span>源帧率</span><b>{averageFps == null ? '无视频' : `${video?.is_vfr ? '变帧率 · 平均 ' : ''}${averageFps.toFixed(3)} fps`}</b>
      <span>音频编码</span><b>{String(audio?.codec ?? audio?.codec_name ?? '无音轨')}</b>
      {audio && <><span>采样与声道</span><b>{String(audio.sample_rate ?? '未知')} Hz / {String(audio.channels ?? '未知')} 声道</b></>}
      <span>素材版本</span><b>{asset.version}</b>
      <span>导入时间</span><b>{asset.created_at ? new Date(asset.created_at).toLocaleString('zh-CN') : '历史记录未提供'}</b>
      <span>项目关联</span><b>{linked ? '属于本项目' : '共享素材库'}</b>
      <span>代理</span><b>{manifest?.proxy ? '已生成' : '尚未生成'}</b>
      <span>缩略图</span><b>{manifest?.shots?.length ? `${manifest.shots.length} 个镜头候选` : '尚未生成'}</b>
      <span>波形</span><b>{manifest?.waveform?.length ? '已生成' : audio ? '尚未生成' : '无音轨'}</b>
      <span>处理状态</span><b>{analysis?.latest_job?.status === 'failed' ? '处理失败，可重试' : manifest ? '本地结果可查看' : '待处理'}</b>
    </div>
    {analysis?.latest_job?.error && <p role="alert" className="field-help">{analysis.latest_job.error}</p>}
    <button type="button" className="wide-button" onClick={onAnalyze}>查看处理结果 / 校对对白</button>
    {linked && productionId && !removed && <><button type="button" className="wide-button" disabled={working} onClick={() => void checkReferences()}>检查引用与移除</button>
      {references && <div className="asset-reference-result"><p className="field-help">只移除本项目的素材关联，原始文件、共享分析和其他项目保留。</p>
        {!!references.blockers.length && <><p>当前不能移除：</p><ul>{references.blockers.slice(0, 8).map((item, index) => <li key={`${item.kind}:${item.id}:${index}`}>{item.message}{item.revision ? `（版本 ${item.revision}）` : ''}</li>)}</ul><p className="field-help">已有工程、历史版本或导出快照仍在引用，不会因删除当前片段而解除历史引用。</p></>}
        {references.can_remove && <button type="button" className="danger-button wide-button" disabled={working} onClick={() => void remove()}>从本项目素材库移除</button>}
      </div>}
    </>}
    {error && <p role="alert" className="field-help">{error}</p>}{notice && <p role="status" className="field-help">{notice}</p>}
  </section>;
}
