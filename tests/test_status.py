"""status.sh against a fake nvidia-smi, fake weight dirs and a fake runs dir."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import node_status  # noqa: E402

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
                   "real_downloads = node_status.downloads\n"
                   "def between_samples(env):\n"
                   "    rows = real_downloads(env)\n"
                   f"    with open({str(self.partial)!r}, 'r+b') as stream: stream.truncate({MODEL['files'][1]['size']})\n"
                   "    return rows\n"
                   "node_status.downloads = between_samples\n"
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

    def test_json_once(self):
        started = time.monotonic()
        proc = subprocess.run([str(ROOT/'scripts/status.sh'), '--json'], env=self.env,
                              capture_output=True, text=True, timeout=20)
        took = time.monotonic()-started
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1)
        snap = json.loads(proc.stdout)
        self.assertEqual(set(snap), SCHEMA_KEYS)
        self.assertEqual(snap['schema'], 1)
        self.assertRegex(snap['ts'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        self.assertEqual(snap['phase'], 'downloading')
        self.assertFalse(snap['ready'])
        self.assertEqual(snap['run_id'], 'node-run-20261102T140000.000000Z')
        self.assertAlmostEqual(snap['run_s'], 300, delta=10)
        self.assertEqual(len(snap['gpus']), 8)
        self.assertEqual(snap['gpus'][7], {'i': 7, 'used_gib': 7.0, 'total_gib': 268.6, 'util': 70})
        self.assertEqual(snap['downloads'][0]['have'], self.have)
        self.assertEqual(snap['downloads'][0]['done'], 1)
        self.assertEqual([d['label'] for d in snap['downloads'][1:]], [g['repo'].split('/')[-1] for g in GUARDS])
        self.assertEqual(snap['rate_bps'], 0)
        self.assertIsNone(snap['eta_s'])
        self.assertEqual(snap['gate']['name'], 'warm')
        self.assertEqual(snap['gate']['status'], 'running')
        self.assertIsNone(snap['gate']['reason'])
        self.assertEqual(snap['passed'], ['preflight', 'warm-timeout', 'key'])
        self.assertEqual(snap['upcoming'][0], 'guards')
        self.assertEqual(snap['total_gates'], len(snap['gates'])+len(snap['upcoming']))
        self.assertEqual(snap['warm'], {'state': 'warming', 'running': ['weights_and_hash', 'image'], 'failed': []})
        self.assertEqual([(g['name'], g['port']) for g in snap['guards']], [('shieldstral', 8600), ('qwen', 8601)])
        self.assertEqual(snap['fit']['state'], 'not_run')
        self.assertIsNone(snap['tunnel']['connected'])
        logs = {row['path']: row['exists'] for row in snap['logs']}
        self.assertTrue(logs['~/runs/warm-20261102T140010-abcdef12/weights.log'])
        self.assertFalse(logs['~/runs/node-run-20261102T140000.000000Z/warm.log'])
        self.assertLess(took, 2)

    def test_json_loop_is_ndjson(self):
        proc = subprocess.Popen([str(ROOT/'scripts/status.sh'), '--json', '--loop', '0.5'], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.3)
        with open(self.partial, 'r+b') as f:
            f.truncate(MODEL['files'][1]['size'])
        time.sleep(1.5)
        proc.send_signal(2)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, err)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertGreaterEqual(len(rows), 2)
        self.assertNotIn('\033', out)
        self.assertIsNone(rows[0]['rate_bps'])
        self.assertIsNone(rows[0]['eta_s'])
        self.assertTrue(any(r['rate_bps'] and r['eta_s'] for r in rows[1:]), rows)

    def test_json_with_no_run_and_no_gpus(self):
        env = dict(self.env, PATH='/usr/bin:/bin', RUNS_DIR=str(self.folder/'empty'))
        proc = subprocess.run([str(ROOT/'scripts/status.sh'), '--json'], env=env, capture_output=True, text=True,
                              timeout=10)
        snap = json.loads(proc.stdout)
        self.assertEqual(snap['phase'], 'no_run')
        self.assertIsNone(snap['gpus'])
        self.assertIsNone(snap['run_id'])
        self.assertEqual(snap['logs'], [])
        self.assertEqual(snap['fit']['state'], 'not_run')


SCHEMA_KEYS = {'schema', 'ts', 'node_id', 'phase', 'ready', 'run_id', 'started_at', 'run_s', 'gpus', 'downloads',
               'download_have', 'download_target', 'rate_bps', 'eta_s', 'gate', 'gates', 'passed', 'upcoming',
               'total_gates', 'warm', 'guards', 'fit', 'tunnel', 'logs', 'runs_dir'}


class FakeGuard(BaseHTTPRequestHandler):
    def do_GET(self):
        code, delay = self.server.reply
        time.sleep(delay)
        self.send_response(code if self.path == '/health' else 404)
        self.end_headers()

    def log_message(self, *args):
        pass


def serve(code, delay=0):
    server = ThreadingHTTPServer(('127.0.0.1', 0), FakeGuard)
    server.reply = (code, delay)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class PartTests(unittest.TestCase):
    def test_guard_probes_run_in_parallel_with_a_short_timeout(self):
        healthy, sick, hung = serve(200), serve(503), serve(200, delay=3)
        closed = serve(200)
        closed_port = closed.server_address[1]
        closed.shutdown()
        closed.server_close()
        for server in (healthy, sick, hung):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        ports = (('a', healthy.server_address[1]), ('b', sick.server_address[1]),
                 ('c', hung.server_address[1]), ('d', closed_port))
        with unittest.mock.patch.object(node_status, 'GUARD_PORTS', ports), \
                node_status.ThreadPoolExecutor(max_workers=8) as pool:
            started = time.monotonic()
            probes = node_status.start_probes(pool)
            result = {name: future.result() for name, _, future in probes['guards']}
            took = time.monotonic()-started
        self.assertEqual(result, {'a': {'up': True, 'http': 200}, 'b': {'up': False, 'http': 503},
                                  'c': {'up': False, 'http': None}, 'd': {'up': False, 'http': None}})
        self.assertLess(took, 1.8)

    def test_upcoming_gates_follow_steady_or_calibration_mode(self):
        for mode, resume, next_gate, absent in (
                ('steady', False, 'tunnel', 'loadtest'),
                ('steady', True, 'tunnel', 'loadtest'),
                ('calibrate', False, 'guards', None),
                ('calibrate', True, 'guards', None)):
            with self.subTest(mode=mode, resume=resume):
                names = ['preflight', 'key', 'resume'] if resume else ['preflight', 'warm-timeout', 'key', 'warm']
                record = {'state': 'running', 'mode': mode,
                          'gates': [{'name': name, 'status': 'PASS'} for name in names]}
                state = node_status.gates_state(record, time.time())
                self.assertEqual(state['upcoming'][0], next_gate)
                self.assertEqual(state['total_gates'], len(names)+len(state['upcoming']))
                if absent:
                    self.assertNotIn(absent, state['upcoming'])
                else:
                    self.assertIn('loadtest', state['upcoming'])

    def test_fit_state(self):
        self.assertEqual(node_status.fit_state(None)['state'], 'not_run')
        self.assertEqual(node_status.fit_state({'gates': [{'name': 'warm', 'status': 'PASS'}]})['state'], 'not_run')
        record = {'decision': 64, 'gates': [{'name': 'fit', 'status': 'PASS'}],
                  'measurements': {'loadtests': [{'concurrency': 64, 'requests': 7800, 'non_200': 0,
                                                  'invalid_responses': 0, 'p99_ms': 87.46}]},
                  'fit': [{'concurrency': 64, 'passed': True, 'reasons': [], 'p99_ms': 87.46}]}
        self.assertEqual(node_status.fit_state(record), {'state': 'pass', 'concurrency': 64, 'p99_ms': 87.46,
                                                         'requests': 7800, 'errors': 0, 'reasons': []})
        record['gates'][0]['status'] = 'running'
        self.assertEqual(node_status.fit_state(record)['state'], 'running')
        record['gates'][0]['status'] = 'FAIL'
        record['fit'][-1].update(passed=False, reasons=['loadtest p99 is not below 100 ms'])
        self.assertEqual(node_status.fit_state(record)['reasons'], ['loadtest p99 is not below 100 ms'])

    def test_tunnel_state_from_report_and_process(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        binary = Path(folder)/'cloudflared'
        shutil.copy(shutil.which('sleep'), binary)
        proc = subprocess.Popen([str(binary), '30'])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        record = {'tunnel': {'status': 'up', 'pid': proc.pid, 'registered_seconds': 4.2, 'reason': None}}
        self.assertEqual(node_status.tunnel_state(record), {'status': 'up', 'running': True, 'connected': True,
                                                            'registered_s': 4.2, 'reason': None})
        self.assertTrue(node_status.tunnel_state({'tunnel': {}})['running'])
        proc.kill()
        proc.wait()
        state = node_status.tunnel_state(record)
        self.assertFalse(state['running'])
        self.assertFalse(state['connected'])
        self.assertIsNone(node_status.tunnel_state({'tunnel': {'status': 'not started', 'pid': None}})['connected'])
        # The raw shared cloudflared log is never opened.
        self.assertNotIn('cloudflared.log', (ROOT/'scripts/node_status.py').read_text())

    def test_free_text_keeps_only_loopback_addresses(self):
        text = 'GET https://mirror.example.com/x failed; 192.0.2.7 refused; http://127.0.0.1:8600/health ok'
        self.assertEqual(node_status.scrub(text),
                         'GET https://<host>/x failed; <ip> refused; http://127.0.0.1:8600/health ok')

    def test_failed_gate_is_failed_phase_with_exact_reason(self):
        runs = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, runs)
        now = time.time()
        (runs/'node-run-20261102T150000.000000Z.json').write_text(json.dumps({
            'state': 'failed', 'started_epoch': now-60, 'gates': [
                {'name': 'preflight', 'status': 'FAIL', 'started_epoch': now-60, 'ended_epoch': now-50,
                 'reason': 'disk needs 600 GB free'}]}))
        with unittest.mock.patch.object(node_status, 'GUARD_PORTS', ()), \
                node_status.ThreadPoolExecutor(max_workers=2) as pool:
            snap, _ = node_status.snapshot({'PATH': '/usr/bin:/bin', 'WEIGHTS_DIR': str(runs/'w')}, runs, None,
                                           node_status.start_probes(pool))
        self.assertEqual(snap['phase'], 'failed')
        self.assertEqual(snap['gate'], {'name': 'preflight', 'status': 'fail', 'elapsed_s': 10.0,
                                        'reason': 'disk needs 600 GB free'})
        self.assertEqual(snap['upcoming'], [])
        self.assertEqual(json.loads(json.dumps(snap)), snap)


if __name__ == '__main__':
    unittest.main()
