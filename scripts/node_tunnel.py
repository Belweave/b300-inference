"""Tunnel connector: a verified cloudflared, Netops' helper, and real readiness checks."""
import asyncio
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

from mirror_auth import parse_mirror_auth

# Cloudflare's bot check rejects urllib's default user agent with 403 (error 1010).
USER_AGENT = 'b300-inference'

CLOUDFLARED_VERSION = '2026.10.7'
CLOUDFLARED_SHA256 = '1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872'
# Object path under the MIRROR_URL origin. Netops uploads the binary (and a .sha256 beside it) here.
MIRROR_OBJECT = f'tools/cloudflared/{CLOUDFLARED_VERSION}/cloudflared-linux-amd64'
GITHUB_URL = ('https://github.com/cloudflare/cloudflared/releases/download/'
              f'{CLOUDFLARED_VERSION}/cloudflared-linux-amd64')
# Netops' helper reuses this path when its checksum matches, and appends its log here.
BINARY = Path('/tmp/cloudflared')
LOG = Path('/tmp/cloudflared.log')
# cloudflared logs this once an edge connection is up; it is the readiness signal.
REGISTERED = b'Registered tunnel connection'


def testing_value(env, name, default):
    """Paths and URLs are fixed on the node; only the offline rehearsal may move them."""
    if env.get('NODE_RUN_TESTING') == '1' and env.get(name):
        return env[name]
    return default


def expected_sha(env):
    value = env.get('CLOUDFLARED_SHA256') or CLOUDFLARED_SHA256
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('CLOUDFLARED_SHA256 must be 64 lowercase hex digits')
    return value


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            digest.update(chunk)
    return digest.hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # The mirror header must never follow a redirect to another host.
    def redirect_request(self, *args, **kwargs):
        return None


def mirror_base(env):
    url = env.get('MIRROR_URL', '').rstrip('/')
    if not url:
        return None
    parsed = urlsplit(url)
    loopback = env.get('NODE_RUN_TESTING') == '1' and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
    if (parsed.scheme != 'https' and not loopback) or parsed.username or parsed.password or parsed.query:
        raise ValueError('MIRROR_URL must be HTTPS without credentials')
    # MIRROR_URL is the model base (<origin>/models/<name>); tools live at the origin root.
    return f'{parsed.scheme}://{parsed.netloc}'


def download(url, headers, target, timeout, redirects):
    handlers = [urllib.request.ProxyHandler({})] + ([] if redirects else [NoRedirect()])
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **headers})
    with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response, target.open('wb') as out:
        shutil.copyfileobj(response, out, 1024*1024)


def stage_binary(env, timeout=120):
    """Leave a checksum-verified cloudflared at BINARY; never keep an unverified file.

    Order: an already verified binary, the R2 mirror, then the GitHub release of
    the same version. Returns where it came from and which sources failed."""
    if platform.machine() != 'x86_64':
        raise ValueError('cloudflared is pinned for x86_64 only')
    want = expected_sha(env)
    dest = Path(testing_value(env, 'CLOUDFLARED_BIN', BINARY))
    if dest.is_file() and sha256(dest) == want:
        dest.chmod(0o755)
        return {'source': 'cached', 'failed': [], 'sha256': want}
    sources = []
    base = mirror_base(env)
    if base:
        auth = parse_mirror_auth(env.get('MIRROR_AUTH_HEADER'))
        sources.append(('mirror', base+'/'+MIRROR_OBJECT, auth.headers() if auth else {}, False))
    sources.append(('github', testing_value(env, 'CLOUDFLARED_GITHUB_URL', GITHUB_URL), {}, True))
    failed = []
    tmp = dest.with_name(dest.name+'.new')
    for name, url, headers, redirects in sources:
        try:
            download(url, headers, tmp, timeout, redirects)
            if sha256(tmp) == want:
                tmp.chmod(0o755)
                tmp.replace(dest)
                return {'source': name, 'failed': failed, 'sha256': want}
            failed.append(name+': checksum mismatch')
        except Exception:
            # No URL, header or response detail; the header may carry a credential.
            failed.append(name+': download failed')
        finally:
            tmp.unlink(missing_ok=True)
    raise ValueError('no verified cloudflared ('+'; '.join(failed)+')')


def prerequisites(env, token):
    script = Path(env['HOME'])/'node-tunnel.sh'
    result = {'status': 'FAIL', 'script': str(script), 'token_present': bool(token), 'missing_tools': []}
    if not script.is_file() or not os.access(script, os.X_OK):
        result['reason'] = 'tunnel script ~/node-tunnel.sh missing or not executable'
    elif not token:
        result['reason'] = 'CF_TUNNEL_TOKEN missing or empty in '+env['NODE_ENV_FILE']
    elif not shutil.which('sha256sum', path=env.get('PATH')):
        # The helper checks the staged binary with sha256sum before it reuses it.
        result.update(missing_tools=['sha256sum'], reason='tunnel requires sha256sum')
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
    reason = 'helper failed'
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        reason = 'helper timeout'
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


def alive(pid, name=None):
    try:
        comm = Path(f'/proc/{int(pid)}/comm').read_text().strip()
    except (OSError, ValueError, TypeError):
        return False
    return name is None or comm == name


def log_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def registered(path, offset):
    """True once the shared log, after offset, has a registered edge connection.
    Reads the raw log in blocks and never stores or prints it."""
    try:
        with path.open('rb') as stream:
            stream.seek(offset)
            tail = b''
            while block := stream.read(1024*1024):
                data = tail+block
                if REGISTERED in data:
                    return True
                tail = data[-len(REGISTERED):]
    except FileNotFoundError:
        pass
    return False


async def reachable(url, until, private=False):
    import aiohttp
    # No Authorization, cookies, proxy environment or redirects. A 401 from the
    # relay (or, with PRIVATE_UNGUARDED=1, the engine) proves the request reached
    # the application rather than an edge error page.
    async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        while (remaining := until-time.monotonic()) > 0:
            timeout = aiohttp.ClientTimeout(total=min(3, remaining))
            try:
                if private:
                    # Without SAFETY the relay has no /v1/safety/csam route; that path
                    # would 404 forever, which is how a live tunnel looked like a timeout.
                    async with session.get(url+'/v1/models', allow_redirects=False, timeout=timeout) as response:
                        if response.status == 401:
                            return True
                else:
                    async with session.post(url+'/v1/safety/csam', json={'text': 'hello world', 'phase': 'input'},
                                            allow_redirects=False, timeout=timeout) as response:
                        if response.status == 401 and await response.json() == {'error': 'unauthorized'}:
                            return True
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError):
                pass
            await asyncio.sleep(min(2, max(0, until-time.monotonic())))
    return False
