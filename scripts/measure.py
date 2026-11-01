"""Noon measurements against an already-running local engine; never restart it.
Run once per config, then --compare those JSONs to validate one-factor changes.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import time

import aiohttp
import bench
from warm_download import load_manifest
from warm_logic import parse_pool, pool_table, recommend_running
from warm_network import probe
from serve_plan import add_arguments, argument_env, print_plan, variant, image_reference

ROOT = Path(__file__).resolve().parents[1]


def compare(reports):
    if len(reports) < 2: raise ValueError('At least two reports required')
    base = reports[0]
    rows = []
    for report in reports[1:]:
        factors = ('layout', 'engram', 'dspark') if 'layout' in base['config'] else ('attn_mode','spec_decode')
        changed = [k for k in factors if base['config'][k] != report['config'][k]]
        if len(changed) != 1: raise ValueError('Compare exactly one changed config at a time')
        if base.get('image_id') != report.get('image_id'): raise ValueError('Image changed between comparisons')
        if base['model'] != report['model'] or base['workload'] != report['workload']:
            raise ValueError('Model/revision and workload must match')
        # Remaining actual engine settings must be identical; labels alone are insufficient.
        left,right=dict(base['engine_config']),dict(report['engine_config'])
        keys = {'attn_mode': ('PARALLELISM',), 'spec_decode': ('SPECULATION',),
                'layout': ('LAYOUT', 'PARALLELISM'), 'engram': ('ENGRAM',),
                'dspark': ('DSPARK', 'SPECULATION')}[changed[0]]
        for key in keys: left.pop(key,None);right.pop(key,None)
        if left != right: raise ValueError('Additional engine settings changed')
        rows.append({'factor':changed[0], 'baseline':base['config'], 'candidate':report['config'],
                     'baseline_results':base['results'], 'candidate_results':report['results']})
    return rows


def variant_metadata(warm, requested):
    if 'launch_plan' not in warm or 'container_started_at' not in warm:
        raise ValueError('Warm report predates A/B plans; warm again with this kit')
    actual = variant(warm['config'])
    if actual != warm['variant'] or actual != warm['launch_plan']['variant']:
        raise ValueError('Warm variant metadata disagrees with launch plan')
    if 'ATTN_MODE' in requested and 'LAYOUT' not in requested:
        requested = dict(requested, LAYOUT=variant(requested)['layout'])
    for key in ('LAYOUT', 'ENGRAM', 'DSPARK'):
        if key in requested and requested[key] != actual[key.lower()]:
            raise ValueError(key+' does not match the running warm variant')
    if any(k in requested for k in ('ENGINE_IMAGE', 'BAKED_IMAGE', 'IMAGE_DIGEST')):
        wanted = image_reference(requested)
        if any(k in requested for k in ('ENGINE_IMAGE', 'BAKED_IMAGE')):
            matches = wanted['image_ref'] == warm['image_ref']
        else:
            matches = wanted['image_digest'] == warm['image_digest']
        if not matches: raise ValueError('Requested image does not match warm report')
    if not warm['image_pinned']:
        print('WARNING: measurement image is unpinned: '+warm['image_ref'], file=sys.stderr)
    return {'config': actual, 'launch_plan': warm['launch_plan'],
            **{k: warm[k] for k in ('image_ref', 'image_digest', 'image_tag', 'image_pinned')}}


async def sweep():
    results=[]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180),
        headers={'Authorization':'Bearer '+os.environ['INFER_UPSTREAM_KEY']},
        connector=aiohttp.TCPConnector(limit=64), trust_env=False) as session:
        # One fixed operator-owned synthetic prompt measures prefix reuse explicitly.
        await bench.request(session,'cache-prime')
        for n in (1,8,16,32,64):
            start=time.perf_counter()
            streams=await asyncio.gather(*(bench.request(session,i) for i in range(n)))
            elapsed=time.perf_counter()-start
            ok=[r for r in streams if r['ok']]
            cached=[r.get('cached_tokens') for r in ok]
            inputs=sum(r['input_tokens'] for r in ok)
            results.append({'concurrency':n,'errors':len(streams)-len(ok),'seconds':elapsed,
                'ttft_p50_ms':bench.percentile([r['ttft_ms'] for r in ok],.5),
                'ttft_p95_ms':bench.percentile([r['ttft_ms'] for r in ok],.95),
                'decode_tokens_s_per_stream_p50':bench.percentile([r['decode_tokens_s'] for r in ok],.5),
                'aggregate_input_tokens_s':inputs/elapsed,
                'aggregate_output_tokens_s':sum(r['output_tokens'] for r in ok)/elapsed,
                'cached_tokens_hit_rate':sum(cached)/inputs if inputs and all(x is not None for x in cached) else None,
                'streams':streams})
    return results


def markdown(report):
    advice = report['recommendation']
    recommended = advice['max_running_requests']
    recommendation = ('unknown (pool not logged)' if recommended is None else
                      'no full context fits; reduce context/memory use before launch' if recommended == 0 else
                      str(recommended))
    lines=['# Noon measurement','', 'Measurements apply only to this model, image and synthetic workload.', '',
           'Variant: `'+json.dumps(report['config'], sort_keys=True)+'`; image: `'+report['image_ref']+'`.', '',
           f"Recommended MAX_RUNNING_REQUESTS: **{recommendation}** at {advice['context_tokens']} tokens per stream.",
           advice['basis'], '',
           '| Streams | Average context | Tokens required | Pool with 10% headroom | Fits |',
           '|---:|---:|---:|---:|:---:|']
    for r in report['pool_table']:
        lines.append(f"| {r['streams']} | {r['avg_context']} | {r['tokens']} | {r['usable_pool_tokens']} | {r['fits'] if r['fits'] is not None else 'unknown'} |")
    lines += ['', 'KV bytes/token: '+str(report['pool']['kv_bytes_per_token']),report['pool']['scope'],'',
              '| Concurrency | TTFT p50/p95 ms | Decode tok/s per stream p50 | Aggregate in/out tok/s | Cached token rate | Errors |',
              '|---:|---:|---:|---:|---:|---:|']
    for r in report['results']:
        lines.append(f"| {r['concurrency']} | {r['ttft_p50_ms']} / {r['ttft_p95_ms']} | {r['decode_tokens_s_per_stream_p50']} | {r['aggregate_input_tokens_s']:.2f} / {r['aggregate_output_tokens_s']:.2f} | {r['cached_tokens_hit_rate']} | {r['errors']} |")
    lines += ['', 'Missing pool/cache metrics are unknown, never zero. Sparse-cache bytes/token is workload-dependent.',
              'Pool fit is a conservative token-budget screen, not an SLA or a DP load-balancing guarantee.',
              'MTP comparison is unavailable: no verified V4.1 MTP configuration. No silent substitution with DSpark.',
              '', 'Network: `'+json.dumps(report['network'])+'`']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--warm-report',type=Path,help='Ready warm timing JSON; supplies actual selected configuration')
    parser.add_argument('--log',type=Path,help='Redacted SGLang startup log')
    parser.add_argument('--output',type=Path,default=ROOT/'runs'/('measure-'+time.strftime('%Y%m%dT%H%M%S')))
    parser.add_argument('--compare',nargs='+',type=Path)
    add_arguments(parser)
    args=parser.parse_args(argv)
    requested = argument_env(args, os.environ)
    if args.print_plan:
        print_plan(requested, args.available_ram_gb)
        return
    if args.available_ram_gb is not None: parser.error('--available-ram-gb is dry-run only')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.compare:
        rows=compare([json.loads(p.read_text()) for p in args.compare])
        args.output.with_suffix('.json').write_text(json.dumps(rows,indent=2)+'\n')
        args.output.with_suffix('.md').write_text('# One-factor comparisons\n\n'+ '\n'.join(
            f"- {r['factor']}: {r['baseline']} → {r['candidate']}; paired metrics in JSON." for r in rows)+'\n')
        return
    if not args.warm_report or not args.log: parser.error('--warm-report and --log required')
    warm=json.loads(args.warm_report.read_text())
    if warm['state'] != 'ready': raise ValueError('Warm report is not ready')
    metadata = variant_metadata(warm, requested)
    manifest=load_manifest()
    if warm['model'] != manifest['model']: raise ValueError('Wrong model/revision')
    e=os.environ
    e['MODEL']=manifest['model']['repo'];e['ENDPOINT']=e.get('ENDPOINT','http://127.0.0.1:8000')
    if e['ENDPOINT'] != 'http://127.0.0.1:8000':
        raise ValueError('Measure requires the warm front door at http://127.0.0.1:8000')
    if not e.get('INFER_UPSTREAM_KEY'): raise ValueError('Existing exported upstream key required')
    # Tie measurements to the container that produced the warm report.
    state=subprocess.check_output(['docker','inspect',warm['container'],'--format','{{.Image}} {{.State.Running}} {{.State.StartedAt}}'],
                                  stderr=subprocess.DEVNULL,text=True,timeout=10).strip()
    if state != warm['image_id']+' true '+warm['container_started_at']: raise ValueError('Warm container image/running state/start time changed; warm again before measuring')
    config=warm['config']
    e['BENCH_FIXED_PROMPT']='1'
    pool=parse_pool(args.log.read_text())
    report={'model':manifest['model'],'image_id':warm['image_id'],'engine_config':config,
            **metadata,
            'workload':{'prompt_repeats':int(e.get('PROMPT_REPEATS','64')),'output_tokens':int(e.get('OUTPUT_TOKENS','256')),'fixed_operator_prompt':True},
            'pool':pool,'pool_table':pool_table(pool),'recommendation':recommend_running(pool,int(config['CONTEXT_LENGTH'])),
            'network':probe(manifest,e.get('MIRROR_URL'),e.get('MIRROR_TEST_OBJECT','speed-test.bin'),e.get('MIRROR_AUTH_HEADER')),
            'results':asyncio.run(sweep())}
    args.output.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n')
    summary=markdown(report);args.output.with_suffix('.md').write_text(summary);print(summary)


if __name__ == '__main__':
    main()
