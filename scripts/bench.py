"""Closed-loop SSE sweep. Requires aiohttp. Token counts come from usage, not chunks."""
import asyncio
import json
import math
import os
from pathlib import Path
import time
import uuid

import aiohttp


def percentile(values, p):
    values = sorted(values)
    return values[max(0, math.ceil(len(values)*p)-1)] if values else None


async def request(session, sequence):
    start = time.perf_counter()
    first = last = None
    chunks = 0
    usage = None
    done = False
    payload = {
        'model': os.environ['MODEL'], 'stream': True, 'stream_options': {'include_usage': True},
        'max_tokens': int(os.environ.get('OUTPUT_TOKENS', '256')), 'temperature': 0,
        'messages': [{'role': 'user', 'content': ('Operator-owned fixed prefix. ' if os.environ.get('BENCH_FIXED_PROMPT') == '1' else f'Unique case {uuid.uuid4()} {sequence}. ')+
                      ' '.join(['Describe the tradeoffs of batching requests in a server.'] * int(os.environ.get('PROMPT_REPEATS', '64')))}],
    }
    try:
        async with session.post(os.environ['ENDPOINT']+'/v1/chat/completions', json=payload) as resp:
            if resp.status != 200:
                raise ValueError(f'HTTP {resp.status}')
            # aiohttp line iteration handles arbitrarily fragmented TCP chunks.
            async for raw in resp.content:
                line = raw.decode().strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    done = True
                    break
                event = json.loads(data)
                if event.get('usage'):
                    usage = event['usage']
                for choice in event.get('choices', []):
                    delta = choice.get('delta', {})
                    if delta.get('content') or delta.get('reasoning_content') or delta.get('reasoning'):
                        stamp = time.perf_counter()
                        first = stamp if first is None else first
                        last = stamp
                        chunks += 1
        if not done or first is None or not usage or usage.get('completion_tokens', 0) < 2:
            raise ValueError('Missing DONE/text/usage or fewer than two output tokens')
        n = usage['completion_tokens']
        if last <= first:
            raise ValueError('All tokens buffered in one event; TPOT is unmeasurable')
        return {'ok': True, 'ttft_ms': (first-start)*1000,
                'tpot_ms': (last-first)*1000/(n-1), 'output_tokens': n,
                'input_tokens': usage.get('prompt_tokens', 0),
                'cached_tokens': (usage.get('prompt_tokens_details') or {}).get('cached_tokens'),
                'decode_tokens_s': (n-1)/(last-first), 'end_to_end_tokens_s': n/(last-start),
                'sse_content_events': chunks}
    except Exception as exc:
        # Never store response bodies, prompts, URLs containing credentials, or auth headers.
        return {'ok': False, 'error': type(exc).__name__}


async def main():
    e = os.environ
    output = Path(e['RUN_DIR'])
    ttft_target = float(e.get('TARGET_TTFT_MS', '2000'))
    tpot_target = float(e.get('TARGET_TPOT_MS', '50'))
    levels = [int(x) for x in e.get('CONCURRENCY', '16,32,64,128,256,512,1024').split(',')]
    assert all(0 < c <= 4096 for c in levels)
    timeout = aiohttp.ClientTimeout(total=float(e.get('REQUEST_TIMEOUT_SECONDS', '180')))
    results = []
    async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=0),
                                    headers={'Authorization': 'Bearer '+e['INFER_UPSTREAM_KEY']}) as session:
        await request(session, 'warmup')
        for concurrency in levels:
            if (output/'stop').exists() or time.time() > int(e.get('WINDOW_END', '9999999999'))-660:
                break
            records = []
            started = time.perf_counter()
            async def worker(i):
                for j in range(int(e.get('REQUESTS_PER_STREAM', '2'))):
                    records.append(await request(session, f'{i}-{j}'))
            await asyncio.gather(*(worker(i) for i in range(concurrency)))
            elapsed = time.perf_counter()-started
            ok = [x for x in records if x['ok']]
            ttft95 = percentile([x['ttft_ms'] for x in ok], .95)
            tpot95 = percentile([x['tpot_ms'] for x in ok], .95)
            row = {'concurrency': concurrency, 'requests': len(records), 'errors': len(records)-len(ok),
                   'elapsed_s': elapsed, 'ttft_p50_ms': percentile([x['ttft_ms'] for x in ok], .5),
                   'ttft_p95_ms': ttft95, 'tpot_p95_ms': tpot95,
                   'stream_tokens_s_p50': percentile([x['decode_tokens_s'] for x in ok], .5),
                   'aggregate_output_tokens_s': sum(x['output_tokens'] for x in ok)/elapsed,
                   'aggregate_input_tokens_s': sum(x['input_tokens'] for x in ok)/elapsed,
                   'passes': bool(ok) and len(ok)==len(records) and ttft95<=ttft_target and tpot95<=tpot_target,
                   'streams': records}
            results.append(row)
            report = {'model': e['MODEL'], 'engine': e['ENGINE'], 'target_ttft_ms': ttft_target,
                      'target_tpot_ms': tpot_target, 'results': results,
                      'max_passing_concurrency': max((r['concurrency'] for r in results if r['passes']), default=None)}
            tmp = output/'loadtest.json.tmp'
            tmp.write_text(json.dumps(report, indent=2)+'\n'); tmp.replace(output/'loadtest.json')
            lines = ['# Load test', '', f"Maximum passing tested concurrency: {report['max_passing_concurrency']}", '',
                     '| Streams | TTFT p50 / p95 ms | TPOT p95 ms | Stream tok/s p50 | Aggregate output tok/s | Errors | Pass |',
                     '|---:|---:|---:|---:|---:|---:|:---:|']
            def fmt(x): return '-' if x is None else f'{x:.1f}'
            for r in results:
                lines.append(f"| {r['concurrency']} | {fmt(r['ttft_p50_ms'])} / {fmt(r['ttft_p95_ms'])} | {fmt(r['tpot_p95_ms'])} | {fmt(r['stream_tokens_s_p50'])} | {fmt(r['aggregate_output_tokens_s'])} | {r['errors']} | {r['passes']} |")
            lines += ['', 'TPOT is per-request mean decode latency, then p95 across requests; SSE chunks are not tokens.',
                      'This closed-loop synthetic sweep is a sampled capacity bound, not a production SLA or maximum proof.',
                      'Unique prompt prefixes avoid measuring only a shared-prefix cache hit. Reasoning tokens count if usage includes them.']
            (output/'loadtest.md').write_text('\n'.join(lines)+'\n')
            print(f"Concurrency {concurrency}: pass={row['passes']}; errors={row['errors']}", flush=True)

if __name__ == '__main__':
    asyncio.run(main())
