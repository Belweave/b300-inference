"""Pure decisions shared by warm-up, measurement and offline tests."""
import math
import re


def tunnel_slot(env):
    slot = env.get('TUNNEL_SLOT')
    if slot not in ('a', 'b'):
        raise ValueError("TUNNEL_SLOT is required and must be exactly 'a' or 'b'")
    return slot


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
    # The scheduler's capacity line carries the effective running limit. ServerArgs dumps
    # repeat the *requested* value, so only lines that also report the pool count.
    running = [int(m) for line in text.splitlines() if 'max_total_num_tokens' in line
               for m in re.findall(r'max_running_requests\s*[=:]\s*(\d+)', line)]
    bpt = re.findall(r'(?:KV\s+bytes[/_ ]per[_ ]token|KV\s+bytes/token|kv_bytes_per_token)\s*[=:]\s*([\d.]+)', text, re.I)
    # Several DP ranks may log their own pool. Never sum them without a topology proof.
    return {'max_total_num_tokens': min(int(x.replace(',', '')) for x in tokens) if tokens else None,
            'kv_bytes_per_token': max(map(float, bpt)) if bpt else None,
            'engine_max_running_requests': min(running) if running else None,
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


# ---- 128-vs-64 concurrency decision (the only place it is made) ------------------
# Orchestrators: warm.py Warm.launch_with_fit_fallback and bootstrap.sh
# launch_with_fit_fallback (via scripts/fit.py). Both call only these two functions.
GRAPH_SIZES = (1, 2, 4, 8, 16, 32, 64, 96, 128)
FALLBACK_RUNNING = 64
FIT_HEADROOM = 0.9


def graph_sizes(maximum, explicit=None):
    """CUDA graph decode batch sizes: explicit list, else the standard ladder up to maximum."""
    graph = [int(x) for x in explicit.split(',')] if explicit else [n for n in GRAPH_SIZES if n <= maximum]
    if not graph or sorted(set(graph)) != graph or graph[0] < 1 or graph[-1] > maximum:
        raise ValueError('Graph sizes must be increasing, unique and within max running')
    return graph


def concurrency_fit(pool, requested, context, tokens_per_stream, started=True):
    """Did the engine start with `requested` running requests and report a KV pool that fits?

    "Fit" means all of: the engine reached health with this setting (no OOM in KV
    allocation or CUDA-graph capture); it logged max_total_num_tokens; it did not clamp
    max_running_requests below the request; and 90% of the pool holds `requested`
    streams of `tokens_per_stream` resident tokens each. It does NOT mean `requested`
    full-context (262144-token) streams are resident at once; full_context_streams
    records how many of those the pool holds.
    """
    if requested < 1 or context < 1 or tokens_per_stream < 1:
        raise ValueError('Fit inputs must be positive')
    capacity = pool.get('max_total_num_tokens')
    usable = math.floor(capacity*FIT_HEADROOM) if capacity is not None else None
    engine_running = pool.get('engine_max_running_requests')
    reasons = []
    if not started:
        reasons.append(f'engine did not reach health with max_running_requests={requested}')
    elif capacity is None:
        reasons.append('engine log reported no KV pool size (max_total_num_tokens)')
    else:
        if usable < requested*tokens_per_stream:
            reasons.append(f'KV pool {capacity} tokens (90% = {usable}) < {requested} x {tokens_per_stream} tokens')
        if engine_running is not None and engine_running < requested:
            reasons.append(f'engine clamped max_running_requests to {engine_running}')
    return {'requested': requested, 'passed': not reasons, 'reason': '; '.join(reasons) or None,
            'max_total_num_tokens': capacity, 'usable_pool_tokens': usable,
            'engine_max_running_requests': engine_running, 'tokens_per_stream': tokens_per_stream,
            'required_tokens': requested*tokens_per_stream, 'context_tokens': context,
            'full_context_streams': usable//context if usable is not None else None,
            'definition': 'engine healthy at requested; pool logged; not clamped; '
                          '0.9*max_total_num_tokens >= requested*FIT_TOKENS_PER_STREAM'}


def fallback_settings(selected, fallback=FALLBACK_RUNNING):
    """Engine settings for the fallback launch: running limit and graph ladder back to 64."""
    current = int(selected['MAX_RUNNING_REQUESTS'])
    if current <= fallback:
        return None
    result = dict(selected, MAX_RUNNING_REQUESTS=str(fallback), CUDA_GRAPH_MAX_BS=str(fallback))
    if selected.get('WARM_GRAPH_BS'):
        result['WARM_GRAPH_BS'] = ','.join(str(n) for n in graph_sizes(current, selected['WARM_GRAPH_BS'])
                                           if n <= fallback)
    return result
