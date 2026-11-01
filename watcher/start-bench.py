import json
import os
from pathlib import Path
import subprocess
import sys
payload = json.load(sys.stdin)
env = dict(os.environ, **payload['env'])
root = Path.cwd()
with (root/'runs'/env['RUN_ID']/'bench.log').open('ab') as log:
    proc = subprocess.Popen([str(root/'loadtest.sh')], env=env, stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True, close_fds=True)

(root/"runs"/env["RUN_ID"]/"bench.pid").write_text(str(proc.pid))
