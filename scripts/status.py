"""Atomic status updates; only allowlisted metadata reaches disk."""
import datetime
import json
import os
from pathlib import Path
import sys
import time

path = Path(os.environ['RUN_DIR']) / 'status.json'
path.parent.mkdir(parents=True, exist_ok=True)
now = time.time()
state = json.loads(path.read_text()) if path.exists() else {
    'run_id': os.environ['RUN_ID'], 'started_epoch': now, 'timings': [],
    'model': os.environ['MODEL'], 'revision': os.environ['MODEL_REVISION'],
    'engine': os.environ['ENGINE'], 'endpoint': os.environ['ENDPOINT'],
    'public_endpoint': None,
}
phase, result = sys.argv[1:3]
if state.get('ok') == 'running':
    state['timings'].append({'phase': state['phase'], 'seconds': now - state['phase_started_epoch'],
                             'result': result if phase == state['phase'] else 'ok'})
state.update(phase=phase, ok=result, phase_started_epoch=now,
             updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
             elapsed_seconds=now-state['started_epoch'])
for name in ('gpu.json', 'runtime.json', 'network.json', 'fit.json'):
    p = path.parent / name
    if p.exists():
        state[name[:-5]] = json.loads(p.read_text())
if os.environ.get('WINDOW_END'):
    state['window_end'] = int(os.environ['WINDOW_END'])
    state['teardown_epoch'] = int(os.environ['WINDOW_END']) - 600
state['ready'] = phase in ('ready', 'serving') and result == 'ok'
if not state['ready']:
    state['public_endpoint'] = None
if phase == 'serving' and state.get('network', {}).get('selected') == 'cloudflared':
    state['public_endpoint'] = 'https://api.infer.belweave.ai'
tmp = path.with_suffix('.tmp')
tmp.write_text(json.dumps(state, indent=2) + '\n')
tmp.replace(path)
