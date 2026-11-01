"""Run the real Mac watcher orchestration with fake SSH, rsync and Netops helper."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!PYTHON
import json,os,pathlib,sys
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
base=pathlib.Path(os.environ['MOCK_ROOT'])
with (base/'calls.jsonl').open('a') as out: out.write(json.dumps([name,*args])+'\\n')
if name=='ssh':
 cmd=args[-1]
 if 'node-network.py' in cmd:
  json.load(sys.stdin)
  print(json.dumps({'requested':'auto','selected':'ssh','ssh_direction':'forward','probes':{}}))
 elif 'watcher/remote.py' in cmd:
  json.load(sys.stdin); print('Bootstrap started')
 elif 'status.json' in cmd:
  count=int((base/'count').read_text()) if (base/'count').exists() else 0
  (base/'count').write_text(str(count+1))
  print(json.dumps({'phase':'ready' if count==0 else 'stopped','ok':'ok','ready':count==0}))
 elif 'test -f' in cmd:
  count=int((base/'count').read_text()) if (base/'count').exists() else 0
  sys.exit(0 if count>1 else 1)
'''


class WatcherFlowTests(unittest.TestCase):
    def test_push_then_bootstrap_then_up_then_drained_down(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); repo=base/'kit'
            shutil.copytree(ROOT,repo,ignore=shutil.ignore_patterns('.git','.venv','runs','logs','staging','__pycache__'))
            binaries=base/'bin'; binaries.mkdir()
            for name in ('ssh','rsync','relay.sh'):
                p=binaries/name; p.write_text(FAKE.replace('PYTHON',sys.executable)); p.chmod(0o755)
            staging=base/'staged'/'fp8'
            (staging/'weights').mkdir(parents=True); (staging/'images').mkdir()
            for f in json.loads((repo/'docs/model-metadata.json').read_text())['variants']['fp8']['files']:
                p=staging/'weights'/f['path']; p.parent.mkdir(parents=True,exist_ok=True); p.touch()
            (staging/'images'/'engine.tar.zst').write_bytes(b'fake image; never loaded')
            (staging/'images'/'engine.json').write_text('{}')
            key=base/'key-ref'; key.touch()  # Path fixture, no key material.
            claim=base/'claim.json'
            claim.write_text(json.dumps({'host':'node.example.invalid','port':22,'user':'ubuntu',
                                        'key':{'path':str(key)},'expires_at':int(time.time())+3600}))
            env=dict(os.environ,PATH=str(binaries)+':'+os.environ['PATH'],MOCK_ROOT=str(base),HOME=str(base),
                     RELAY_SH=str(binaries/'relay.sh'),STAGING_DIR=str(base/'staged'),NETWORK_MODE='auto',
                     SSH_DIRECTION='forward',FIRST_WINDOW='0',POLL_SECONDS='0',NODE_KIT_DIR='kit',WEIGHTS_SOURCE='hf')
            for name in ('RUN_ID','RELAY_HOST','IMAGE_SOURCE','DRY_RUN','MODEL_METADATA','MODEL','MODEL_REVISION','MODEL_VARIANT'):
                env.pop(name,None)
            result=subprocess.run(['/bin/bash',str(repo/'watcher/deploy.sh'),str(claim)],env=env,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            events=[json.loads(line) for line in (base/'calls.jsonl').read_text().splitlines()]
            relay=[e for e in events if e[0]=='relay.sh']
            self.assertEqual([e[1] for e in relay],['push-weights','up','down'])
            self.assertEqual(relay[0][2:],['ubuntu@node.example.invalid',str(staging/'weights'),'/data/weights/DeepSeek-V4.1-Flash-1017',str(key)])
            window=relay[1][2]
            self.assertTrue(window.startswith('window-'))
            self.assertEqual(relay[1][3:],['ubuntu@node.example.invalid',str(key)])
            self.assertEqual(relay[2],['relay.sh','down',window])
            bootstrap=next(i for i,e in enumerate(events) if 'watcher/remote.py' in e[-1])
            statuses=[i for i,e in enumerate(events) if 'cat kit/runs/' in e[-1] and 'status.json' in e[-1]]
            self.assertLess(events.index(relay[0]),bootstrap)
            self.assertLess(statuses[0],events.index(relay[1]))
            self.assertLess(statuses[-1],events.index(relay[2]))
