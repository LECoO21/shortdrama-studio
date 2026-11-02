'use client';

import Link from 'next/link';
import {useEffect, useRef, useState} from 'react';
import {createChatGPTLogin, disconnectChatGPT, getChatGPTStatus, smokeTestLabel} from '@/lib/chatgpt';
import type {ChatGPTDisconnectResult, ChatGPTStatus} from '@/lib/chatgpt';
import styles from './page.module.css';

const labels: Record<ChatGPTStatus['status'], string> = {
  disconnected: '未连接', connecting: '连接中', connected: '已连接', needs_login: '需要重新登录', error: '连接出现问题',
};
const descriptions: Record<ChatGPTStatus['status'], string> = {
  disconnected: '点击下方按钮，在官方授权页面连接你的 ChatGPT。',
  connecting: '正在等待授权结果。完成官方页面的操作后，连接状态会在这里更新。',
  connected: '你的 ChatGPT 已连接。下面分别显示文字和两帧画面的接入验证记录。',
  needs_login: '登录已失效，请重新连接。你的工程和素材会继续保留。',
  error: '本次连接未完成。刷新状态，或重新连接后再试。',
};

export default function AISettings() {
  const [connection, setConnection] = useState<ChatGPTStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<'connect' | 'disconnect' | null>(null);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [disconnectResult, setDisconnectResult] = useState<ChatGPTDisconnectResult | null>(null);
  const [refresh, setRefresh] = useState(0);
  const operation = useRef(false);

  useEffect(() => {
    // Callback query text is not evidence of a connection. Read server state,
    // and leave no callback parameters in the visible URL or subsequent links.
    window.history.replaceState(window.history.state, '', '/settings/ai');
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function poll() {
      try {
        const result = await getChatGPTStatus(controller.signal);
        if (stopped) return;
        setConnection(result); setReadError(''); setLoading(false);
        timer = setTimeout(poll, result.status === 'connecting' ? 1500 : 5000);
      } catch (failure) {
        if (stopped) return;
        setReadError(failure instanceof Error ? failure.message : '读取连接状态失败，请稍后重试。');
        setLoading(false);
        timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {stopped = true; controller.abort(); if (timer) clearTimeout(timer);};
  }, [refresh]);

  async function connect() {
    if (operation.current) return;
    operation.current = true; setBusy('connect'); setError(''); setDisconnectResult(null);
    try {
      const loginUrl = await createChatGPTLogin();
      window.location.assign(loginUrl);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '未能打开登录入口，请刷新状态后重试。');
      setRefresh(value => value + 1);
    } finally {operation.current = false; setBusy(null);}
  }

  async function disconnect() {
    if (operation.current) return;
    operation.current = true; setBusy('disconnect'); setError('');
    try {
      const result = await disconnectChatGPT();
      setDisconnectResult(result);
      setConnection(null); setLoading(true);
      setRefresh(value => value + 1);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : '断开未完成，请刷新状态后重试。');
    } finally {operation.current = false; setBusy(null);}
  }

  const status = connection?.status;
  const connected = status === 'connected';
  const connecting = status === 'connecting';
  const unavailable = Boolean(readError);
  const label = unavailable ? '暂时无法确认连接' : loading ? '正在读取连接状态' : status ? labels[status] : '未读取到连接状态';

  return <main className={styles.page}>
    <div className={styles.content}>
      <Link href="/" className={styles.back}>← 返回剪辑工作台</Link>
      <header className={styles.heading}>
        <span className={styles.eyebrow}>序场 STUDIO · AI 设置</span>
        <h1>连接你的 ChatGPT</h1>
        <p>使用你的 ChatGPT 账号为素材理解和剪辑建议提供 AI 能力。</p>
      </header>

      <section className={styles.card} aria-labelledby="connection-heading">
        <div className={styles.statusRow}>
          <h2 id="connection-heading">ChatGPT 连接</h2>
          <span className={styles.status} data-state={unavailable ? 'error' : status ?? 'loading'} aria-live="polite"><i/>{label}</span>
        </div>
        <p className={styles.description}>{unavailable ? '未能取得最新状态，暂不判断是否连接成功。' : status ? descriptions[status] : '正在向本机服务查询，请稍候。'}</p>
        {!unavailable && connection?.account_label && connected && <p className={styles.account}>当前账号：{connection.account_label}</p>}
        {!unavailable && connection?.message && <p className={styles.detail}>{connection.message}</p>}
        {(error || readError) && <p className={styles.error} role="alert">{error || readError}</p>}
        <div className={styles.actions}>
          {!connected && <button type="button" className="primary" disabled={Boolean(busy) || connecting || loading || unavailable} onClick={() => void connect()}>{busy === 'connect' ? '正在打开官方登录…' : 'Continue with ChatGPT'}</button>}
          {(connected || connecting) && <button type="button" disabled={Boolean(busy) || loading} onClick={() => void disconnect()}>{busy === 'disconnect' ? '正在断开…' : connecting ? '取消本次连接' : '断开连接'}</button>}
          <button type="button" disabled={Boolean(busy) || loading} onClick={() => {setError(''); setLoading(true); setRefresh(value => value + 1);}}>刷新状态</button>
        </div>
        {disconnectResult && <div className={styles.disconnectResult} role="status">
          <strong>已在本机断开连接</strong>
          {disconnectResult.message && <p>{disconnectResult.message}</p>}
          {!disconnectResult.remote_revocation_confirmed && <p>远端授权撤销尚未确认，请前往 ChatGPT 设置中的应用授权管理，断开此应用。</p>}
        </div>}
        <p className={styles.caption}>登录会跳转到官方授权页面，无须在工作台填写密码、API Key 或令牌。</p>
        {!unavailable && connected && connection && <dl className={styles.facts}>
          <div><dt>ChatGPT 订阅额度</dt><dd>{connection.plan_usage ? '已授权使用；本页无法读取剩余额度' : '尚未授权使用订阅额度'}</dd></div>
          <div><dt>模型接入验证</dt><dd>{connection.verified_inference ? '当前账号已有真实验证通过' : '尚无已通过的真实验证'}</dd></div>
          <div><dt>文字验证</dt><dd>{smokeTestLabel(connection.smoke_tests?.text)}</dd></div>
          <div><dt>两帧画面验证</dt><dd>{smokeTestLabel(connection.smoke_tests?.frames)}</dd></div>
        </dl>}
        {!unavailable && connected && connection?.smoke_tests?.has_unknown && <p className={styles.runtime} role="status">
          {connection.smoke_tests.record_available ? '有服务请求的结果未知，不计为通过，也不会自动重试。' : '暂时无法读取本机验证记录，不能确认验证结果。'}
        </p>}
        {!unavailable && connection?.runtime && !connection.runtime.ready && <p className={styles.runtime}>能力边界{connection.runtime.reason ? `：${connection.runtime.reason}` : '：完整素材分析与自动粗剪尚未开放。'}</p>}
        <p className={styles.caption}>刷新状态只读取本机记录，不会重新调用模型。</p>
      </section>

      <section className={styles.card} aria-labelledby="data-heading">
        <h2 id="data-heading">连接后，会使用哪些数据？</h2>
        <ul className={styles.dataList}>
          <li><strong>发送至 OpenAI</strong><span>仅在你主动分析时发送选定画面帧和完成任务所需的文字，例如对白、镜头描述或剪辑目标。</span></li>
          <li><strong>保留在本机</strong><span>原始视频、音轨和剪辑工程保留在本机；转写与检索计划由本地组件处理。</span></li>
          <li><strong>费用与额度</strong><span>使用你现有账号的可用额度，额度可能受账号或工作空间限制。工作台不会自动购买额外额度。</span></li>
        </ul>
        <p className={styles.caption}>连接本身不会上传素材或开始推理。OpenAI 对所发送内容的处理与留存，以你的账号设置和适用条款为准。</p>
      </section>

      <aside className={styles.note}><strong>接入验证通过，不等于 AI 剪辑已经验收。</strong><p>文字与两帧测试只验证模型接入。转写、检索、完整素材分析与自动粗剪还需继续实现和验证；现有手动剪辑、保存和导出可继续使用。</p></aside>
    </div>
  </main>;
}
