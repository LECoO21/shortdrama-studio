'use client';

import {useEffect, useRef, useState} from 'react';
import type {Asset} from '../lib/types';
import {formatTime} from '../lib/timeline-editing';
import {
  confirmedTranscriptSegments, transcriptDraft, transcriptJobActive, transcriptJobLabel,
  transcriptPreviewSeconds, transcripts, transcriptUncertaintyLabel, validateTranscriptEdit,
  type TranscriptApply, type TranscriptDraft, type TranscriptImport, type TranscriptReview,
  type TranscriptSegment, type TranscriptSubtitleMode, type TranscriptSubtitlePreview, type TranscriptTarget, type TranscriptionRecord,
} from '../lib/transcripts';
import styles from './TranscriptPanel.module.css';

type Draft = TranscriptDraft & {generationId: string; segmentId: string; revision: number};
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求未完成，请重试。';
const stamp = (ms: number) => `${Math.floor(ms / 60000).toString().padStart(2, '0')}:${(ms % 60000 / 1000).toFixed(3).padStart(6, '0')}`;
const reviewLabel = {pending: '模型候选 · 待校对', confirmed: '已人工确认', rejected: '已拒绝 · 不加入字幕'};

export default function TranscriptPanel({asset, onPreview, target, onAddTranscript, onApplyTranscript, onDraftChange}: {
  asset: Asset; onPreview: (asset: Asset, seconds: number) => void;
  target?: TranscriptTarget; onAddTranscript?: TranscriptImport; onApplyTranscript?: TranscriptApply;
  onDraftChange?: (editing: boolean) => void;
}) {
  const scope = `${asset.id}:${asset.version}`;
  const [record, setRecord] = useState<{scope: string; data: TranscriptionRecord} | null>(null);
  const [historyId, setHistoryId] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [working, setWorking] = useState(false);
  const [notice, setNotice] = useState('');
  const [failure, setFailure] = useState('');
  const [readFailure, setReadFailure] = useState('');
  const [draft, setDraft] = useState<Draft | null>(null);
  const [subtitleMode, setSubtitleMode] = useState<TranscriptSubtitleMode>('segmented');
  const [subtitlePreview, setSubtitlePreview] = useState<{scope: string; data: TranscriptSubtitlePreview} | null>(null);
  const [preparingPreview, setPreparingPreview] = useState(false);
  const mounted = useRef(false), operation = useRef(false), generation = useRef(0);
  const currentScope = useRef(scope);
  const data = record?.scope === scope ? record.data : null;
  const current = data?.current;
  const job = data?.latest_job;
  const bound = current?.binding.asset_id === asset.id && current.binding.asset_version === asset.version
    && current.binding.source_sha256 === asset.sha256;
  const confirmed = bound ? confirmedTranscriptSegments(current) : [];
  const targetMatches = !!target && (!target.assetId || target.assetId === asset.id);
  const model = data?.local_models.find(value => value.id === 'medium');
  const editing = !!draft && draft.generationId === current?.id;
  const prepared = subtitlePreview?.data;
  const previewInvalid = !!prepared && (subtitlePreview.scope !== scope || !bound || !current
    || current.id !== prepared.proposal.transcript_id || current.revision !== prepared.proposal.transcript_revision
    || !targetMatches || target?.clipId !== prepared.proposal.clip_id);
  const previewSubtitles = prepared?.proposal.document.subtitles.filter(subtitle => prepared.proposal.added_ids.includes(subtitle.id)) ?? [];
  const unfinished = !!draft || !!subtitlePreview || preparingPreview;

  useEffect(() => {
    mounted.current = true;
    return () => {mounted.current = false;};
  }, []);

  useEffect(() => () => {onDraftChange?.(false);}, [onDraftChange]);

  useEffect(() => {
    onDraftChange?.(unfinished);
  }, [unfinished, onDraftChange]);

  useEffect(() => {
    if (!unfinished) return;
    const warn = (event: BeforeUnloadEvent) => {event.preventDefault(); event.returnValue = '';};
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [unfinished]);

  useEffect(() => {
    currentScope.current = scope;
    const controller = new AbortController(), readGeneration = ++generation.current;
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const result = await transcripts.read(asset.id, historyId || undefined, controller.signal);
        if (stopped || readGeneration !== generation.current) return;
        setRecord({scope, data: result}); setReadFailure('');
        timer = setTimeout(poll, transcriptJobActive(result.latest_job) ? 1000 : 5000);
      } catch (error) {
        if (stopped || readGeneration !== generation.current) return;
        setReadFailure(errorMessage(error));
        timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {stopped = true; controller.abort(); if (timer) clearTimeout(timer);};
  }, [asset.id, scope, historyId, refresh]);

  async function run(action: () => Promise<unknown>, message: string) {
    if (operation.current) return;
    const actionScope = scope;
    operation.current = true; ++generation.current;
    setWorking(true); setFailure(''); setNotice('');
    try {
      await action();
      if (mounted.current && currentScope.current === actionScope) setNotice(message);
    } catch (error) {
      if (mounted.current && currentScope.current === actionScope) setFailure(errorMessage(error));
    } finally {
      operation.current = false;
      if (mounted.current) {setWorking(false); setRefresh(value => value + 1);}
    }
  }

  async function save(segment: TranscriptSegment, status: TranscriptReview, input?: Draft) {
    if (operation.current || !current || !bound || subtitlePreview || preparingPreview) return;
    const selected = current, actionScope = scope;
    let edit;
    try {edit = validateTranscriptEdit(input ?? transcriptDraft(segment), selected.binding, status);}
    catch (error) {setFailure(errorMessage(error)); return;}
    operation.current = true; ++generation.current;
    setWorking(true); setFailure(''); setNotice('');
    try {
      const result = await transcripts.save(selected.id, segment.id, input?.revision ?? selected.revision, edit);
      if (!mounted.current || currentScope.current !== actionScope) return;
      setRecord(previous => previous?.scope === actionScope
        ? {...previous, data: {...previous.data, current: result}} : previous);
      setDraft(null);
      setNotice(status === 'confirmed' ? '这句已保存并人工确认，可加入所选片段。'
        : status === 'rejected' ? '已保存拒绝状态；原始识别候选仍保留。' : '校对草稿已保存，仍需人工确认。');
    } catch (error) {
      if (mounted.current && currentScope.current === actionScope) {
        setFailure(`${errorMessage(error)}${input ? '。编辑内容仍保留；如发生版本冲突，请复制后重新载入。' : ''}`);
      }
    } finally {
      operation.current = false;
      if (mounted.current) {setWorking(false); setRefresh(value => value + 1);}
    }
  }

  async function addConfirmed() {
    if (!targetMatches || !onAddTranscript || !current || !bound || !confirmed.length || draft || subtitlePreview || operation.current) return;
    const selected = current, actionScope = scope;
    operation.current = true; setWorking(true); setPreparingPreview(true); setFailure(''); setNotice('');
    try {
      const result = await onAddTranscript(asset, selected, confirmed, subtitleMode);
      if (!mounted.current || currentScope.current !== actionScope) return;
      if (typeof result === 'string') setNotice(result);
      else setSubtitlePreview({scope: actionScope, data: result});
    } catch (error) {
      if (mounted.current && currentScope.current === actionScope) setFailure(errorMessage(error));
    } finally {operation.current = false; if (mounted.current) {setWorking(false); setPreparingPreview(false);}}
  }

  async function applyConfirmed() {
    if (!prepared || previewInvalid || !onApplyTranscript || operation.current) return;
    const actionScope = scope;
    operation.current = true; setWorking(true); setFailure(''); setNotice('');
    try {
      const message = await onApplyTranscript(prepared);
      if (!mounted.current || currentScope.current !== actionScope) return;
      setSubtitlePreview(null);
      setNotice(message || '已将预览字幕加入所选片段，可在时间线上检查并撤销。');
    } catch (error) {
      if (mounted.current && currentScope.current === actionScope) setFailure(errorMessage(error));
    } finally {operation.current = false; if (mounted.current) setWorking(false);}
  }

  const preview = (segment: TranscriptSegment) => {
    if (current && bound) onPreview(asset, transcriptPreviewSeconds(asset, current.binding, segment.start_ms));
  };

  return <section className={styles.panel} aria-label="本地对白转写与校对">
    <div className={styles.heading}><h3>对白转写与校对</h3><span>本地 medium 模型 · 每句确认后才能加入字幕</span></div>
    <p className={styles.intro}>原视频和音轨保留本机。识别结果可能漏句、重复或混入歌词；时间为估计值，需试听校对。模型解码概率不代表文字正确率。</p>
    <div className={styles.toolbar}>
      <button type="button" className={styles.primary} disabled={working || unfinished || transcriptJobActive(job) || !model?.ready}
        title={!model?.ready ? model?.reason || '正在检查本地模型' : '只在本机运行，不上传音轨'}
        onClick={() => {setHistoryId(''); void run(() => transcripts.start(asset.id, crypto.randomUUID()), '本地识别已提交；关闭面板后任务仍会继续。');}}>开始本地对白识别</button>
      <button type="button" disabled={working} onClick={() => setRefresh(value => value + 1)}>刷新记录</button>
    </div>
    {!data && !readFailure && <p role="status" className={styles.hint}>正在读取本地转写记录…</p>}
    {data && !model?.ready && <p className={styles.warning}>{model?.reason || '本地 medium 模型尚未准备好，请先完成本地模型准备。'}不会自动切换至云服务。</p>}
    {(failure || readFailure) && <p role="alert" className={styles.error}>{failure || readFailure}</p>}
    {notice && <p role="status" className={styles.notice}>{notice}</p>}

    {job && <div className={styles.job} aria-label="本地对白识别任务">
      <div className={styles.jobHeading}><strong>本地对白任务</strong><span>{transcriptJobLabel(job)} · {job.progress ?? 0}%</span>
        {transcriptJobActive(job) && <button type="button" disabled={working || !!job.cancel_requested}
          onClick={() => void run(() => transcripts.cancel(job.id), '已申请停止本地识别。')}>取消识别</button>}
        {(job.status === 'failed' || job.status === 'cancelled') && <button type="button" disabled={working || unfinished}
          onClick={() => void run(() => transcripts.retry(job.id), '本地识别重试已提交；原有结果与校对记录保留。')}>重试识别</button>}
      </div>
      <progress aria-label="本地对白识别进度" max={100} value={job.progress ?? 0}/>
      <small>任务 {job.id.slice(0, 10)} · 第 {job.attempt ?? 1} 次执行 · 以后台任务状态为准</small>
      {job.error && <p className={styles.warning}>{job.error}</p>}
    </div>}

    {!!data?.history.length && <label className={styles.history}>结果历史
      <select aria-label="切换本地转写结果" value={historyId} disabled={working || unfinished}
        onChange={event => {setHistoryId(event.target.value); setFailure(''); setNotice('');}}>
        <option value="">最新一代结果</option>
        {data.history.map(item => <option value={item.id} key={item.id}>第 {item.generation} 代 · {item.model} · 已确认 {item.confirmed_count} 句{item.created_at ? ` · ${item.created_at.replace('T', ' ').slice(0, 16)}` : ''}</option>)}
      </select>
    </label>}
    {!current && data && <p className={styles.empty}>还没有可校对的转写结果。开始本地识别后，每次成功结果会单独保存；已有人工校对不会被新一代覆盖。</p>}
    {current && !bound && <p role="alert" className={styles.error}>转写来源与当前素材版本不一致，已停止编辑和字幕加入。请刷新或重新识别当前素材。</p>}
    {current && bound && <>
      <p className={styles.hint}>第 {current.generation} 代 · {current.model} · 修订 {current.revision} · {current.segments.length} 个候选。整段识别质量尚未验收；逐句确认仅代表本次人工校对。</p>
      {current.segments.length === 0 && <p className={styles.empty}>本代没有返回对白候选。这不代表原音轨一定没有对白；可试听并调整识别方案。</p>}
      <div className={styles.segments}>
        {current.segments.map((segment, index) => {
          const input = editing && draft.segmentId === segment.id ? draft : null;
          return <article className={styles.segment} data-review={segment.status} key={segment.id}>
            <div className={styles.segmentHeading}><strong>句子 {index + 1}</strong><span>{reviewLabel[segment.status]}</span>
              <button type="button" aria-label={`试听句子 ${index + 1}`} onClick={() => preview(segment)}>{stamp(segment.start_ms)} – {stamp(segment.end_ms)} ▶</button>
            </div>
            {input ? <div className={styles.editor}>
              <label>校对文字<textarea aria-label={`句子 ${index + 1} 校对文字`} rows={3} maxLength={1000} value={input.text} disabled={working}
                onChange={event => setDraft({...input, text: event.target.value})}/></label>
              <div className={styles.times}>
                <label>音轨开始秒数<input aria-label={`句子 ${index + 1} 开始秒数`} type="text" inputMode="decimal" value={input.start} disabled={working}
                  onChange={event => setDraft({...input, start: event.target.value})}/></label>
                <label>音轨结束秒数<input aria-label={`句子 ${index + 1} 结束秒数`} type="text" inputMode="decimal" value={input.end} disabled={working}
                  onChange={event => setDraft({...input, end: event.target.value})}/></label>
              </div>
              <p className={styles.hint}>秒数相对本素材音轨起点，最多三位小数；源时间及原始候选单独保留。</p>
              {input.revision !== current.revision && <p className={styles.warning}>后台修订已变化，草稿仍保留。请复制草稿，再取消并重新编辑此句。</p>}
              <div className={styles.actions}>
                <button type="button" className={styles.primary} disabled={working} onClick={() => void save(segment, 'confirmed', input)}>保存并确认这句</button>
                <button type="button" disabled={working} onClick={() => void save(segment, 'pending', input)}>保存为待复核</button>
                <button type="button" disabled={working} onClick={() => void save(segment, 'rejected', input)}>保存并拒绝</button>
                <button type="button" disabled={working} onClick={() => {setDraft(null); setFailure('');}}>取消编辑</button>
              </div>
            </div> : <>
              <p className={styles.text}>{segment.text}</p>
              <div className={styles.actions}>
                <button type="button" disabled={working || unfinished} onClick={() => {setHistoryId(current.id); setDraft({...transcriptDraft(segment), generationId: current.id, segmentId: segment.id, revision: current.revision}); setFailure('');}}>编辑文字与时间</button>
                {segment.status !== 'confirmed' && <button type="button" disabled={working || unfinished} onClick={() => void save(segment, 'confirmed')}>确认这句</button>}
                {segment.status !== 'rejected' && <button type="button" disabled={working || unfinished} onClick={() => void save(segment, 'rejected')}>拒绝这句</button>}
                {segment.status !== 'pending' && <button type="button" disabled={working || unfinished} onClick={() => void save(segment, 'pending')}>恢复待复核</button>}
              </div>
            </>}
            {segment.status === 'pending' && <p className={styles.uncertainty}>{transcriptUncertaintyLabel(segment.uncertainty) || '模型候选，需人工试听复核'}</p>}
            <details className={styles.details}><summary>查看原始候选</summary>
              <p>{stamp(segment.raw_start_ms)} – {stamp(segment.raw_end_ms)} · {segment.raw_text}</p>
              <p>原始候选与模型不确定性记录保留。已确认的文字或时间来自人工校对，词级推测不会自动作为精确对齐。</p>
            </details>
          </article>;
        })}
      </div>
      <div className={styles.summary}>
        <div><strong>已确认 {confirmed.length} 句</strong>{targetMatches ? `所选片段：${target?.label}` : target ? '所选片段属于另一份素材；请切回相应素材，或重新选择时间线片段。' : '请先在时间线上选中这份素材的视频或音频片段。'}<br/>只加入落在该片段源范围内的已确认句子；已有字幕保留。
          <label className={styles.subtitleMode}>字幕生成方式
            <select aria-label="字幕生成方式" value={subtitleMode} disabled={working || unfinished}
              onChange={event => {setSubtitleMode(event.target.value as TranscriptSubtitleMode); setFailure(''); setNotice('');}}>
              <option value="segmented">中文分句</option><option value="original">保留原句</option>
            </select>
          </label>
          <p className={styles.hint}>{subtitleMode === 'segmented' ? '按中文标点与长度拆分字幕；分句无法成立时，可切换保留原句。' : '每个已确认原句生成一条字幕，沿用原句起止时间。'}</p>
        </div>
        <button type="button" className={styles.primary} disabled={working || unfinished || !targetMatches || !onAddTranscript || !confirmed.length}
          onClick={() => void addConfirmed()}>{subtitleMode === 'segmented' ? '预览字幕分句' : '预览原句字幕'}</button>
      </div>
      {draft && <p className={styles.warning}>有未完成的编辑，请先保存或取消，再切换结果、重跑或加入字幕。</p>}
    </>}
    {prepared && <div className={styles.subtitlePreview} aria-label={prepared.mode === 'original' ? '原句字幕预览' : '字幕分句预览'}>
      <div className={styles.previewHeading}><strong>{prepared.mode === 'original' ? '原句字幕预览' : '字幕分句预览'} · {prepared.original_count} 句 → {previewSubtitles.length} 条</strong><span>尚未加入工程</span></div>
      <p className={styles.hint}>{prepared.mode === 'original' ? '以下为工程时间；保留原句及其起止时间，不推算内部断句时间。原句时间仍可能为模型估计，需试听检查。' : '以下为工程时间；中文分句的内部断句时间按句段时长估计，需试听检查。'}整段识别质量尚未验收。</p>
      {previewInvalid && <p role="alert" className={styles.warning}>当前转写修订、素材或所选片段已变化，这份预览已失效。请取消预览后重新准备。</p>}
      <ol className={styles.previewList}>
        {previewSubtitles.map((subtitle, index) => <li key={subtitle.id}>
          <div><span>字幕 {index + 1}</span><span className={styles.previewTime}>{formatTime(subtitle.start_frame, prepared.proposal.document.fps)} – {formatTime(subtitle.end_frame, prepared.proposal.document.fps)}</span></div>
          <p>{subtitle.text}</p>
        </li>)}
      </ol>
      {previewSubtitles.length === 0 && <p className={styles.hint}>这份预览没有新增字幕。</p>}
      <div className={styles.previewActions}>
        <button type="button" className={styles.primary} disabled={working || previewInvalid || !onApplyTranscript || previewSubtitles.length === 0}
          onClick={() => void applyConfirmed()}>加入所选片段</button>
        <button type="button" disabled={working} onClick={() => {setSubtitlePreview(null); setFailure(''); setNotice('已取消字幕预览，可调整生成方式后重新准备。');}}>取消预览</button>
      </div>
      <p className={styles.hint}>加入后可在时间线上试听检查，并一次撤销本次新增字幕。预览期间请先加入或取消，再编辑转写及切换素材。</p>
    </div>}
  </section>;
}
