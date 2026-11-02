#!/usr/bin/env python3
"""Manage a pinned, local-only OpenFilm editor POC, separate from the product and personal OpenFilm."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / '.runtime/research/openfilm'
CLONE = PROJECT / '.runtime/poc/openfilm-0.2.6'
PIN = 'ea8cf6dcbb367065ce16d75e9784dbe2ab3e013c'
ROOT = Path.home() / 'Library/Application Support/Shortdrama Studio/poc/openfilm-0.2.6'
PACKAGE = CLONE / 'packages/openfilm'
GUARD = PROJECT / 'scripts/openfilm_poc_guard.mjs'
STATE = ROOT / 'manager.json'
RUN = ROOT / 'home/run.json'
PORTS = (4748, 4749)


def directories() -> None:
    for sub in ('home', 'shared-home', 'cache/npm', 'cache/pnpm', 'cache/chromium', 'cache/xdg', 'config', 'tmp', 'tools', 'library/Projects', 'library/Saved', 'exports', 'imports', 'logs'):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o700)


def isolated_env() -> dict[str, str]:
    # Deliberate allowlist: never inherit API keys, provider vars, Codex settings, proxies or personal OpenFilm prefs.
    node = Path(shutil.which('node') or '/usr/bin/node')
    ffmpeg = Path(shutil.which('ffmpeg') or '/usr/bin/ffmpeg')
    return {
        'PATH': os.pathsep.join(dict.fromkeys([str(node.parent), str(ffmpeg.parent), '/usr/bin', '/bin', '/usr/sbin', '/sbin'])),
        'TMPDIR': str(ROOT / 'tmp'), 'LANG': 'en_US.UTF-8', 'CI': '1',
        'XDG_CACHE_HOME': str(ROOT / 'cache/xdg'), 'XDG_CONFIG_HOME': str(ROOT / 'config'),
        'npm_config_cache': str(ROOT / 'cache/npm'), 'npm_config_userconfig': str(ROOT / 'config/npmrc'), 'npm_config_globalconfig': '/dev/null',
        'PNPM_HOME': str(ROOT / 'tools'), 'PLAYWRIGHT_BROWSERS_PATH': str(ROOT / 'cache/chromium'),
        'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1', 'ELECTRON_SKIP_BINARY_DOWNLOAD': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
        'OPENFILM_POC_ROOT': str(ROOT), 'OPENFILM_HOME': str(ROOT / 'home'),
        'OPENFILM_SHARED_HOME': str(ROOT / 'shared-home'), 'OPENFILM_LIBRARY': str(ROOT / 'library/Projects'),
        'OPENFILM_SAVED': str(ROOT / 'library/Saved'), 'OPENFILM_EXPORTS': str(ROOT / 'exports'),
        'OPENFILM_STUDIO_PORT': str(PORTS[0]), 'OPENFILM_STUDIO_WORKER': '1',
        'OPENFILM_NO_BROWSER': '1', 'OPENFILM_NO_UPDATE_CHECK': '1',
    }


def run(args: list[str], cwd: Path = PROJECT) -> None:
    subprocess.run(args, cwd=cwd, env=isolated_env(), check=True)


def apply_once(file: Path, before: str, after: str) -> None:
    content = file.read_text()
    if after in content:
        return
    if content.count(before) != 1:
        raise RuntimeError(f'Pinned source changed; refusing unsafe patch: {file.relative_to(CLONE)}')
    file.write_text(content.replace(before, after, 1))


def patch_source() -> None:
    (PACKAGE / 'studio/server/poc-guard.mjs').write_bytes(GUARD.read_bytes())
    server = PACKAGE / 'studio/server/server.mjs'
    apply_once(server, "import { randomBytes, timingSafeEqual } from 'node:crypto';", "import { guardRoute, POC_CSP } from './poc-guard.mjs';\nimport { randomBytes, timingSafeEqual } from 'node:crypto';")
    apply_once(server, "    const parts = path.slice('/api/'.length).split('/').map(decodeURIComponent);", "    const parts = path.slice('/api/'.length).split('/').map(decodeURIComponent);\n    const blocked = guardRoute(parts, req.method);\n    if (blocked) return json(res, blocked.status, blocked.body);")
    apply_once(server, '  const editor = createServer((req, res) => {', "  const editor = createServer((req, res) => {\n    res.setHeader('content-security-policy', POC_CSP);")
    apply_once(server, '  const exports = createExports({ onChange:', '  const exports = createExports({ drafts: () => null, onChange:')
    exports = PACKAGE / 'studio/server/exports.mjs'
    apply_once(exports, "export const draftApps = () => ({ jianying: Boolean(defaultDrafts('jianying')), capcut: Boolean(defaultDrafts('capcut')) });", 'export const draftApps = () => ({ jianying: false, capcut: false });')
    helpers = PACKAGE / 'studio/server/http.mjs'
    apply_once(helpers, '/** Small HTTP helpers for Studio\'s server: JSON in and out, errors as JSON. */', "/** Small HTTP helpers for Studio's server: JSON in and out, errors as JSON. */\nimport { validateRequestBody } from './poc-guard.mjs';")
    apply_once(helpers, "  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }\n  catch { throw new HttpError(400, 'the body is not JSON'); }", "  let body;\n  try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }\n  catch { throw new HttpError(400, 'the body is not JSON'); }\n  try { return validateRequestBody(req, body); } catch (error) { throw new HttpError(403, error.message, { code: 'poc-disabled' }); }")
    # Empty JSON requests must pass the same picker/path validation.
    apply_once(helpers, '  if (!size) return {};', "  if (!size) { try { return validateRequestBody(req, {}); } catch (error) { throw new HttpError(403, error.message, { code: 'poc-disabled' }); } }")
    pages = PACKAGE / 'studio/server/pages.mjs'
    apply_once(pages, "import { execFile } from 'node:child_process';", "import { POC_CSP } from './poc-guard.mjs';\nimport { execFile } from 'node:child_process';")
    apply_once(pages, "    'access-control-allow-origin': editorOrigin(),", "    'access-control-allow-origin': editorOrigin(),\n    'content-security-policy': POC_CSP,")
    # No personal keys or external service can be reached even through a later call to the subtitle helpers.
    get = PACKAGE / 'studio/server/get.mjs'
    apply_once(get, 'export async function subtitleTranslator(root) {', "export async function subtitleTranslator(root) {\n  if (process.env.OPENFILM_POC_ROOT) return null;")
    apply_once(get, 'export async function subtitleTranscriber() {', "export async function subtitleTranscriber() {\n  if (process.env.OPENFILM_POC_ROOT) return async () => { throw Object.assign(new Error('POC: cloud transcription is disabled'), { code: 'poc-disabled' }); };")
    main = PACKAGE / 'studio/server/main.mjs'
    apply_once(main, "import { spawn } from 'node:child_process';", "import { installFetchGuard } from './poc-guard.mjs';\ninstallFetchGuard();\nimport { spawn } from 'node:child_process';")
    # Headless page capture inherits an isolated profile and cannot load external resources.
    host = PACKAGE / 'src/host.mjs'
    apply_once(host, "  LIVE.add(browser);", "  const newContext = browser.newContext.bind(browser);\n  browser.newContext = async (...options) => {\n    const context = await newContext(...options);\n    await context.route('**/*', (route) => {\n      const url = new URL(route.request().url());\n      return ['data:', 'blob:'].includes(url.protocol) || (['http:', 'https:'].includes(url.protocol) && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) ? route.continue() : route.abort('blockedbyclient');\n    });\n    return context;\n  };\n  browser.newPage = async (options) => (await browser.newContext(options)).newPage();\n  LIVE.add(browser);")


def prepare(with_browser: bool) -> None:
    directories()
    if not CLONE.exists():
        if not SOURCE.exists():
            raise RuntimeError('Pinned research checkout is missing; no unpinned download will be used.')
        run(['git', 'clone', '--no-hardlinks', '--no-checkout', str(SOURCE), str(CLONE)])
        run(['git', 'checkout', '--detach', PIN], CLONE)
    actual = subprocess.check_output(['git', '-C', str(CLONE), 'rev-parse', 'HEAD'], env=isolated_env(), text=True).strip()
    if actual != PIN:
        raise RuntimeError('Wrong OpenFilm version; refusing to run.')
    node_version = subprocess.check_output(['node', '--version'], env=isolated_env(), text=True).strip()
    version = tuple(map(int, node_version.lstrip('v').split('.')))
    if version < (22, 18, 0):
        raise RuntimeError('OpenFilm needs Node 22.18 or later.')
    pnpm = ROOT / 'tools/node_modules/pnpm/bin/pnpm.cjs'
    if not pnpm.exists():
        run(['npm', 'install', '--prefix', str(ROOT / 'tools'), '--no-save', '--ignore-scripts', '--package-lock=false', '--no-audit', '--no-fund', 'pnpm@9.15.9'])
    patch_source()
    run(['node', str(pnpm), '--filter', 'openfilm', 'install', '--frozen-lockfile', '--ignore-scripts', '--store-dir', str(ROOT / 'cache/pnpm')], CLONE)
    run(['node', str(pnpm), '--filter', 'openfilm', 'build:ui'], CLONE)
    if with_browser:
        run(['node', str(pnpm), '--filter', 'openfilm', 'exec', 'playwright-core', 'install', 'chromium-headless-shell'], CLONE)
    manifest = {'version': '0.2.6', 'commit': PIN, 'clone': str(CLONE), 'private_root': str(ROOT), 'node': node_version,
                'ports': PORTS, 'desktop_started': False, 'agents_enabled': False, 'providers_enabled': False,
                'guard_sha256': hashlib.sha256(GUARD.read_bytes()).hexdigest(),
                'environment': isolated_env(), 'installed': True}
    (ROOT / 'isolation-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print('OpenFilm 0.2.6 独立编辑器准备完成。未启动 Electron、Agent 或模型服务。')


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def status() -> dict:
    state, up = read_json(STATE), read_json(RUN)
    managed = bool(alive(state.get('pid', 0)) and state.get('pid') == up.get('pid'))
    return {'running': managed, 'pid': state.get('pid') if managed else None,
            'url': up.get('origin') if managed else 'http://127.0.0.1:4748',
            'private_root': str(ROOT), 'commit': PIN, 'providers_enabled': False, 'agents_enabled': False}


def start() -> None:
    directories()
    if status()['running']:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return
    if not (PACKAGE / 'studio/ui/dist/index.html').exists():
        raise RuntimeError('Run prepare --with-browser first; start never installs or downloads anything.')
    if not (ROOT / 'isolation-manifest.json').exists():
        raise RuntimeError('Isolation manifest missing; run prepare first.')
    for port in PORTS:
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as error:
                raise RuntimeError(f'POC port {port} is occupied; existing service will not be stopped.') from error
    with (ROOT / 'logs/studio.log').open('ab') as log:
        child = subprocess.Popen(['node', 'studio/server/main.mjs'], cwd=PACKAGE, env=isolated_env(), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    STATE.write_text(json.dumps({'pid': child.pid, 'commit': PIN, 'started_at': time.time()}) + '\n')
    STATE.chmod(0o600)
    for _ in range(80):
        up = read_json(RUN)
        if up.get('pid') == child.pid and up.get('origin') == 'http://127.0.0.1:4748':
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(up['origin'] + '/api/health', timeout=1) as response:
                    if response.status == 200:
                        print(json.dumps(status(), ensure_ascii=False, indent=2))
                        return
            except OSError:
                pass
        if child.poll() is not None:
            raise RuntimeError(f'POC stopped. Inspect {ROOT / "logs/studio.log"}')
        time.sleep(0.25)
    os.killpg(child.pid, signal.SIGTERM)
    raise RuntimeError('POC did not become healthy; isolated process was stopped.')


def stop() -> None:
    state = read_json(STATE)
    pid = state.get('pid', 0)
    if alive(pid):
        # Only signal a process that still runs our exact pinned server from this isolated launch.
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        if 'node studio/server/main.mjs' not in command or read_json(RUN).get('pid') != pid:
            raise RuntimeError('PID ownership could not be verified; no process was stopped.')
        os.killpg(pid, signal.SIGTERM)
        for _ in range(40):
            if not alive(pid):
                break
            time.sleep(0.25)
        if alive(pid):
            raise RuntimeError('POC is still stopping; no force-kill was issued.')
    STATE.unlink(missing_ok=True)
    print('独立编辑器已停止，POC 素材与工程保留。')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'start', 'stop', 'status', 'open'])
    parser.add_argument('--with-browser', action='store_true', help='prepare: download only the isolated Chromium headless shell')
    args = parser.parse_args()
    directories()
    with (ROOT / 'manager.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'prepare':
            prepare(args.with_browser)
        elif args.action == 'start':
            start()
        elif args.action == 'stop':
            stop()
        elif args.action == 'status':
            print(json.dumps(status(), ensure_ascii=False, indent=2))
        elif status()['running']:
            up = read_json(RUN)
            webbrowser.open(f'{up["origin"]}/open?key={up["key"]}')
        else:
            raise RuntimeError('POC is not running. Run start first.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f'OpenFilm POC: {error}', file=sys.stderr)
        sys.exit(1)
