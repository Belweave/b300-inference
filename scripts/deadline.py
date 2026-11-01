"""Detached node-local deadline; survives the SSH client disconnecting."""
import os
import json
from pathlib import Path
import signal
import subprocess
import time

end = int(os.environ['WINDOW_END']) - 600
while time.time() < end:
    time.sleep(min(10, end-time.time()))
Path(os.environ['RUN_DIR'], 'stop').touch()
owner = json.loads(Path(os.environ['RUN_DIR'], 'bootstrap-owner.json').read_text())
proc = Path(f"/proc/{owner['pid']}/stat")
if proc.exists() and proc.read_text().split()[21] == owner['start']:
    os.kill(owner['pid'], signal.SIGTERM)
subprocess.run([os.environ['KIT_ROOT']+'/teardown.sh'], check=False)
