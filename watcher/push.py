"""Push prestaged weights and image. No downloads or provider operations."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from weights import manifest
from relay import call as relay_call

ROOT = Path(__file__).resolve().parents[1]


def ssh(env):
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-p', env.get('NODE_SSH_PORT', '22'), '-i', env['NODE_SSH_KEY_PATH']]


def push(target, env=None, run=subprocess.run):
    env = os.environ if env is None else env
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*', target):
        raise ValueError('Invalid node target')
    source = Path(env['STAGING_DIR'])/env.get('MODEL_VARIANT', 'fp8')
    destination = env['WEIGHTS_DIR']
    incoming = env['REMOTE_PUSH_DIR']
    for path in (destination, incoming):
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in Path(path).parts:
            raise ValueError('Remote staging paths must contain only safe path characters')
    if env.get('DRY_RUN') == '1':
        print('DRY RUN: push staged weights using '+('relay.sh push-weights' if env.get('RELAY_SH') else 'parallel rsync --partial'))
        print('DRY RUN: push engine.tar.zst + engine.json; node verifies hashes and loaded image ID')
        return
    for name in ('engine.tar.zst', 'engine.json'):
        if not (source/'images'/name).is_file():
            raise ValueError('Image is not prestaged: '+name)
    data = manifest(env)
    transfer_weights = env.get('WEIGHTS_SOURCE') == 'push'
    if transfer_weights and not (env.get('RELAY_SH') and env.get('RELAY_HOST')):
        if not all((source/'weights'/f['path']).is_file() for f in data['files']):
            raise ValueError('Weights are not fully prestaged')
    original_run = run
    def bounded_run(argv, **kwargs):
        remaining = int(env.get('WINDOW_END', str(int(time.time())+14400)))-660-time.time()
        if remaining <= 0:
            raise RuntimeError('Transfer deadline reached; fewer than eleven minutes remain')
        kwargs['timeout'] = min(kwargs.get('timeout', remaining), remaining)
        return original_run(argv, **kwargs)
    run = bounded_run
    run(ssh(env)+[target, 'mkdir -p '+shlex.quote(destination)+' '+shlex.quote(incoming)], check=True)
    if transfer_weights and env.get('RELAY_SH'):
        if env.get('NODE_SSH_PORT', '22') != '22':
            raise ValueError('Netops relay.sh cannot pass a non-22 node SSH port')
        if env.get('RELAY_HOST'):
            relay_source = str(Path(env['RELAY_STAGING_DIR'])/env.get('MODEL_VARIANT', 'fp8')/'weights')
            relay_key = env['RELAY_NODE_SSH_KEY_PATH']
        else:
            relay_source = str(source/'weights')
            relay_key = env['NODE_SSH_KEY_PATH']
        relay_call(['push-weights', target, relay_source, destination, relay_key], env, run)
    elif transfer_weights:
        workers = int(env.get('PUSH_WORKERS', '4'))
        if not 1 <= workers <= 16:
            raise ValueError('PUSH_WORKERS must be 1..16')
        # File lists go through stdin, not temporary files; each path belongs to one worker.
        batches = [data['files'][i::workers] for i in range(workers)]
        def copy(files):
            if not files:
                return
            run(['rsync', '-a', '--partial', '--from0', '--files-from=-',
                 '-e', shlex.join(ssh(env)), str(source/'weights')+'/', target+':'+destination+'/'],
                input=b'\0'.join(f['path'].encode() for f in files)+b'\0', check=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(copy, batches))
    # Images always originate on the Mac staging side. Remote relay weight staging is independent.
    run(['rsync', '-a', '--partial', '-e', shlex.join(ssh(env)),
         str(source/'images'/'engine.tar.zst'), str(source/'images'/'engine.json'), target+':'+incoming+'/'], check=True)


if __name__ == '__main__':
    push(sys.argv[1])
