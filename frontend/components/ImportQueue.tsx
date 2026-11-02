'use client';

import type {useImportQueue} from '../lib/use-import-queue';
import {formatBytes, importStatus} from '../lib/uploads';
import styles from './ImportQueue.module.css';

export default function ImportQueue({queue, productionName, onAdd}: {
  queue: ReturnType<typeof useImportQueue>; productionName?: string; onAdd: () => void;
}) {
  const entries = queue.entries;
  const pendingOrDone = entries.filter(item => item.session?.status !== 'cancelled');
  const total = pendingOrDone.reduce((sum, item) => sum + item.size, 0);
  const received = pendingOrDone.reduce((sum, item) => sum + (item.session?.offset ?? 0), 0);
  const cancelled = entries.length - pendingOrDone.length;
  const completed = entries.filter(item => item.session?.status === 'completed').length;
  return <section className={styles.panel} aria-label="素材导入队列">
    <div className={styles.header}><div><h3>{productionName ?? '当前项目'}的素材导入</h3><p>可一次选多个文件。分片与进度保留在本机，失败文件可以单独继续。</p></div><button type="button" className="primary" onClick={onAdd}>添加文件</button></div>
    {!!entries.length && <div className={styles.total}><strong>已完成 {completed} / {pendingOrDone.length} 个文件{cancelled ? ` · 已取消 ${cancelled}` : ''}</strong><span>已传 {formatBytes(received)} / {formatBytes(total)}</span><progress aria-label="整体导入进度" value={received} max={Math.max(1, total)}/></div>}
    {queue.listError && <p role="alert">记录读取失败：{queue.listError}<button type="button" onClick={() => void queue.refresh()}>重新读取</button></p>}
    <div className={styles.list}>{entries.map(item => {
      const active = ['queued', 'hashing', 'transferring'].includes(item.stage);
      const state = item.session;
      const finished = state?.status === 'completed' || state?.status === 'cancelled';
      const status = item.stage === 'hashing' ? '正在核对文件指纹' : item.stage === 'queued' ? '等待导入'
        : item.stage === 'paused' ? '已暂停 · 已传内容保留' : item.stage === 'failed' ? '未完成 · 请查看原因'
        : state ? state.status === 'uploading' && active ? '正在传输' : importStatus(state) : '等待处理';
      return <article className={styles.item} key={item.key} data-upload-id={state?.id} data-import-stage={item.stage}>
        <div className={styles.file}><strong title={item.filename}>{item.filename}</strong><span>{formatBytes(item.size)}</span></div>
        <div className={styles.status}><span>{status}</span><small>{formatBytes(state?.offset ?? 0)} / {formatBytes(item.size)}</small></div>
        {item.stage === 'hashing' ? <progress aria-label={`${item.filename} 指纹计算`}/> : <progress aria-label={`${item.filename} 导入进度`} value={state?.offset ?? 0} max={Math.max(1, item.size)}/>}
        {(item.error || state?.error) && <p role="alert" className={styles.error}>{item.error || state?.error?.message}</p>}
        {state?.status === 'completed' && <p className={styles.success}>{state.duplicate ? '已存在相同内容，复用原片并关联到本项目。' : '媒体已校验并入库，可加入时间线。'}{state.asset?.metadata.audio_stream_index == null ? ' 此素材无音轨，不能识别对白。' : ''}</p>}
        {!finished && <div className={styles.actions}>
          {active ? <button type="button" onClick={() => queue.pause(item.key)}>暂停传输</button> : <>
            {(item.file || state?.status === 'validating') && <button type="button" className="primary" onClick={() => queue.resume(item.key)}>{state?.status === 'validating' ? '继续校验' : '继续 / 重试'}</button>}
            {state?.status !== 'validating' && <label className={styles.choose}>{item.file ? '重新选择原文件' : '选择原文件继续'}<input type="file" accept=".mp4,.mov,.mkv,.webm,.wav,.mp3,.m4a" aria-label={`继续导入 ${item.filename}`} onChange={event => {const file = event.target.files?.[0]; if (file) queue.resume(item.key, file); event.target.value = '';}}/></label>}
          </>}
          {!active && <button type="button" onClick={() => void queue.cancel(item.key)}>取消这次导入</button>}
        </div>}
      </article>;
    })}</div>
    {!entries.length && <p className={styles.empty}>还没有导入记录，添加文件开始。</p>}
    <p className={styles.note}>当前单文件上限 100 MiB、单素材最长10分钟。进度100%只表示字节传完，显示“导入完成”后才可使用。刷新或重启后请重新选择原文件继续，系统会校验完整指纹；不会自动上传其他文件。</p>
  </section>;
}
