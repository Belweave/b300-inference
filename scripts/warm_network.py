"""Small, public, bounded probes. Store location and speed, never URLs or IPs."""
import time
import urllib.request
from urllib.parse import urlparse


def public_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.netloc or p.username or p.password or p.query or p.fragment:
        raise ValueError('Public HTTPS URLs without credentials/query strings required')
    return url


def speed(url):
    start = time.monotonic()
    try:
        req = urllib.request.Request(public_url(url), headers={'Range':'bytes=0-8388607'})
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read(8*1024*1024)
        elapsed = max(.001,time.monotonic()-start)
        return {'ok':True, 'bytes':len(body), 'seconds':elapsed, 'MBps':len(body)/elapsed/1e6,
                'note':'bounded 8 MiB sample, not sustained shard throughput'}
    except Exception as exc:
        return {'ok':False, 'error':type(exc).__name__}


def probe(manifest, mirror=None, test_object='speed-test.bin'):
    result = {}
    try:
        with urllib.request.urlopen('https://cloudflare.com/cdn-cgi/trace',timeout=10) as response:
            fields = dict(line.split('=',1) for line in response.read(16384).decode().splitlines() if '=' in line)
        result['location'] = {k:fields.get(k) for k in ('colo','loc')}
    except Exception as exc:
        result['location'] = {'error':type(exc).__name__}
    m = manifest['model']
    shard = next(p for p in manifest['files'] if p.endswith('.safetensors'))
    result['hf'] = speed(f"https://huggingface.co/{m['repo']}/resolve/{m['revision']}/{shard}")
    if mirror:
        if '..' in test_object.split('/') or test_object.startswith('/'):
            raise ValueError('Mirror test object must be a relative path')
        result['mirror'] = speed(public_url(mirror.rstrip('/'))+'/'+test_object)
    return result
