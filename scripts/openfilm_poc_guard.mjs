// Applied only to the pinned, disposable OpenFilm POC clone. Never imported by the product.
import { realpathSync, existsSync } from 'node:fs';
import { dirname, isAbsolute, resolve, sep } from 'node:path';

export const DENIED_MESSAGE = '独立编辑器验证：此入口暂未开放。仅处理 POC 目录内的素材副本，不连接 AI 或外部工程。';
const pathKeys = new Set(['path', 'paths', 'folder', 'at', 'to', 'from', 'projectsFolder']);

export function assertRelativePath(value) {
  let plain = value;
  for (let pass = 0; pass < 3; pass += 1) {
    let next;
    try { next = decodeURIComponent(plain); } catch { throw new Error(DENIED_MESSAGE); }
    if (next === plain) break;
    plain = next;
  }
  if (plain.includes('\0') || plain.includes('\\') || plain.startsWith('~') || plain.startsWith('//')
      || /^[a-z][a-z0-9+.-]*:/i.test(plain) || plain.split('/').includes('..')) throw new Error(DENIED_MESSAGE);
  return value;
}

export function assertInside(value, root = process.env.OPENFILM_POC_ROOT) {
  if (!root) throw new Error('OPENFILM_POC_ROOT is required');
  const base = realpathSync(root);
  const logicalBase = resolve(root);
  const target = resolve(value);
  if (target !== logicalBase && !target.startsWith(logicalBase + sep) && target !== base && !target.startsWith(base + sep)) throw new Error(DENIED_MESSAGE);
  let parent = target;
  while (!existsSync(parent) && dirname(parent) !== parent) parent = dirname(parent);
  const real = realpathSync(parent);
  if (real !== base && !real.startsWith(base + sep)) throw new Error(DENIED_MESSAGE);
  return target;
}

export function validateBody(body, root = process.env.OPENFILM_POC_ROOT) {
  const visit = (value, key = '') => {
    if (typeof value === 'string') {
      if (pathKeys.has(key)) {
        assertRelativePath(value);
        if (isAbsolute(value)) assertInside(value, root);
      }
      if (key === 'src') {
        assertRelativePath(value);
        if (isAbsolute(value)) throw new Error(DENIED_MESSAGE);
      }
      return;
    }
    if (Array.isArray(value)) { for (const item of value) visit(item, key); return; }
    if (value && typeof value === 'object') for (const [child, item] of Object.entries(value)) visit(item, child);
  };
  visit(body);
  return body;
}

// Called after upstream authorization, before any provider, repository, picker or system action.
export function guardRoute(parts, method) {
  const top = parts[0];
  if (method === 'GET' && top === 'providers' && parts.length === 1) return { status: 200, body: { providers: [], use: {}, model: {}, services: [] } };
  if (method === 'GET' && top === 'examples' && parts.length === 1) return { status: 200, body: { examples: [] } };
  if (method === 'GET' && top === 'skills' && parts.length === 1) return { status: 200, body: { skills: [] } };
  if (method === 'GET' && top === 'version') return { status: 200, body: { version: '0.2.6', latest: null, newer: false, checkedAt: null, checking: false, source: true } };
  const denied = new Set(['providers', 'get', 'examples', 'skills', 'update', 'desktop', 'pick-files', 'pick-folder', 'browse', 'doctor', 'diagnostics']);
  if (denied.has(top) || (top === 'projects' && parts[1] === 'fetch')
      || (top === 'projects' && parts[2] === 'subtitles' && ['translate', 'transcribe'].includes(parts[3]))
      || parts.includes('relink') || parts.includes('reveal')
      || (top === 'projects' && parts[2] === 'exports' && parts[4] === 'open')
      || (top === 'locations' && method !== 'GET')) return { status: 403, body: { error: DENIED_MESSAGE, code: 'poc-disabled' } };
  return null;
}

export function validateRequestBody(req, body) {
  validateBody(body);
  const route = new URL(req.url ?? '/', 'http://127.0.0.1').pathname;
  const absolute = (value) => {
    if (typeof value !== 'string' || !isAbsolute(value)) throw new Error(DENIED_MESSAGE);
    return assertInside(value);
  };
  if ((route === '/api/projects' || route === '/api/open' || /^\/api\/projects\/[^/]+\/locate$/.test(route)) && body?.path != null) absolute(body.path);
  if (route === '/api/prefs' && body?.projectsFolder != null) absolute(body.projectsFolder);
  if (/^\/api\/projects\/[^/]+\/exports$/.test(route)) {
    if (body?.folder != null) absolute(body.folder);
    if (Array.isArray(body?.jobs)) for (const job of body.jobs) if (job?.folder != null) absolute(job.folder);
  }
  if (route.endsWith('/files/bring')) {
    if (!Array.isArray(body?.paths) || body.paths.length === 0 || body.mode === 'link') throw new Error(DENIED_MESSAGE);
    for (const path of body.paths) absolute(path);
  }
  return body;
}

export function isLocalRequest(input) {
  try {
    const value = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    return ['http:', 'https:'].includes(value.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(value.hostname)
      && ['4748', '4749'].includes(value.port);
  } catch { return false; }
}

export function installFetchGuard() {
  const original = globalThis.fetch;
  globalThis.fetch = (input, options) => {
    if (!isLocalRequest(input)) return Promise.reject(new Error(DENIED_MESSAGE));
    return original(input, options);
  };
}

// Blocks external subresources, websocket/fetch, forms and embedded remote pages in editor and preview.
export const POC_CSP = "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: http://127.0.0.1:4748 http://localhost:4749; media-src 'self' data: blob: http://127.0.0.1:4748 http://localhost:4749; font-src 'self' data:; connect-src 'self' http://127.0.0.1:4748 http://localhost:4749 ws://127.0.0.1:4748; frame-src 'self' blob: http://localhost:4749; form-action 'none'; object-src 'none'; base-uri 'self'";
