"""Pure launch plans; no Docker, network, credential or hardware access."""
import json
import re
import shlex
import sys

from engine import launch_args
from warm_logic import ram_gate

LAYOUTS = {'tp8': (8, 1), 'dep4x2': (4, 2), 'dep8x1': (8, 1), 'dep2x4': (2, 4)}
DEFAULT_IMAGE = 'lmsysorg/sglang:dev-dsv41'
# Tag's linux/amd64 manifest as of 2026-11-01; not yet qualified on B300.
DEFAULT_IMAGE_DIGEST = 'sha256:c1633485e2ef3a8562fc93b2e3b686abb31c4499300bf5ce13ee5d491b65c386'


RELAY_PORT = 8000  # tunnel ingress and every local client; fixed contract


def front_port(env):
    """Port of the engine (tp8) or router (DP layouts) behind the SSE relay on :8000."""
    if env.get('SSE_RELAY', '1') == '0':
        return RELAY_PORT
    if env.get('SSE_RELAY', '1') != '1':
        raise ValueError('SSE_RELAY must be 1 or 0')
    port = int(env.get('ENGINE_PORT') or 8001)
    if port == RELAY_PORT or not 1024 <= port <= 65535 or 8100 <= port <= 8499 or 29500 <= port <= 29899:
        raise ValueError('ENGINE_PORT must be a free loopback port other than 8000 and the worker/dist ports')
    return port


def variant(env):
    layout = env.get('LAYOUT', 'dep8x1' if env.get('ATTN_MODE') == 'dp_ep' else 'tp8')
    engram = env.get('ENGRAM', 'gpu')
    dspark = env.get('DSPARK', 'off')
    if layout not in LAYOUTS: raise ValueError('LAYOUT must be '+', '.join(LAYOUTS))
    if engram not in ('gpu', 'host'): raise ValueError('ENGRAM must be gpu or host')
    if dspark not in ('off', 'on'): raise ValueError('DSPARK must be off or on')
    return dict(layout=layout, engram=engram, dspark=dspark)


def image_reference(env):
    ref = env.get('BAKED_IMAGE') or env.get('ENGINE_IMAGE', DEFAULT_IMAGE)
    digest = env.get('IMAGE_DIGEST', '')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/:@-]*', ref):
        raise ValueError('Invalid image reference')
    if '@' in ref:
        ref, embedded = ref.split('@', 1)
        if digest and digest != embedded: raise ValueError('Conflicting image digests')
        digest = embedded
        if not digest: raise ValueError('Empty image digest')
    elif ref == DEFAULT_IMAGE and not digest:
        digest = DEFAULT_IMAGE_DIGEST
    if digest and not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('IMAGE_DIGEST must be sha256: followed by 64 lowercase hex digits')
    resolved = ref+'@'+digest if digest else ref
    return {'image_ref': resolved, 'image_digest': digest or None,
            'image_tag': ref, 'image_pinned': bool(digest)}


def require_pinned(image):
    if not image['image_pinned']:
        raise ValueError('Unpinned image '+image['image_ref']+'; set IMAGE_DIGEST or use image@sha256:')


def host_gate(env, available):
    replicas = LAYOUTS[variant(env)['layout']][1]
    table = max(203_000_000_000, int(float(env.get('ENGRAM_TABLE_GB', '203'))*1e9))
    headroom = max(32_000_000_000, int(float(env.get('ENGRAM_HEADROOM_GB', '32'))*1e9))
    gate = ram_gate(available, table=table*replicas, headroom=headroom)
    gate['replicas'] = replicas
    return gate


def require_host_ram(selected, gate):
    minimum = 203_000_000_000*LAYOUTS[variant(selected)['layout']][1]+32_000_000_000
    if selected.get('ENGRAM', 'gpu') == 'host' and (
            not gate or not gate['enabled'] or gate.get('available_bytes', 0) < minimum):
        raise ValueError(f'Host Engram requires MemAvailable >= {minimum} bytes (203 GB per replica + 32 GB headroom)')


def build_plan(selected, image, name, weights, kit, *, engram_flag=True):
    v = variant(selected)
    width, replicas = LAYOUTS[v['layout']]
    front = int(selected.get('ENGINE_PORT', RELAY_PORT))
    processes = []
    for i in range(replicas):
        port = front if v['layout'] == 'tp8' else 8100+i*100
        devices = ','.join(str(n) for n in range(i*width, (i+1)*width))
        args = launch_args(selected, width, port, engram_flag=engram_flag)
        if v['layout'] != 'tp8': args += ['--dist-init-addr', f'127.0.0.1:{29500+i*100}']
        processes.append({'role': 'engine', 'port': port, 'env': {'CUDA_VISIBLE_DEVICES': devices},
                          'argv_without_key': args})
    if v['layout'] != 'tp8':
        processes.append({'role': 'router', 'port': front, 'env': {}, 'argv_without_key': [
            'python3', '-m', 'sglang_router.launch_router', '--host', '127.0.0.1', '--port', str(front),
            '--worker-urls', *[f"http://127.0.0.1:{p['port']}" for p in processes],
            '--policy', 'cache_aware', '--dp-aware', '--prometheus-host', '127.0.0.1']})
    create = ['docker', 'create', '-i', '--name', name, '--gpus', 'all', '--network', 'host',
              '--ipc', 'host', '--cap-add', 'SYS_NICE', '--log-driver', 'none',
              '--mount', f'type=bind,src={weights},dst=/weights,readonly',
              '--mount', f'type=bind,src={kit},dst=/kit,readonly',
              '--entrypoint', 'python3', image, '/kit/scripts/serve_group.py']
    return {'schema': 1, 'variant': v, 'processes': processes, 'docker_create': create,
            'engram_config': 'flag' if engram_flag else 'image-default',
            'docker_start': ['docker', 'start', '-ai', name],
            'auth': 'Existing INFER_UPSTREAM_KEY supplied through stdin; --api-key appended inside container only'}


def add_arguments(parser):
    parser.add_argument('--layout', choices=LAYOUTS)
    parser.add_argument('--engram', choices=('gpu', 'host'))
    parser.add_argument('--dspark', choices=('off', 'on'))
    parser.add_argument('--image', dest='engine_image')
    parser.add_argument('--image-digest')
    parser.add_argument('--print-plan', '--dry-run', action='store_true')
    parser.add_argument('--available-ram-gb', type=float, help='Dry-run RAM assumption only; never used for a live launch')


def argument_env(args, env):
    result = dict(env)
    for key in ('layout', 'engram', 'dspark', 'engine_image', 'image_digest'):
        value = getattr(args, key)
        if value is not None: result[key.upper()] = value
    if args.engine_image is not None: result.pop('BAKED_IMAGE', None)
    return result


def print_plan(env, available_gb=None):
    from pathlib import Path
    from warm import settings
    from warm_download import load_manifest
    data = load_manifest()
    available = int(available_gb*1e9) if available_gb is not None else 0
    if available < 0: raise ValueError('RAM assumption cannot be negative')
    gate = host_gate(env, available)
    selected = settings(data, env, gate)
    image = image_reference(env)
    if not image['image_pinned'] and env.get('ALLOW_UNPINNED') == '1':
        print('WARNING: image is unpinned (dry run only): '+image['image_ref'], file=sys.stderr)
    else:
        require_pinned(image)
    plan = build_plan(selected, image['image_ref'], 'belweave-plan',
                      env.get('WEIGHTS_DIR', '/mnt/nvme/models/dsv41-uncensored'), Path(__file__).resolve().parents[1])
    plan.update(image, config=selected, model=data['model'], ram_assumption=gate)
    plan['shell_commands_without_key'] = [shlex.join(plan['docker_create']), shlex.join(plan['docker_start'])]
    plan['process_commands_without_key'] = [shlex.join(['env', *[k+'='+v for k,v in p['env'].items()],
        *p['argv_without_key']]) for p in plan['processes']]
    print(json.dumps(plan, indent=2))
