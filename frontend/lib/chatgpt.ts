import {API_BASE} from './api';

export type ChatGPTSmokeTest = {
  state: 'passed' | 'failed' | 'unknown' | 'not_tested';
  has_unknown: boolean;
};

export type ChatGPTSmokeTests = {
  text: ChatGPTSmokeTest;
  frames: ChatGPTSmokeTest;
  has_unknown: boolean;
  record_available: boolean;
};

export type ChatGPTStatus = {
  status: 'disconnected' | 'connecting' | 'connected' | 'needs_login' | 'error';
  account_label?: string | null;
  plan_usage: boolean;
  verified_inference: boolean;
  smoke_tests?: ChatGPTSmokeTests | null;
  message?: string | null;
  runtime?: {ready: boolean; reason?: string | null} | null;
};

export type ChatGPTDisconnectResult = {
  disconnected: true;
  remote_revocation_confirmed: boolean;
  message?: string | null;
};

const statuses = new Set<ChatGPTStatus['status']>(['disconnected', 'connecting', 'connected', 'needs_login', 'error']);
const smokeStates = new Set<ChatGPTSmokeTest['state']>(['passed', 'failed', 'unknown', 'not_tested']);

function validSmokeTest(value: unknown): value is ChatGPTSmokeTest {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<ChatGPTSmokeTest>;
  return Boolean(item.state && smokeStates.has(item.state) && typeof item.has_unknown === 'boolean');
}

export function smokeTestLabel(item?: ChatGPTSmokeTest | null): string {
  if (!item || item.state === 'not_tested') return '尚未验证';
  if (item.state === 'unknown') return '结果未知，不计为通过';
  const label = item.state === 'passed' ? '已通过' : '未通过';
  return item.has_unknown ? `${label}；另有结果未知的请求` : label;
}

async function request(path: string, method = 'GET', signal?: AbortSignal): Promise<unknown> {
  const response = await fetch(`${API_BASE}/api/v1/chatgpt/${path}`, {
    method, cache: 'no-store', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(15000)]) : AbortSignal.timeout(15000),
    headers: {'X-Studio-Client': '1'},
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.error?.message ?? '暂时无法连接本机服务，请确认工作台服务正在运行后重试。');
  return body;
}

export async function getChatGPTStatus(signal?: AbortSignal): Promise<ChatGPTStatus> {
  const body = await request('status', 'GET', signal) as Partial<ChatGPTStatus> | null;
  if (!body || !body.status || !statuses.has(body.status) || typeof body.plan_usage !== 'boolean'
    || typeof body.verified_inference !== 'boolean'
    || (body.account_label != null && typeof body.account_label !== 'string')
    || (body.message != null && typeof body.message !== 'string')
    || (body.smoke_tests != null && (!validSmokeTest(body.smoke_tests.text)
      || !validSmokeTest(body.smoke_tests.frames) || typeof body.smoke_tests.has_unknown !== 'boolean'
      || typeof body.smoke_tests.record_available !== 'boolean'))
    || (body.runtime && (typeof body.runtime.ready !== 'boolean'
      || (body.runtime.reason != null && typeof body.runtime.reason !== 'string')))) {
    throw new Error('连接状态暂不可用，请刷新状态重试。');
  }
  // Keep only the public status contract, even if an unexpected response adds
  // a ledger, model output or credential-shaped field.
  const result: ChatGPTStatus = {status: body.status, plan_usage: body.plan_usage,
    verified_inference: body.verified_inference};
  if (body.account_label !== undefined) result.account_label = body.account_label;
  if (body.message !== undefined) result.message = body.message;
  if (body.runtime !== undefined) result.runtime = body.runtime == null ? null : {
    ready: body.runtime.ready, ...(body.runtime.reason !== undefined ? {reason: body.runtime.reason} : {}),
  };
  if (body.smoke_tests !== undefined) result.smoke_tests = body.smoke_tests == null ? null : {
    text: {state: body.smoke_tests.text.state, has_unknown: body.smoke_tests.text.has_unknown},
    frames: {state: body.smoke_tests.frames.state, has_unknown: body.smoke_tests.frames.has_unknown},
    has_unknown: body.smoke_tests.has_unknown, record_available: body.smoke_tests.record_available,
  };
  return result;
}

export async function createChatGPTLogin(): Promise<string> {
  const body = await request('connect', 'POST') as {login_url?: unknown} | null;
  // The browser only receives a one-time local handoff, never OAuth credentials.
  // Do not turn an unexpected server response into an arbitrary-site redirect.
  try {
    if (typeof body?.login_url !== 'string') throw new Error();
    const url = new URL(body.login_url);
    if (url.origin !== new URL(API_BASE).origin || url.pathname !== '/auth/chatgpt/start'
      || url.username || url.password || url.hash || url.searchParams.size !== 1 || !url.searchParams.get('ticket')) throw new Error();
    return url.href;
  } catch {
    throw new Error('未能取得有效的登录入口，请刷新状态后重试。');
  }
}

export async function disconnectChatGPT(): Promise<ChatGPTDisconnectResult> {
  const body = await request('disconnect', 'POST') as Partial<ChatGPTDisconnectResult> | null;
  if (!body || body.disconnected !== true || typeof body.remote_revocation_confirmed !== 'boolean'
    || (body.message != null && (typeof body.message !== 'string' || body.message.length > 1000))) {
    throw new Error('未能确认本机是否已断开，请刷新状态后重试。');
  }
  return {disconnected: true, remote_revocation_confirmed: body.remote_revocation_confirmed,
    message: body.message};
}
