"""Small, pure launch plans for the two loopback guard engines."""
import math
from pathlib import Path
import re

from safety import SafetyConfig
from serve_plan import require_pinned


def cache_dir(env):
    return Path(env.get('GUARD_WEIGHTS_DIR', str(Path(env['WEIGHTS_DIR']).parent/'guards'))).absolute()


def plans(env, image, run_id):
    require_pinned(image)
    config = SafetyConfig.from_env(env)
    if not config.enabled or config.output_guard != 'qwen3guard':
        raise ValueError('node-run requires SAFETY=1 and both guards')
    if config.url != 'http://127.0.0.1:8600' or config.qwen_url != 'http://127.0.0.1:8601':
        raise ValueError('node-run guard URLs must be loopback ports 8600 and 8601')
    gpu = int(env.get('GUARD_GPU', '0'))
    if gpu < 0:
        raise ValueError('GUARD_GPU must be a GPU index')
    cache = cache_dir(env)
    result = []
    for label, model, revision, port, fraction in (
        ('shieldstral', config.model, config.model_revision, 8600, env.get('GUARD_SHIELD_MEM_FRACTION', '0.06')),
        ('qwen', config.qwen_model, config.qwen_revision, 8601, env.get('GUARD_QWEN_MEM_FRACTION', '0.02')),
    ):
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError(label+' requires a pinned 40-character model commit revision')
        fraction = float(fraction)
        if not math.isfinite(fraction) or not 0 < fraction < 1:
            raise ValueError(label+' mem fraction must be between 0 and 1')
        name = run_id.lower()+'-'+label
        # guard_weights.stage fills this dir from the pinned manifest before launch.
        weights = cache/model/revision
        args = ['docker', 'run', '-d', '--name', name, '--gpus', f'device={gpu}',
                '--network', 'host', '--ipc', 'host', '--log-driver', 'json-file', '--log-opt', 'max-size=20m',
                '--mount', f'type=bind,src={weights},dst=/guard-model,readonly',
                '--env', 'HF_HUB_OFFLINE=1', '--entrypoint', 'python3', image['image_ref'],
                '-m', 'sglang.launch_server', '--model-path', '/guard-model', '--served-model-name', model,
                '--host', '127.0.0.1', '--port', str(port), '--dtype', 'bfloat16',
                '--mem-fraction-static', str(fraction), '--context-length', '8192',
                '--max-running-requests', '32',
                '--max-total-tokens', '8192', '--chunked-prefill-size', '2048',
                '--max-prefill-tokens', '2048', '--cuda-graph-max-bs-decode', '32', '--cuda-graph-bs-decode', '1', '2', '4', '8', '16', '32']
        result.append({'name': name, 'model': model, 'revision': revision, 'port': port,
                       'gpu': gpu, 'mem_fraction_static': fraction, 'cache_dir': str(cache), 'weights_dir': str(weights), 'argv': args})
    return result
