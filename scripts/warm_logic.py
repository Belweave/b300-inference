"""Pure decisions shared by warm-up, measurement and offline tests."""
import math
import re


def shard_plan(manifest):
    changed = set(manifest['differs_from_base'])
    shards = {p: f for p, f in manifest['files'].items() if p.endswith('.safetensors')}
    return {'reuse': [p for p in shards if p not in changed],
            'download': [p for p in shards if p in changed],
            'changed_bytes': sum(f['size'] for p, f in shards.items() if p in changed)}


def verify_mirror(manifest, mirror):
    if mirror.get('revision') != manifest['model']['revision']:
        raise ValueError('Mirror revision disagreement')
    shards = {p for p in manifest['files'] if p.endswith('.safetensors')}
    files = mirror.get('files', {})
    if isinstance(files, list):
        entries = {f['filename']: f for f in files}
        if len(entries) != len(files): raise ValueError('Duplicate mirror files')
        files = entries
    # Accept the original shard-only mirror contract or Kai's full 52-file
    # manifest. Every supplied file must match; small files may stay HF-only.
    if not shards <= set(files) <= set(manifest['files']):
        raise ValueError('Mirror shard list disagreement')
    for path, other in files.items():
        f = manifest['files'][path]
        if other.get('sha256') != f['sha256'] or other.get('size') != f['size']:
            raise ValueError('Mirror checksum/size disagreement')
    return set(files)


def fallback(error=False, valid=True, mbps=None, floor=0):
    return error or not valid or (mbps is not None and mbps < floor)


def ram_gate(available, table=203_000_000_000, headroom=32_000_000_000):
    if min(available, table, headroom) < 0:
        raise ValueError('RAM quantities must be nonnegative')
    return {'enabled': available >= table + headroom, 'available_bytes': available,
            'required_bytes': table + headroom,
            'reason': 'enough MemAvailable' if available >= table+headroom else 'insufficient MemAvailable; host table disabled'}


def deadline(window_end):
    return float(window_end)-13*60


def jit_start(window_end, measured_warm):
    # Warm completion must precede the gateway deadline, with five extra minutes.
    return deadline(window_end)-max(0, measured_warm)-5*60


def drain_until(window_end, now, cap=20*60):
    return max(now, min(now+max(0, cap), float(window_end)-3*60))


def parse_pool(text):
    tokens = re.findall(r'max_total_num_tokens\s*[=:]\s*([\d,]+)', text)
    bpt = re.findall(r'(?:KV\s+bytes[/_ ]per[_ ]token|KV\s+bytes/token|kv_bytes_per_token)\s*[=:]\s*([\d.]+)', text, re.I)
    # Several DP ranks may log their own pool. Never sum them without a topology proof.
    return {'max_total_num_tokens': min(int(x.replace(',', '')) for x in tokens) if tokens else None,
            'kv_bytes_per_token': max(map(float, bpt)) if bpt else None,
            'scope': 'minimum logged pool; conservative, not a verified aggregate across DP ranks'}


def pool_table(pool):
    capacity = pool.get('max_total_num_tokens')
    usable = math.floor(capacity*0.9) if capacity is not None else None
    return [{'streams': n, 'avg_context': ctx, 'tokens': n*ctx, 'usable_pool_tokens': usable,
             'fits': n*ctx <= usable if usable is not None else None,
             'estimated_kv_bytes': n*ctx*pool['kv_bytes_per_token'] if pool.get('kv_bytes_per_token') else None}
            for n in (16, 32, 48, 64) for ctx in (100_000, 250_000, 400_000)]


def recommend_running(pool, context):
    if context <= 0: raise ValueError('Context must be positive')
    capacity = pool.get('max_total_num_tokens')
    usable = math.floor(capacity*0.9) if capacity is not None else None
    return {'context_tokens': context, 'usable_pool_tokens': usable,
            'max_running_requests': usable//context if usable is not None else None,
            'basis': 'floor(0.9 * max_total_num_tokens / context_tokens); conservative pool bound, validate with sweep'}
