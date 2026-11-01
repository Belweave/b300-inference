"""Detached node tunnel helper and unauthenticated public reachability check."""
import asyncio
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit


def cached_binary_verified(vendor_script, binary=Path('/tmp/cloudflared')):
    # Use the vendored helper's pins, so its version/checksums have one owner.
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    try:
        pins = dict(re.findall(r'ARCH=(amd64|arm64); SHA=([0-9a-f]{64})', vendor_script.read_text()))
        if arch not in pins or not binary.is_file() or not os.access(binary, os.X_OK):
            return False
        digest = hashlib.sha256()
        with binary.open('rb') as stream:
            while chunk := stream.read(1024*1024):
                digest.update(chunk)
        return digest.hexdigest() == pins[arch]
    except OSError:
        return False


def prerequisites(env, token, vendor_script):
    script = Path(env['HOME'])/'node-tunnel.sh'
    result = {'status': 'FAIL', 'script': str(script), 'token_present': bool(token),
              'missing_tools': [], 'cached_binary_verified': False}
    if not script.is_file() or not os.access(script, os.X_OK):
        result['reason'] = 'tunnel script ~/node-tunnel.sh missing or not executable'
    elif not token:
        result['reason'] = 'CF_TUNNEL_TOKEN missing or empty in '+env['NODE_ENV_FILE']
    else:
        result['missing_tools'] = [name for name in ('curl', 'sha256sum')
                                   if not shutil.which(name, path=env.get('PATH'))]
        if result['missing_tools']:
            result['cached_binary_verified'] = cached_binary_verified(vendor_script)
        if result['missing_tools'] and not result['cached_binary_verified']:
            result['reason'] = ('tunnel requires '+', '.join(result['missing_tools'])+
                                ' or an already verified /tmp/cloudflared')
        else:
            result.update(status='PASS', reason=None)
    return result


def public_url(env, default):
    testing = env.get('NODE_RUN_TESTING') == '1'
    override = env.get('NODE_RUN_TUNNEL_PUBLIC_URL')
    url = (override if testing and override else env.get('TUNNEL_PUBLIC_URL', default)).rstrip('/')
    parsed = urlsplit(url)
    scheme_ok = parsed.scheme == 'https'
    if testing and override:
        scheme_ok = parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if (not scheme_ok or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('TUNNEL_PUBLIC_URL must be HTTPS without credentials; test overrides must be loopback HTTP')
    return url


def launch(script, env, token, timeout):
    # Deliberate allow-list: no classifier/upstream key, guard config or shell options.
    child_env = {name: env[name] for name in ('PATH', 'HOME') if name in env}
    child_env['CF_TUNNEL_TOKEN'] = token
    proc = subprocess.Popen([str(script)], env=child_env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    reason = 'launch failed'
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        reason = 'timeout'
        # Stop only a stuck helper, never the detached tunnel or engine/guards.
        proc.kill()
        try:
            output, _ = proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            output = exc.output or b''
            proc.stdout.close()
            proc.wait(timeout=1)
    text = output.decode(errors='replace')
    pids = re.findall(r'^tunnel started, pid ([1-9][0-9]*)$', text, re.MULTILINE)
    if proc.returncode == 0 and len(pids) == 1:
        return int(pids[0]), None, text
    return None, reason, text


async def reachable(url, until):
    import aiohttp
    # No Authorization, cookies, proxy environment or redirects. A relay-shaped
    # 401 proves the request reached the application rather than an edge error.
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        while (remaining := until-time.monotonic()) > 0:
            try:
                async with session.post(url+'/v1/safety/csam', json={'text': 'hello world', 'phase': 'input'},
                                        allow_redirects=False,
                                        timeout=aiohttp.ClientTimeout(total=min(3, remaining))) as response:
                    if response.status == 401 and await response.json() == {'error': 'unauthorized'}:
                        return True
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError):
                pass
            await asyncio.sleep(min(5, max(0, until-time.monotonic())))
    return False
