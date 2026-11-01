"""Secrets arrive through Docker stdin, never Docker env metadata."""
import json
import os
import subprocess
import sys
payload = json.loads(sys.stdin.readline())
os.environ.update(payload['env'])
if payload['kind'] == 'engine':
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    args = subprocess.check_output(['python3', '/kit/scripts/engine.py']).decode().strip('\0').split('\0')
    if os.environ['ENGINE'] == 'vllm':
        # v0.32.0 middleware reads this directly; no CLI or Docker metadata key.
        os.environ['VLLM_API_KEY'] = os.environ['INFER_UPSTREAM_KEY']
    else:
        # No native env/file option verified in SGLang. Narrow fallback: only
        # this inner process argv carries it; outer Docker argv stays key-free.
        args += ['--api-key', os.environ['INFER_UPSTREAM_KEY']]
else:
    args = ['litellm', '--config', '/kit/config/litellm.yaml', '--host', '127.0.0.1',
            '--port', os.environ['GATEWAY_PORT']]
os.execvp(args[0], args)
