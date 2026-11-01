"""Probe the node through SSH before deciding whether to push staged artifacts."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from network import save

payload = json.load(sys.stdin)
os.environ.update(payload['env'])
os.environ['RUN_DIR'] = str(Path.cwd()/'runs'/os.environ['RUN_ID'])
print(json.dumps(save(os.environ)))
