"""Call the external Netops relay.sh interface; never implement its tunnels here."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time


def call(args, env=None, run=subprocess.run):
    env = os.environ if env is None else env
    if not args or args[0] not in ('up', 'down', 'push-weights'):
        raise ValueError('Unknown relay operation')
    positional = {'up': 3, 'down': 2, 'push-weights': 4}[args[0]]
    if len(args) < positional or (len(args)-positional) % 2:
        raise ValueError('Wrong relay.sh argument count')
    allowed = {'--port', '--key', '--expires'} if args[0] == 'up' else {'--port', '--key'}
    if args[0] == 'down':
        allowed = set()
    seen = set()
    for flag, value in zip(args[positional::2], args[positional+1::2]):
        if flag not in allowed or flag in seen or not value:
            raise ValueError('Invalid relay.sh flag')
        seen.add(flag)
        if flag == '--port' and (not value.isdigit() or not 1 <= int(value) <= 65535):
            raise ValueError('Invalid node SSH port')
        if flag == '--expires' and not value.isdigit():
            raise ValueError('Invalid relay expiry')
    if args[0] in ('up', 'down') and not re.fullmatch(r'[A-Za-z0-9_-]+', args[1]):
        raise ValueError('Invalid window ID')
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
        remote = '''import json,os,subprocess,sys
from pathlib import Path
p=json.load(sys.stdin)
os.environ.update(p['env'])
code=subprocess.call(p['argv'])
if code == 0 and p['argv'][1] == 'up':
    port=Path(os.environ.get('RELAY_STATE', str(Path.home()/'.b300-relay')))/(p['argv'][2]+'.port')
    if port.is_file():
        value=port.read_text().strip()
        if value.isdigit(): print('RELAY_PORT='+value)
sys.exit(code)
'''
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
    port = None
    if args[0] == 'up':
        output = result.stdout or ''
        match = re.search(r'^window '+re.escape(args[1])+r' serving via relay port (\d+)\b', output, re.M)
        if not match:
            match = re.search(r'^RELAY_PORT=(\d+)\s*$', output, re.M)
        if match:
            port = int(match[1])
        elif not env.get('RELAY_HOST'):
            state = Path(env.get('RELAY_STATE', str(Path(env.get('HOME', str(Path.home())))/'.b300-relay')))
            saved = state/(args[1]+'.port')
            if saved.is_file() and saved.read_text().strip().isdigit():
                port = int(saved.read_text().strip())
        if port is None or not 18000 <= port <= 18999:
            raise RuntimeError('Netops relay.sh returned no valid chosen relay port')
    if env.get('RUN_DIR') and args[0] in ('up', 'down'):
        status = Path(env['RUN_DIR'])/'relay.status.json'
        status.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(status.read_text()) if status.exists() else {}
        data.update(window_id=args[1], phase='ready' if args[0] == 'up' else 'stopped')
        if port is not None:
            data['relay_port'] = port
            data['node_ssh_port'] = int(args[args.index('--port')+1]) if '--port' in args else 22
            data['expires_at'] = int(args[args.index('--expires')+1]) if '--expires' in args else None
        temporary = status.with_suffix('.tmp')
        temporary.write_text(json.dumps(data)+'\n')
        temporary.replace(status)
    print('Netops relay.sh '+args[0]+' completed')
    return port


if __name__ == '__main__':
    call(sys.argv[1:])
