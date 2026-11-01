"""Run one serve plan in a container; keys enter only through stdin."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from serve_plan import host_gate, require_host_ram


def child_command(process, key):
    return [*process['argv_without_key'], '--api-key', key]


def run(payload):
    selected = payload['env']
    if selected['ENGRAM'] == 'host':
        match = re.search(r'^MemAvailable:\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M)
        if not match: raise ValueError('MemAvailable is unavailable')
        require_host_ram(selected, host_gate(selected, int(match[1])*1024))
    env = {**os.environ, **selected, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'DO_NOT_TRACK': '1'}
    env.pop('SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE', None)
    key = env.pop('INFER_UPSTREAM_KEY')
    children = []

    def stop(signum, frame):
        raise SystemExit(128+signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for process in payload['plan']['processes']:
            children.append(subprocess.Popen(child_command(process, key), env={**env, **process['env']}))
        while all(p.poll() is None for p in children):
            time.sleep(1)
        raise RuntimeError('A serve process exited; stopping the complete plan')
    finally:
        for p in children:
            if p.poll() is None: p.terminate()
        for p in children:
            try: p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill(); p.wait()


if __name__ == '__main__':
    run(json.loads(sys.stdin.readline()))
