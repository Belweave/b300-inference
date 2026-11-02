"""Stage pinned guard weights into a local dir so sglang never downloads at launch.

Each file comes from, in order: an already verified local copy, the R2 mirror,
then Hugging Face at the same revision. Every byte is checked against the
committed manifest, so an unverified file is never left where a guard loads it.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from time import time_ns
import urllib.request
from urllib.parse import quote, urlsplit

from mirror_auth import parse_mirror_auth

MANIFEST = Path(__file__).resolve().parents[1]/'config/manifests/guards.json'
HF_URL = 'https://huggingface.co'
CHUNK = 1024*1024


def testing_value(env, name, default):
    """Fixed on the node; only the offline rehearsal may move these."""
    if env.get('NODE_RUN_TESTING') == '1' and env.get(name):
        return env[name]
    return default


def pinned_files(env, repo, revision):
    """The committed file list for exactly this repo and revision, or a refusal."""
    raw = json.loads(Path(testing_value(env, 'GUARD_MANIFEST', MANIFEST)).read_text())
    match = [g for g in raw['guards'] if g['repo'] == repo and g['revision'] == revision]
    if len(match) != 1:
        raise ValueError(f'{repo} has no committed manifest for that revision')
    files = match[0]['files']
    for f in files:
        path = PurePosixPath(f['filename'])
        if (path.is_absolute() or '..' in path.parts or '\\' in f['filename']
                or not isinstance(f['size'], int) or f['size'] < 0
                or not re.fullmatch(r'[0-9a-f]{64}', f['sha256'])):
            raise ValueError(f'{repo} manifest has an unsafe entry')
    return files


def mirror_origin(env):
    """MIRROR_URL is the model base (<origin>/models/<name>); guards live under the origin."""
    url = env.get('MIRROR_URL', '').rstrip('/')
    if not url:
        return None
    parsed = urlsplit(url)
    loopback = env.get('NODE_RUN_TESTING') == '1' and parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')
    if (parsed.scheme != 'https' and not loopback) or parsed.username or parsed.password or parsed.query:
        raise ValueError('MIRROR_URL must be HTTPS without credentials')
    return f'{parsed.scheme}://{parsed.netloc}'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # The mirror header must never follow a redirect to another host.
    def redirect_request(self, *args, **kwargs):
        return None


def stamp(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino, s.st_dev]


def marker(path):
    # Beside the weights, so a copied or remounted dir never inherits a stale marker.
    return path.parent/'.verified'/(path.name+'.json')


def matches(path, f):
    """Hash once; after that a marker with the same inode, size and times is enough.
    Any write changes ctime, which can't be set back. Some filesystems keep ctime in
    coarse ticks, so a marker only counts for files last changed a second before it
    was written (git's racy-file rule); anything newer gets hashed again."""
    if not path.is_file() or path.stat().st_size != f['size']:
        return False
    try:
        digest, written, *saved = json.loads(marker(path).read_text())
        now = stamp(path)
        if digest == f['sha256'] and saved == now and now[2] < written-1_000_000_000:
            return True
    except (OSError, ValueError):
        pass
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != f['sha256']:
        return False
    remember(path, f)
    return True


def remember(path, f):
    marker(path).parent.mkdir(exist_ok=True)
    marker(path).write_text(json.dumps([f['sha256'], time_ns(), *stamp(path)]))


def missing_bytes(env, repo, revision, root):
    """Bytes still to download for this guard: files that are absent or don't verify."""
    dest = Path(root)/repo/revision
    return sum(f['size'] for f in pinned_files(env, repo, revision) if not matches(dest/f['filename'], f))


def verify(env, repo, revision, root):
    """Launch-time check: every pinned file is present and verified, or refuse."""
    dest = Path(root)/repo/revision
    if not all(matches(dest/f['filename'], f) for f in pinned_files(env, repo, revision)):
        raise ValueError(f'{repo} weights are missing or changed since preflight')
    return str(dest.absolute())


def fetch(url, headers, redirects, target, f, timeout):
    """Stream to target while hashing; True only if size and SHA256 both match."""
    handlers = [urllib.request.ProxyHandler({})] + ([] if redirects else [NoRedirect()])
    request = urllib.request.Request(url, headers=headers)
    digest, size = hashlib.sha256(), 0
    with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response, target.open('wb') as out:
        while chunk := response.read(CHUNK):
            size += len(chunk)
            if size > f['size']:
                return False
            digest.update(chunk)
            out.write(chunk)
    return size == f['size'] and digest.hexdigest() == f['sha256']


def stage(env, repo, revision, root, timeout=60):
    """Leave every pinned file verified under root/<repo>/<revision>, or raise.

    Returns the dir plus how many files came from each source and which sources
    failed. Errors name only the guard, file and source, never a URL or header."""
    files = pinned_files(env, repo, revision)
    dest = (Path(root)/repo/revision).absolute()
    dest.mkdir(parents=True, exist_ok=True)
    sources = []
    origin = mirror_origin(env)
    if origin:
        auth = parse_mirror_auth(env.get('MIRROR_AUTH_HEADER'))
        if auth:
            sources.append(('mirror', f'{origin}/models/{repo}/{revision}/', auth.headers(), False))
    # Same pinned revision, no credential, and HF redirects to its CDN.
    sources.append(('hf', f"{testing_value(env, 'GUARD_HF_URL', HF_URL)}/{repo}/resolve/{revision}/", {}, True))
    counts, failed = {}, []
    for f in files:
        target = dest/f['filename']
        if matches(target, f):
            counts['verified-local'] = counts.get('verified-local', 0)+1
            continue
        # Drop the bad copy first, so disk use never peaks above what preflight checked.
        target.unlink(missing_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name+'.part')
        for name, base, headers, redirects in sources:
            try:
                ok = fetch(base+quote(f['filename']), headers, redirects, tmp, f, timeout)
            except Exception:
                ok = False
                failed.append(f"{name}: {f['filename']} download failed")
            else:
                if not ok:
                    failed.append(f"{name}: {f['filename']} checksum mismatch")
            if ok:
                tmp.replace(target)
                remember(target, f)
                counts[name] = counts.get(name, 0)+1
                break
            tmp.unlink(missing_ok=True)
        else:
            raise ValueError(f'no verified {repo} weights ('+'; '.join(failed)+')')
    return {'dir': str(dest), 'files': len(files), 'bytes': sum(f['size'] for f in files),
            'sources': counts, 'failed': failed}
