"""Small, pure launch plans for the two loopback guard engines."""
import math
from pathlib import Path
import re

from safety import SafetyConfig
from serve_plan import require_pinned


def plans(env, image, run_id):
    require_pinned(image)
    config = SafetyConfig.from_env(env)
    if not config.enabled or config.output_guard != 'qwen3guard':
        raise ValueError('node-run requires SAFETY=1 and both guards')
    if config.url != 'http://127.0.0.1:8600' or config.qwen_url != 'http://127.0.0.1:8601':
        raise ValueError('node-run guard URLs must be loopback ports 8600 and 8601')
    gpu = int(env.get('GUARD_GPU', '7'))
    if gpu < 0:
        raise ValueError('GUARD_GPU must be a GPU index')
    cache = Path(env.get('GUARD_WEIGHTS_DIR', str(Path(env['WEIGHTS_DIR']).parent/'guards'))).absolute()
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
        args = ['docker', 'run', '-d', '--name', name, '--gpus', f'device={gpu}',
                '--network', 'host', '--ipc', 'host', '--log-driver', 'none',
                '--mount', f'type=bind,src={cache},dst=/guard-cache',
                '--env', 'HF_HOME=/guard-cache', '--entrypoint', 'python3', image['image_ref'],
                '-m', 'sglang.launch_server', '--model-path', model, '--revision', revision,
                '--host', '127.0.0.1', '--port', str(port), '--dtype', 'bfloat16',
                '--mem-fraction-static', str(fraction), '--context-length', '8192',
                '--max-running-requests', '128', '--download-dir', '/guard-cache']
        result.append({'name': name, 'model': model, 'revision': revision, 'port': port,
                       'gpu': gpu, 'mem_fraction_static': fraction, 'cache_dir': str(cache), 'argv': args})
    return result
