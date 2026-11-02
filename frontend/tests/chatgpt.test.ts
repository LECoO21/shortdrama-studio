import assert from 'node:assert/strict';
import {afterEach, test} from 'node:test';
import {createChatGPTLogin, disconnectChatGPT, getChatGPTStatus, smokeTestLabel} from '../lib/chatgpt';
import {API_BASE} from '../lib/api';

const originalFetch = globalThis.fetch;
afterEach(() => {globalThis.fetch = originalFetch;});

test('reading connection state makes only a read request and preserves unverified inference', async () => {
  const state = {status: 'connected', plan_usage: true, verified_inference: false, runtime: {ready: true}};
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/chatgpt/status`);
    assert.equal(options?.method, 'GET');
    assert.equal(options?.cache, 'no-store');
    return Response.json(state);
  };
  assert.deepEqual(await getChatGPTStatus(), state);
});

test('current-account smoke states preserve an unknown alongside a pass and expose no ledger data', async () => {
  const smoke = {text: {state: 'passed', has_unknown: true},
    frames: {state: 'unknown', has_unknown: true}, has_unknown: true, record_available: true};
  let calls = 0;
  globalThis.fetch = async (input, options) => {
    calls += 1;
    assert.equal(input, `${API_BASE}/api/v1/chatgpt/status`);
    assert.equal(options?.method, 'GET');
    return Response.json({status: 'connected', plan_usage: true, verified_inference: true,
      runtime: {ready: false}, smoke_tests: {...smoke,
        text: {...smoke.text, text: 'private-model-output'}, account: 'private-account-hash'},
      attempts: [{account: 'private-account-hash', access_token: 'private-token'}]});
  };
  const result = await getChatGPTStatus();
  assert.deepEqual(result.smoke_tests, smoke);
  assert.equal(result.verified_inference, true);
  assert.equal(result.runtime?.ready, false);
  assert.equal(calls, 1);
  assert.doesNotMatch(JSON.stringify(result), /private-|attempts/);
});

test('smoke labels distinguish success, failure, unknown, and missing evidence', () => {
  assert.equal(smokeTestLabel({state: 'passed', has_unknown: false}), '已通过');
  assert.equal(smokeTestLabel({state: 'failed', has_unknown: false}), '未通过');
  assert.equal(smokeTestLabel({state: 'unknown', has_unknown: true}), '结果未知，不计为通过');
  assert.equal(smokeTestLabel({state: 'not_tested', has_unknown: false}), '尚未验证');
  assert.equal(smokeTestLabel(), '尚未验证');
  assert.equal(smokeTestLabel({state: 'passed', has_unknown: true}), '已通过；另有结果未知的请求');
});

test('malformed smoke details cannot become verified model access', async () => {
  const item = {state: 'passed', has_unknown: false};
  for (const smoke of [
    {text: item, frames: item, has_unknown: false},
    {text: {...item, state: 'completed'}, frames: item, has_unknown: false, record_available: true},
    {text: {...item, has_unknown: 'false'}, frames: item, has_unknown: false, record_available: true},
  ]) {
    globalThis.fetch = async () => Response.json({status: 'connected', plan_usage: true,
      verified_inference: true, smoke_tests: smoke});
    await assert.rejects(getChatGPTStatus(), /连接状态暂不可用/);
  }
});

test('login is an explicit POST and accepts only the backend one-time handoff', async () => {
  const loginUrl = `${API_BASE}/auth/chatgpt/start?ticket=one-time-handoff`;
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/chatgpt/connect`);
    assert.equal(options?.method, 'POST');
    assert.equal(new Headers(options?.headers).get('X-Studio-Client'), '1');
    return Response.json({login_url: loginUrl});
  };
  assert.equal(await createChatGPTLogin(), loginUrl);
});

test('login rejects external redirects and credentials in place of the local ticket', async () => {
  for (const loginUrl of [
    'https://example.com/auth/chatgpt/start?ticket=x',
    `${API_BASE}/auth/chatgpt/start?access_token=secret`,
    `${API_BASE}/auth/chatgpt/start?ticket=x&access_token=secret`,
    `${API_BASE}/auth/chatgpt/start?ticket=x#access_token=secret`,
    `${API_BASE}/auth/chatgpt/start?ticket=`,
    `${API_BASE}/api/v1/assets?ticket=x`,
  ]) {
    globalThis.fetch = async () => Response.json({login_url: loginUrl});
    await assert.rejects(createChatGPTLogin(), /未能取得有效的登录入口/);
  }
});

test('malformed or unavailable status cannot become a successful connection', async () => {
  for (const state of [
    {status: 'connected'},
    {status: 'success', plan_usage: true, verified_inference: false},
    {status: 'connected', plan_usage: true, verified_inference: false, account_label: {token: 'private'}},
  ]) {
    globalThis.fetch = async () => Response.json(state);
    await assert.rejects(getChatGPTStatus(), /连接状态暂不可用/);
  }
  globalThis.fetch = async () => Response.json({error: {message: '本机连接组件尚未就绪'}}, {status: 503});
  await assert.rejects(getChatGPTStatus(), /本机连接组件尚未就绪/);
});

test('disconnect is an explicit protected POST and never starts inference', async () => {
  globalThis.fetch = async (input, options) => {
    assert.equal(input, `${API_BASE}/api/v1/chatgpt/disconnect`);
    assert.equal(options?.method, 'POST');
    assert.equal(new Headers(options?.headers).get('X-Studio-Client'), '1');
    return Response.json({disconnected: true, remote_revocation_confirmed: false,
      message: '已在本机断开；未能确认远端撤销。', access_token: 'must-not-reach-the-view'});
  };
  assert.deepEqual(await disconnectChatGPT(), {disconnected: true, remote_revocation_confirmed: false,
    message: '已在本机断开；未能确认远端撤销。'});
});

test('disconnect cannot report local success or remote revocation from an incomplete response', async () => {
  for (const body of [
    {disconnected: false, remote_revocation_confirmed: false},
    {disconnected: true},
    {disconnected: true, remote_revocation_confirmed: 'true'},
    {disconnected: true, remote_revocation_confirmed: true, message: {private: 'invalid'}},
  ]) {
    globalThis.fetch = async () => Response.json(body);
    await assert.rejects(disconnectChatGPT(), /未能确认本机是否已断开/);
  }
});
