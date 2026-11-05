"""status.sh against a fake nvidia-smi, fake weight dirs and a fake runs dir."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODEL = json.loads((ROOT/'config/manifests/uncensored-c19ca5c.json').read_text())
GUARDS = json.loads((ROOT/'config/manifests/guards.json').read_text())['guards']
SMI = """#!/bin/sh
case "$*" in *--query-gpu=index,memory.used,memory.total,utilization.gpu*) ;; *) exit 9 ;; esac
for i in 0 1 2 3 4 5 6 7; do echo "$i, $((i * 1024)), 275040, $((i * 10))"; done
"""


class StatusTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        bin_dir = self.folder/'bin'
        bin_dir.mkdir()
        (bin_dir/'nvidia-smi').write_text(SMI)
        (bin_dir/'nvidia-smi').chmod(0o755)
        self.weights = self.folder/'models/dsv41-uncensored'
        self.weights.mkdir(parents=True)
        files = MODEL['files']
        # One whole file, one half-downloaded shard, the rest missing. Sparse files keep this cheap.
        with open(self.weights/files[0]['filename'], 'wb') as f:
            f.truncate(files[0]['size'])
        self.partial = self.weights/(files[1]['filename']+'.incomplete')
        with open(self.partial, 'wb') as f:
            f.truncate(files[1]['size']//2)
        self.have = files[0]['size']+files[1]['size']//2
        guard = GUARDS[0]
        guard_dir = self.folder/'models/guards'/guard['repo']/guard['revision']
        guard_dir.mkdir(parents=True)
        (guard_dir/(guard['files'][0]['filename']+'.part')).write_bytes(b'x'*10)
        runs = self.folder/'runs'
        runs.mkdir()
        now = time.time()
        node = runs/'node-run-20261102T140000.000000Z'
        node.mkdir()
        (node/'guards.log').write_text('')
        node.with_name(node.name+'.json').write_text(json.dumps({'state': 'running', 'started_epoch': now-300, 'gates': [
            {'name': 'preflight', 'status': 'PASS', 'started_epoch': now-300, 'ended_epoch': now-200},
            {'name': 'warm-timeout', 'status': 'PASS', 'started_epoch': now-200, 'ended_epoch': now-200},
            {'name': 'key', 'status': 'PASS', 'started_epoch': now-200, 'ended_epoch': now-200},
            {'name': 'warm', 'status': 'running', 'started_epoch': now-190}]}))
        warm = runs/'warm-20261102T140010-abcdef12'
        warm.mkdir()
        (warm/'operations.log').write_text('')
        (warm/'weights.log').write_text('')
        warm.with_name(warm.name+'.json').write_text(json.dumps({'state': 'warming', 'started_epoch': now-190, 'stages': {
            'hardware': {'start_epoch': now-190, 'end_epoch': now-189, 'ok': True},
            'weights_and_hash': {'start_epoch': now-189}, 'image': {'start_epoch': now-189}}}))
        self.env = {'PATH': f"{bin_dir}:{os.environ['PATH']}", 'HOME': str(self.folder),
                    'WEIGHTS_DIR': str(self.weights), 'RUNS_DIR': str(runs)}

    def test_one_screen_without_a_terminal(self):
        # Grow the fake download between samples, independent of interpreter startup time.
        harness = (f"import sys; sys.path.insert(0, {str(ROOT/'scripts')!r})\n"
                   "import node_status\n"
                   "real_sleep = node_status.time.sleep\n"
                   "def between_samples(seconds):\n"
                   f"    with open({str(self.partial)!r}, 'r+b') as stream: stream.truncate({MODEL['files'][1]['size']})\n"
                   "    real_sleep(seconds)\n"
                   "node_status.time.sleep = between_samples\n"
                   "raise SystemExit(node_status.main())\n")
        proc = subprocess.run([sys.executable, '-c', harness], env=self.env, text=True,
                              capture_output=True, timeout=20)
        out, err = proc.stdout, proc.stderr
        self.assertEqual(proc.returncode, 0, err)
        self.assertNotIn('\033', out)
        self.assertIn('    7     7.0 / 268.6      70%', out)
        self.assertEqual(sum(1 for line in out.splitlines() if '/ 268.6' in line), 8)
        total = sum(f['size'] for f in MODEL['files'])
        self.assertIn(f"model {'':14} {(self.have+MODEL['files'][1]['size']//2)/1e9:7.1f} / {total/1e9:6.1f} GB", out)
        self.assertIn('Shieldstral-1.0-3B', out)
        self.assertRegex(out, r'rate \d+ MB/s  ETA \S+')
        self.assertIn('gate warm: running (', out)
        self.assertIn('warm: warming, running weights_and_hash, image', out)
        self.assertIn('passed: preflight warm-timeout key', out)
        self.assertIn('~/runs/warm-20261102T140010-abcdef12/ operations.log weights.log', out)
        self.assertIn('~/runs/node-run-20261102T140000.000000Z/ guards.log', out)
        self.assertLess(len(out.splitlines()), 40)

    def test_loop_redraws_and_stops_cleanly(self):
        proc = subprocess.Popen([str(ROOT/'scripts/status.sh'), '--loop', '0.5'], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2)
        proc.send_signal(2)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        self.assertGreaterEqual(out.count('b300 status '), 2)
        self.assertIn('rate 0 MB/s', out)

    def test_bad_flag_and_missing_nvidia_smi(self):
        proc = subprocess.run([str(ROOT/'scripts/status.sh'), '--loop', '0'], env=self.env,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        env = dict(self.env, PATH='/usr/bin:/bin', RUNS_DIR=str(self.folder/'empty'))
        proc = subprocess.run([str(ROOT/'scripts/status.sh')], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('nvidia-smi unavailable', proc.stdout)
        self.assertIn('no node-run report yet', proc.stdout)


if __name__ == '__main__':
    unittest.main()
