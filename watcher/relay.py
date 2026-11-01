"""Call the external Netops relay.sh interface; never implement its tunnels here."""
import json
import os
import re
import shlex
import subprocess
import sys
import time


def call(args, env=None, run=subprocess.run):
    env = os.environ if env is None else env
    if not args or args[0] not in ('up', 'down', 'push-weights'):
        raise ValueError('Unknown relay operation')
    limits = {'up': (3, 4), 'down': (2, 2), 'push-weights': (4, 5)}
    low, high = limits[args[0]]
    if not low <= len(args) <= high:
        raise ValueError('Wrong relay.sh argument count')
    argv = [env['RELAY_SH'], *args]
    if env.get('DRY_RUN') == '1':
        print('DRY RUN: external relay.sh '+args[0]+' interface (not executed)')
        return
    timeout = 120 if args[0] == 'up' else 30
    if args[0] == 'push-weights':
        timeout = max(1, int(env.get('WINDOW_END', str(int(time.time())+14400)))-660-time.time())
    if env.get('RELAY_HOST'):
        host = env['RELAY_HOST']
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
            raise ValueError('Invalid RELAY_HOST')
        remote = ('import json,os,subprocess,sys; p=json.load(sys.stdin); '
                  'os.environ.update(p["env"]); '
                  'sys.exit(subprocess.call(p["argv"]))')
        # Runtime secrets cross SSH only through stdin, never command text/files.
        payload = {'argv': argv, 'env': {k: env[k] for k in ('CF_TUNNEL_TOKEN', 'RELAY_STATE') if env.get(k)}}
        ssh = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
        if env.get('RELAY_SSH_KEY_PATH'):
            ssh += ['-i', env['RELAY_SSH_KEY_PATH']]
        command = ssh+[host, shlex.join(['python3', '-c', remote])]
        result = run(command, input=json.dumps(payload), text=True, capture_output=True, timeout=timeout)
    else:
        result = run(argv, env=dict(env), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # External helper output may contain credentials; never echo it.
        raise RuntimeError('Netops relay.sh '+args[0]+' failed; inspect relay-side state safely')
    print('Netops relay.sh '+args[0]+' completed')


if __name__ == '__main__':
    call(sys.argv[1:])
