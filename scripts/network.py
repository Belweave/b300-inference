"""Bounded HTTPS HEAD probes; explicit SSH mode makes no outbound probe."""
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import subprocess


def probe(url):
    try:
        result = subprocess.run(['curl', '--head', '--location', '--silent', '--show-error',
                                 '--connect-timeout', '2', '--max-time', '5', '--max-redirs', '3',
                                 '--output', '/dev/null', '--write-out', '%{http_code}', url],
                                capture_output=True, text=True, timeout=6)
        code = int(result.stdout) if result.stdout.isdigit() else 0
        return {'ok': result.returncode == 0 and 200 <= code < 400, 'http_status': code}
    except (OSError, subprocess.TimeoutExpired):
        return {'ok': False, 'http_status': 0}


def select(env, probe_fn=probe):
    requested = env.get('NETWORK_MODE', 'auto')
    direction = env.get('SSH_DIRECTION', 'forward')
    if requested not in ('auto', 'cloudflared', 'ssh') or direction not in ('forward', 'reverse'):
        raise ValueError('Invalid NETWORK_MODE or SSH_DIRECTION')
    results = {}
    if requested == 'auto':
        arch = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'amd64'
        urls = {
            'cloudflare': 'https://api.cloudflare.com/client/v4/ips',
            'github_release': f'https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-{arch}',
            'huggingface': 'https://huggingface.co',
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(probe_fn, urls.values()))
        for (name, url), value in zip(urls.items(), values):
            results[name] = dict(value, url=url)
        selected = 'cloudflared' if all(x['ok'] for x in results.values()) else 'ssh'
    else:
        selected = requested
    return {'requested': requested, 'selected': selected, 'ssh_direction': direction,
            'probes': results, 'note': 'HTTPS success does not prove tunnel TCP/UDP 7844 access'}


def save(env):
    path = Path(env['RUN_DIR'])/'network.json'
    data = select(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(data, indent=2)+'\n'); tmp.replace(path)
    return data


if __name__ == '__main__':
    if os.environ.get('DRY_RUN') == '1':
        print('DRY RUN: HTTPS HEAD probes skipped; auto decides on the node (all pass -> cloudflared, else ssh)')
    else:
        print(json.dumps(save(os.environ)))
