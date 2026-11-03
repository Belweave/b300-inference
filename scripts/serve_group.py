"""Run one serve plan in a container; keys enter only through stdin.

Once the whole plan has answered /health, a crash restarts it in place, up to
MAX_RESTARTS times. A crash before that still stops the container, so warm-up
fails fast on a bad image or flag instead of looping.
"""
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

from serve_plan import host_gate, require_host_ram

MAX_RESTARTS = 3
RESTART_PAUSE = 15  # seconds for the driver to hand GPU memory back


def child_command(process, key, admin_key=None):
    # SGLang lets --api-key unlock its admin routes (weight reloads, weights_checker,
    # profiling) unless --admin-api-key is set. Nobody is given this one.
    admin = ['--admin-api-key', admin_key] if admin_key and process['role'] == 'engine' else []
    return [*process['argv_without_key'], *admin, '--api-key', key]


def healthy(process):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{process['port']}/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def stop_all(children):
    for p in children:
        if p.poll() is None: p.terminate()
    for p in children:
        try: p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()


def serve_once(payload, env, key, admin_key):
    """Run the plan until a process exits; return True if it was ever fully healthy."""
    processes = payload['plan']['processes']
    children = []
    served = False
    try:
        for process in processes:
            children.append(subprocess.Popen(child_command(process, key, admin_key), env={**env, **process['env']}))
        while all(p.poll() is None for p in children):
            if not served and all(healthy(p) for p in processes):
                served = True
                print('serve_group: plan healthy', flush=True)
            time.sleep(1)
        codes = [p.poll() for p in children]
        print(f'serve_group: a serve process exited, exit codes {codes}', file=sys.stderr, flush=True)
        return served
    finally:
        stop_all(children)


def run(payload):
    selected = payload['env']
    if selected['ENGRAM'] == 'host':
        match = re.search(r'^MemAvailable:\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M)
        if not match: raise ValueError('MemAvailable is unavailable')
        require_host_ram(selected, host_gate(selected, int(match[1])*1024))
    env = {**os.environ, **selected, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DO_NOT_TRACK': '1'}
    env.pop('SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE', None)
    key = env.pop('INFER_UPSTREAM_KEY')
    admin_key = secrets.token_urlsafe(32)

    def stop(signum, frame):
        raise SystemExit(128+signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for attempt in range(MAX_RESTARTS+1):
        if not serve_once(payload, env, key, admin_key):
            break
        if attempt == MAX_RESTARTS:
            print(f'serve_group: crashed {attempt+1} times; giving up', file=sys.stderr, flush=True)
            break
        print(f'serve_group: restarting the plan ({attempt+1}/{MAX_RESTARTS})', file=sys.stderr, flush=True)
        time.sleep(RESTART_PAUSE)
    raise RuntimeError('A serve process exited; stopping the complete plan')


if __name__ == '__main__':
    run(json.loads(sys.stdin.readline()))
