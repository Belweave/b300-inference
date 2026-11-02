"""Stage pinned guard weights into a local dir so sglang never downloads at launch.

Each file comes from, in order: an already verified local copy, the R2 mirror,
then Hugging Face at the same revision. Every byte is checked against the
committed manifest, so an unverified file is never left where a guard loads it.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import urllib.request
from urllib.parse import quote

from mirror_auth import parse_mirror_auth
# One mirror origin rule and one no-redirect handler, shared with the cloudflared fetch.
from node_tunnel import NoRedirect, mirror_base, testing_value

MANIFEST = Path(__file__).resolve().parents[1]/'config/manifests/guards.json'
HF_URL = 'https://huggingface.co'
CHUNK = 1024*1024


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


def matches(path, f):
    if not path.is_file() or path.stat().st_size != f['size']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest() == f['sha256']


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
    origin = mirror_base(env)
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
                counts[name] = counts.get(name, 0)+1
                break
            tmp.unlink(missing_ok=True)
        else:
            target.unlink(missing_ok=True)
            raise ValueError(f'no verified {repo} weights ('+'; '.join(failed)+')')
    return {'dir': str(dest), 'files': len(files), 'bytes': sum(f['size'] for f in files),
            'sources': counts, 'failed': failed}
