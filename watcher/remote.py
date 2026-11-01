"""Executed on Linux through SSH stdin; secrets never become shell text or files."""
import json
import os
from pathlib import Path
import subprocess
import sys

payload = json.load(sys.stdin)
root = Path.cwd()
env = os.environ.copy()
env.update(payload['env'])
run = root/'runs'/env['RUN_ID']
run.mkdir(parents=True, exist_ok=True)
# Bootstrap prints only fixed messages; its tools filter output before writing logs.
with (run/'bootstrap.log').open('ab') as log:
    subprocess.Popen([str(root/'bootstrap.sh')], env=env, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, close_fds=True)
print('Bootstrap started')
