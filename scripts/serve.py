"""Secrets arrive through Docker stdin, never Docker env metadata."""
import json
import os
import subprocess
import sys
payload = json.loads(sys.stdin.readline())
os.environ.update(payload['env'])
if payload['kind'] == 'engine':
    args = subprocess.check_output(['python3', '/kit/scripts/engine.py']).decode().strip('\0').split('\0')
    args += ['--api-key', os.environ['INFER_UPSTREAM_KEY']]
else:
    args = ['litellm', '--config', '/kit/config/litellm.yaml', '--host', '127.0.0.1',
            '--port', os.environ['GATEWAY_PORT']]
os.execvp(args[0], args)
