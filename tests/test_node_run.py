"""Offline node rehearsal: fake hardware/containers/warm, loopback mocked verdicts."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import pty
import re
import shutil
import itertools
import site
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
# Fake HOME hides the real user site; keep deps installed with pip --user importable.
USER_SITE = os.pathsep.join(filter(None, [site.getusersitepackages(), os.environ.get('PYTHONPATH', '')]))
sys.path.insert(0, str(ROOT/'scripts'))
import guards
import node_run
import node_tunnel
import relay_config
import safety
import safety_loadtest
import serve_plan
import warm
from test_concurrency_fit import selected, pool_log

# Rehearsal-only sentinel, not a credential. It is delivered through pipes only.
KEY = 'rehearsal-only-9d0b1d2a'
TUNNEL_TOKEN = 'eyJ'+'A'*60
PIN = 'a'*40

NVIDIA = r'''import os
from pathlib import Path
import sys
state = Path(os.environ['FAKE_STATE'])
query = ' '.join(sys.argv)
count = int(os.environ.get('FAKE_GPU_COUNT', '8'))
if '--query-compute-apps=' in query:
    if os.environ.get('FAKE_NO_PROCESS_QUERY') == '1':
        sys.exit(1)
    print('GPU-7, 1234, 20000')
elif 'index,uuid,memory.total,memory.used' in query:
    guards = (state/'guards').exists()
    limit = (state/'limit').read_text() if (state/'limit').exists() else '128'
    for i in range(count):
        used = 240000 + (20000 if guards and i == 7 else 0)
        if i == 7 and guards and os.environ.get('FAKE_HEADROOM') == '1' and limit == '128':
            used = 287000
        if i == 7 and os.environ.get('FAKE_HEADROOM_ALWAYS') == '1':
            used = 287000
        print(f'{i}, GPU-{i}, 288000, {used}')
else:
    for i in range(count):
        print('NVIDIA B300, 288000 MiB')
'''

DOCKER = r'''import json
import os
from pathlib import Path
import sys
assert 'SAFETY_CLASSIFIER_KEY' not in os.environ
assert 'CF_TUNNEL_TOKEN' not in os.environ and 'TUNNEL_TOKEN' not in os.environ
assert all('rehearsal-only-9d0b1d2a' not in arg for arg in sys.argv)
assert all('eyJ'+'A'*60 not in arg for arg in sys.argv)
state = Path(os.environ['FAKE_STATE'])
with (state/'docker.jsonl').open('a') as out:
    out.write(json.dumps(sys.argv[1:])+'\n')
if sys.argv[1] == 'ps':
    path = state/'containers'
    print(path.read_text() if path.exists() else '')
elif sys.argv[1] == 'run':
    assert '-d' in sys.argv
    assert '--restart' not in sys.argv
    assert sys.argv[sys.argv.index('--host')+1] == '127.0.0.1'
    assert sys.argv[sys.argv.index('--revision')+1] == 'a'*40
    assert any('@sha256:' in arg for arg in sys.argv)
    (state/'guards').touch()
    print('fake-container')
elif sys.argv[1] == 'inspect' and '--format' in sys.argv:
    print('true')
elif sys.argv[1] == 'inspect':
    # The engine container, as warm.sh left it.
    path = state/'inspect.json'
    if not path.exists():
        sys.exit(1)
    print(path.read_text())
'''

WARM = r'''import json
import os
from pathlib import Path
import sys
assert 'SAFETY_CLASSIFIER_KEY' not in os.environ
assert 'CF_TUNNEL_TOKEN' not in os.environ and 'TUNNEL_TOKEN' not in os.environ
assert all('eyJ'+'A'*60 not in arg for arg in sys.argv)
key = sys.stdin.readline().rstrip('\r\n')
assert key == 'rehearsal-only-9d0b1d2a'
assert sys.argv[1:3] == ['--layout', 'tp8']
assert os.environ['SAFETY'] == ('0' if os.environ.get('PRIVATE_UNGUARDED') == '1' else '1')
state = Path(os.environ['FAKE_STATE'])
limit = int(os.environ['MAX_RUNNING_REQUESTS'])
if os.environ.get('FAKE_WARM_FALLBACK') == '1':
    limit = 64
(state/'limit').write_text(str(limit))
with (state/'warm.jsonl').open('a') as out:
    out.write(json.dumps({'limit': limit, 'timeout': int(os.environ['WARM_TIMEOUT_SECONDS']),
                          'window_end': int(os.environ['WINDOW_END']),
                          'args': sys.argv[1:]})+'\n')
base = Path(os.environ['NODE_RUN_DIR'])/'warm'
base.mkdir(exist_ok=True)
path = base.with_suffix('.json')
tokens = 3000000 if os.environ.get('FAKE_SMALL_POOL') == '1' else 6000000
if os.environ.get('FAKE_TINY_POOL') == '1':
    tokens = 100
running = 96 if os.environ.get('FAKE_CLAMPED') == '1' and limit == 128 else limit
log = f'max_total_num_tokens={tokens}, max_running_requests={running}\n'
if os.environ.get('FAKE_MISSING_POOL') == '1':
    log = 'hello world\n'
(base/'engine.log').write_text(log)
revision = json.loads(Path('config/model-manifest.json').read_text())['model']['revision']
ready = {'state': 'ready', 'timing_file': str(path), 'max_running_requests_effective': limit,
         'container': 'b300-warm-fixture', 'revision': revision}
path.write_text(json.dumps({'state': 'ready', 'config': {'MAX_RUNNING_REQUESTS': str(limit)},
                            'image_ref': os.environ['ENGINE_IMAGE'], 'image_id': 'sha256:'+'b'*64,
                            'concurrency': {'effective': limit}}))
(state/'inspect.json').write_text(json.dumps([{
    'State': {'Running': True}, 'Image': 'sha256:'+'b'*64,
    'Mounts': [{'Destination': '/weights', 'Source': str(Path(os.environ['WEIGHTS_DIR']).absolute())}]}]))
print('READY '+json.dumps(ready))
'''

TUNNEL = r'''import os
from pathlib import Path
import subprocess
import sys
assert os.environ['CF_TUNNEL_TOKEN'] == 'eyJ'+'A'*60
assert all(name not in os.environ for name in ('SAFETY_CLASSIFIER_KEY', 'INFER_UPSTREAM_KEY', 'TUNNEL_TOKEN'))
assert all('eyJ'+'A'*60 not in arg for arg in sys.argv)
assert sys.stdin.read() == ''
assert os.getsid(0) == os.getpid()
home = Path(os.environ['HOME'])
if (home/'echo-token').exists():
    print(os.environ['CF_TUNNEL_TOKEN'])
    print(os.environ['CF_TUNNEL_TOKEN'], file=sys.stderr)
if (home/'fail-tunnel').exists():
    sys.exit(1)
(home/'tunnel-started').touch()
# A real detached child named cloudflared (a copy of sleep), like nohup in Netops' helper.
child = subprocess.Popen([str(home/'cloudflared'), '20'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
if not (home/'no-register').exists():
    with (home/'cloudflared.log').open('a') as log:
        log.write('INF Registered tunnel connection connIndex=0 location=fixture\n')
print(f'tunnel started, pid {child.pid}')
'''


def cloudflared_env(home):
    """A verified stand-in binary, so preflight staging never touches the network."""
    binary = home/'cloudflared'
    if not binary.exists():
        shutil.copy(shutil.which('sleep'), binary)
    return {'CLOUDFLARED_BIN': str(binary), 'CLOUDFLARED_SHA256': node_tunnel.sha256(binary),
            'CLOUDFLARED_LOG': str(home/'cloudflared.log')}


def tunnel_fixture(home):
    script = home/'node-tunnel.sh'
    script.write_text('#!'+sys.executable+'\n'+TUNNEL)
    script.chmod(0o700)
    config = home/'fixture.env'
    # Plain assignment deliberately exercises node-env.sh's set -a semantics.
    config.write_text('CF_TUNNEL_TOKEN="'+TUNNEL_TOKEN+'"\n')
    config.chmod(0o600)
    return config


class RehearsalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.before = set((ROOT/'runs').glob('node-run-*'))
        self.addCleanup(self.remove_runs)
        self.mode = ''
        self.calls = []
        self.tunnel_calls = []
        app = web.Application()
        app.router.add_get('/health', self.health)
        app.router.add_post('/v1/safety/csam', self.classify)
        app.router.add_post('/v1/chat/completions', self.guard)
        app.router.add_get('/v1/models', self.models)
        self.server = TestServer(app, host='127.0.0.1', handler_cancellation=True)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.url = str(self.server.make_url('/')).rstrip('/')
        for name, body in [('nvidia-smi', NVIDIA), ('docker', DOCKER), ('warm.sh', WARM)]:
            path = self.folder/name
            path.write_text('#!'+sys.executable+'\n'+body)
            path.chmod(0o700)
        self.env = {'PATH': str(self.folder)+':'+os.environ['PATH'], 'HOME': str(self.folder), 'PYTHONPATH': USER_SITE,
                    'NODE_ENV_FILE': str(tunnel_fixture(self.folder)), 'NODE_RUN_TESTING': '1',
                    'NODE_RUN_WARM_CMD': str(self.folder/'warm.sh'), 'NODE_RUN_RELAY_URL': self.url,
                    'NODE_RUN_TUNNEL_PUBLIC_URL': self.url, 'TUNNEL_CONNECT_TIMEOUT': '1.5',
                    'NODE_RUN_ENGINE_HEALTH_URL': self.url, **cloudflared_env(self.folder),
                    'NODE_RUN_GUARD_HEALTH_URL': self.url, 'FAKE_STATE': str(self.folder),
                    'WEIGHTS_DIR': str(self.folder/'weights'), 'GUARD_WEIGHTS_DIR': str(self.folder/'guards-cache'),
                    'DISK_SPACE_ACTION': 'warn', 'TUNNEL_SLOT': 'a', 'INFER_UPSTREAM_KEY': 'upstream-fixture',
                    'SAFETY_MODEL_REVISION': PIN, 'SAFETY_QWEN_REVISION': PIN,
                    'LOADTEST_DURATION_SECONDS': '0.12', 'GUARD_TIMEOUT_SECONDS': '1'}

    def remove_runs(self):
        for path in set((ROOT/'runs').glob('node-run-*'))-self.before:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    async def health(self, request):
        return web.Response(text='hello world', status=503 if self.mode in ('guard-down', 'engine-down') else 200)

    async def models(self, request):
        # The engine behind a private relay: no key, no models.
        return web.json_response({'error': 'Unauthorized'}, status=401)

    async def guard(self, request):
        """Mocked guard verdicts only: Shieldstral asks for logprobs, Qwen3Guard for text."""
        body = await request.json()
        if self.mode == 'guard-garbage':
            return web.json_response({}, status=500)
        if body.get('logprobs'):
            return web.json_response({'choices': [{'logprobs': {'content': [{'top_logprobs': [
                {'token': 'no', 'logprob': -0.01}, {'token': 'yes', 'logprob': -6.0}]}]}}]})
        return web.json_response({'choices': [{'message': {'content': 'Safety: Safe\nCategories: None'}}]})

    async def classify(self, request):
        if request.headers.get('Authorization') != 'Bearer '+KEY:
            if (self.folder/'tunnel-started').exists():
                self.tunnel_calls.append((dict(request.headers), await request.json()))
                if self.mode == 'tunnel-530':
                    return web.json_response({}, status=530)
            return web.json_response({'error': 'unauthorized'}, status=401)
        body = await request.json()
        self.calls.append(body)
        # Slow only load-test windows, not the short smoke calls; no verdict text.
        if len(body['text']) > 100 and self.mode in ('slow-128', 'slow-always'):
            limit = (self.folder/'limit').read_text()
            if limit == '128' or self.mode == 'slow-always':
                await asyncio.sleep(0.14)
        if len(body['text']) > 100 and self.mode == 'refuse':
            return web.json_response({}, status=503)
        if len(body['text']) > 100 and self.mode == 'invalid':
            return web.json_response({'flag': False})
        return web.json_response({'flag': body['text'] == safety.CANARY, 'score': 0.123, 'model': 'mock@fixture'})

    async def invoke(self, key=KEY+'\r\n', tty=False, **env):
        if tty:
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            self.addCleanup(os.close, slave)
            stdin = slave
        else:
            stdin = asyncio.subprocess.PIPE
        process = await asyncio.create_subprocess_exec(str(ROOT/'scripts/node-run.sh'),
                    cwd=ROOT, env={**self.env, **env}, stdin=stdin,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(None if tty else key.encode()), 10)
        output = stdout.decode()+stderr.decode()
        # Do not echo leaked values on assertion failures.
        self.assertFalse(KEY in output, 'key leaked to process output')
        self.assertFalse(TUNNEL_TOKEN in output, 'tunnel token leaked to process output')
        reports = sorted(set((ROOT/'runs').glob('node-run-*.json'))-self.before)
        self.assertTrue(reports, output)
        report = json.loads(reports[-1].read_text())
        for path in (ROOT/'runs').rglob('*'):
            if path.is_file():
                self.assertFalse(KEY.encode() in path.read_bytes(), 'key leaked to runs')
                self.assertFalse(TUNNEL_TOKEN.encode() in path.read_bytes(), 'tunnel token leaked to runs')
        self.assertIn('TEARDOWN BY ', output)
        return process.returncode, output, report

    async def test_happy_path_128_secret_free_and_guard_memory(self):
        code, output, report = await self.invoke()
        self.assertEqual(code, 0, output)
        self.assertEqual(report['decision'], 128)
        self.assertEqual(output.count('NODE READY FOR DRILL '), 1)
        self.assertIn('fit=128 ', output)
        self.assertIn('guard_vram_mib=20000 ', output)
        self.assertIn('TUNNEL UP url='+self.url, output)
        self.assertLess(output.index('TUNNEL UP'), output.index('NODE READY FOR DRILL'))
        self.assertLess(output.index('NODE READY FOR DRILL'), output.index('PASS key-leak'))
        self.assertLess(output.index('PASS key-leak'), output.index('TEARDOWN BY'))
        self.assertEqual(report['tunnel']['status'], 'up')
        self.assertIsInstance(report['tunnel']['pid'], int)
        self.assertTrue(report['tunnel']['launched'])
        self.assertEqual(report['tunnel']['binary']['source'], 'cached')
        bench = report['guard_bench']
        self.assertEqual(sorted(bench['guards']), sorted(plan['name'] for plan in report['guards']))
        for row in bench['guards'].values():
            self.assertEqual(row['requests'], 8*len(node_run.BENCH_TEXTS))
            self.assertLessEqual(row['p50_ms'], row['p99_ms'])
            self.assertEqual(row['benign_flagged'], 0)
            self.assertIn('vram_mib', row)
        self.assertEqual(bench['canary_flag'], {'input': True, 'output': True})
        self.assertIn('GUARD BENCH ', output)
        self.assertEqual(report['cleanup'], {'containers': [], 'relay_pids': []})
        self.assertTrue(self.tunnel_calls)
        for headers, body in self.tunnel_calls:
            self.assertNotIn('Authorization', headers)
            self.assertEqual(body, {'text': 'hello world', 'phase': 'input'})
        self.assertEqual([r['name'] for r in report['gates']], list(node_run.GATES))
        self.assertTrue(all(r['status'] == 'PASS' and r['seconds'] >= 0 for r in report['gates']))
        self.assertEqual(report['warm_timeout_seconds'], 3600)
        exported = json.loads((self.folder/'warm.jsonl').read_text())
        self.assertEqual(exported['timeout'], 3600)
        docker = [json.loads(line) for line in (self.folder/'docker.jsonl').read_text().splitlines()]
        self.assertEqual(len([args for args in docker if args[0] == 'run']), 2)
        self.assertTrue(report['image_digest'].startswith('sha256:'))
        self.assertRegex(report['git_sha'], r'^[0-9a-f]{40}$')
        self.assertTrue(any(r['phase'] == 'output' for r in self.calls))
        self.assertTrue(all(r['text'] == safety.CANARY or r['text'].replace('hello world ', '') in ('', 'hell')
                            or r['text'] == 'hello world' for r in self.calls))

    async def test_seven_gpus_fail_preflight(self):
        code, output, report = await self.invoke(FAKE_GPU_COUNT='7')
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL preflight:', output)
        self.assertEqual(report['state'], 'failed')
        self.assertFalse((self.folder/'warm.jsonl').exists())

    async def assert_tunnel_preflight_fails(self, mode):
        config = tunnel_fixture(self.folder)
        script = self.folder/'node-tunnel.sh'
        if mode == 'missing script': script.unlink()
        if mode == 'nonexecutable script': script.chmod(0o600)
        if mode == 'missing token': config.write_text('')
        if mode == 'empty token': config.write_text('CF_TUNNEL_TOKEN=""\n')
        code, output, report = await self.invoke(CF_TUNNEL_TOKEN=TUNNEL_TOKEN)
        self.assertNotEqual(code, 0)
        reason = ('tunnel script ~/node-tunnel.sh missing or not executable' if 'script' in mode else
                  'CF_TUNNEL_TOKEN missing or empty in '+str(config))
        self.assertIn('FAIL preflight: '+reason, output)
        self.assertIn('TEARDOWN BY', output)
        self.assertEqual(report['tunnel']['preflight']['status'], 'FAIL')
        self.assertNotIn('NODE READY FOR DRILL', output)
        self.assertNotIn(TUNNEL_TOKEN, output)
        self.assertFalse((self.folder/'warm.jsonl').exists())
        self.assertFalse((self.folder/'docker.jsonl').exists())

    async def test_tunnel_preflight_missing_script(self):
        await self.assert_tunnel_preflight_fails('missing script')

    async def test_tunnel_preflight_nonexecutable_script(self):
        await self.assert_tunnel_preflight_fails('nonexecutable script')

    async def test_tunnel_preflight_missing_token(self):
        await self.assert_tunnel_preflight_fails('missing token')

    async def test_tunnel_preflight_empty_token(self):
        await self.assert_tunnel_preflight_fails('empty token')

    async def test_tunnel_up_full_script(self):
        code, output, report = await self.invoke()
        self.assertEqual(code, 0, output)
        lines = output.splitlines()
        ready = next(i for i, line in enumerate(lines) if line.startswith('NODE READY FOR DRILL '))
        up = lines.index('TUNNEL UP url='+self.url)
        teardown = next(i for i, line in enumerate(lines) if line.startswith('TEARDOWN BY'))
        self.assertLess(up, ready)
        self.assertLess(ready, teardown)
        self.assertNotIn('TUNNEL NOT STARTED', output)
        self.assertEqual(report['decision'], 128)
        self.assertEqual(report['state'], 'ready')
        self.assertTrue(report['tunnel']['launched'])
        self.assertEqual(report['tunnel']['reason'], None)
        self.assertNotIn(TUNNEL_TOKEN, output+json.dumps(report))

    async def test_tunnel_is_a_hard_gate_and_keeps_the_engine(self):
        for mode, reason in (('tunnel-530', 'never reached the relay'), ('no-register', 'registered no edge connection'),
                             ('launch-failure', 'tunnel helper failed')):
            self.mode = mode
            if mode == 'launch-failure': (self.folder/'fail-tunnel').touch()
            if mode == 'no-register': (self.folder/'no-register').touch()
            code, output, report = await self.invoke()
            self.assertEqual(code, 1, output)
            self.assertIn('FAIL tunnel: ', output)
            self.assertIn(reason, report['tunnel']['reason'])
            self.assertEqual(report['decision'], 128)
            self.assertEqual(report['state'], 'failed')
            self.assertNotIn('NODE READY FOR DRILL', output)
            self.assertIn('ENGINE KEPT', output)
            self.assertIn(f"RESUME={report['report'].rsplit('/', 1)[1][:-5]} ./scripts/node-run.sh", output)
            self.assertEqual(report['tunnel']['launched'], mode != 'launch-failure')
            docker = (self.folder/'docker.jsonl').read_text()
            self.assertNotIn('b300-warm-fixture', docker)
            for name in ('fail-tunnel', 'no-register'):
                (self.folder/name).unlink(missing_ok=True)

    async def test_tunnel_stdout_and_stderr_are_redacted(self):
        (self.folder/'echo-token').touch()
        code, output, report = await self.invoke()
        self.assertEqual(code, 0, output)
        self.assertIn('TUNNEL UP url='+self.url, output)
        log = (Path(report['run_dir'])/'tunnel.log').read_text()
        self.assertEqual(log.count('[REDACTED]'), 2)

    async def test_unpinned_image_cannot_be_relaxed(self):
        code, output, _ = await self.invoke(ENGINE_IMAGE='fixture:latest', ALLOW_UNPINNED='1')
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL preflight: Unpinned image', output)

    async def test_empty_key_and_tty_fail_without_warm(self):
        for options in ({'key': '\r\n'}, {'tty': True}):
            code, output, _ = await self.invoke(**options)
            self.assertNotEqual(code, 0)
            self.assertIn('FAIL key:', output)
        self.assertFalse((self.folder/'warm.jsonl').exists())

    async def test_kv_short_falls_back_once(self):
        code, output, report = await self.invoke(FAKE_SMALL_POOL='1')
        self.assertEqual(code, 0, output)
        self.assertEqual(report['decision'], 64)
        self.assertIn('KV pool', report['fallback_reason'][0])
        launches = [json.loads(line) for line in (self.folder/'warm.jsonl').read_text().splitlines()]
        self.assertEqual([row['limit'] for row in launches], [128, 64])
        self.assertIn('--resume-at-64', launches[1]['args'])
        self.assertEqual([row['concurrency'] for row in report['measurements']['loadtests']], [128, 64])

    async def test_private_unguarded_skips_guards_and_reaches_handoff(self):
        code, output, report = await self.invoke(PRIVATE_UNGUARDED='1')
        self.assertEqual(code, 0, output)
        self.assertIn('SKIP guards', output)
        self.assertIn('NODE READY', output)
        self.assertEqual(report['guards'], 'skipped: PRIVATE_UNGUARDED=1')
        self.assertFalse((self.folder/'state'/'guards').exists() if (self.folder/'state').exists() else False)

    async def test_guard_rank_headroom_falls_back(self):
        code, output, report = await self.invoke(FAKE_HEADROOM='1')
        self.assertEqual(code, 0, output)
        self.assertEqual(report['decision'], 64)
        self.assertEqual(report['fit'][0]['ranks'][7]['free_mib'], 1000)
        self.assertGreaterEqual(report['fit'][1]['ranks'][7]['free_mib'], 4096)

    async def test_engine_clamp_falls_back(self):
        code, output, report = await self.invoke(FAKE_CLAMPED='1')
        self.assertEqual(code, 0, output)
        self.assertEqual(report['decision'], 64)
        self.assertIn('clamped', report['fallback_reason'][0])

    async def test_latency_breach_at_128_falls_back(self):
        self.mode = 'slow-128'
        code, output, report = await self.invoke()
        self.assertEqual(code, 0, output)
        self.assertEqual(report['decision'], 64)
        self.assertGreaterEqual(report['measurements']['loadtests'][0]['p99_ms'], 100)
        self.assertLess(report['measurements']['loadtests'][1]['p99_ms'], 100)

    async def test_64_failure_stops(self):
        for mode, extra in (('', {'FAKE_TINY_POOL': '1'}), ('slow-always', {}),
                            ('', {'FAKE_HEADROOM_ALWAYS': '1'})):
            self.mode = mode
            code, output, report = await self.invoke(**extra)
            self.assertNotEqual(code, 0, output)
            self.assertIsNone(report['decision'])
            self.assertNotIn('NODE READY FOR DRILL ', output)

    async def test_refusals_and_invalid_shapes_stop_without_fallback(self):
        for mode in ('refuse', 'invalid'):
            self.mode = mode
            code, output, _ = await self.invoke()
            self.assertNotEqual(code, 0)
            self.assertIn('FAIL loadtest:', output)
            self.assertNotIn('PASS warm-64', output)

    async def test_missing_pool_stops(self):
        code, output, _ = await self.invoke(FAKE_MISSING_POOL='1')
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL kv-pool:', output)

    async def test_guard_health_timeout_and_unpinned_revision(self):
        self.mode = 'guard-down'
        code, output, _ = await self.invoke(GUARD_TIMEOUT_SECONDS='0.05')
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL guards:', output)
        code, output, _ = await self.invoke(SAFETY_QWEN_REVISION='main')
        self.assertNotEqual(code, 0)
        self.assertIn('pinned 40-character', output)

    async def test_warm_timeout_override_and_deadline_cap_epoch_and_iso(self):
        for origin, timeout in ((str(time.time()-10000), '3600'),
                                (datetime.fromtimestamp(time.time()-10000, timezone.utc).isoformat(), '3600'),
                                (str(time.time()), '123')):
            code, output, report = await self.invoke(PROVISIONED_AT=origin, WARM_TIMEOUT_SECONDS=timeout)
            self.assertEqual(code, 0, output)
            self.assertLessEqual(report['warm_timeout_seconds'], int(timeout))
            self.assertLessEqual(report['warm_timeout_seconds'],
                                 report['teardown_deadline_epoch']-report['started_epoch'])
            if timeout == '123':
                self.assertEqual(report['warm_timeout_seconds'], 123)
            else:
                self.assertLess(report['warm_timeout_seconds'], 500)

    async def test_mode_600_node_env_and_occupied_engine_port(self):
        config = self.folder/'fixture.env'
        config.write_text('CF_TUNNEL_TOKEN="'+TUNNEL_TOKEN+'"\nSAFETY_MAX_INFLIGHT=128\n')
        config.chmod(0o644)
        code, output, _ = await self.invoke(NODE_ENV_FILE=str(config))
        self.assertNotEqual(code, 0)
        self.assertIn('FAIL preflight:', output)
        config.chmod(0o600)
        code, output, _ = await self.invoke(NODE_ENV_FILE=str(config), ENGINE_PORT=str(self.server.port))
        self.assertNotEqual(code, 0)
        self.assertIn(f'port {self.server.port} (engine) is held by a process that is not ours', output)


    async def first_run_fails_at_tunnel(self):
        (self.folder/'no-register').touch()
        code, output, report = await self.invoke()
        self.assertEqual(code, 1, output)
        (self.folder/'no-register').unlink()
        return Path(report['report']).stem, report

    async def test_rehearsal_guards_relay_safety_and_resume(self):
        """make dry-run: a late failure keeps the engine, RESUME reruns only the late gates."""
        run_id, first = await self.first_run_fails_at_tunnel()
        self.assertEqual(first['resume_command'], f'RESUME={run_id} ./scripts/node-run.sh < /path/to/classifier-key')
        before_calls = len(self.calls)
        # cloudflared connected after the first attempt gave up.
        with (self.folder/'cloudflared.log').open('a') as log:
            log.write('INF Registered tunnel connection connIndex=0\n')
        docker_before = (self.folder/'docker.jsonl').read_text().splitlines()
        code, output, report = await self.invoke(RESUME=run_id)
        self.assertEqual(code, 0, output)
        self.assertEqual(report['state'], 'ready')
        resumed = [r['name'] for r in report['gates']][len(first['gates']):]
        self.assertEqual(resumed, list(node_run.RESUME_GATES))
        self.assertTrue(all(r['status'] == 'PASS' for r in report['gates'][len(first['gates']):]))
        # No re-warm, no new guard containers, no cleanup of what is running.
        self.assertEqual(len((self.folder/'warm.jsonl').read_text().splitlines()), 1)
        docker = [json.loads(line) for line in (self.folder/'docker.jsonl').read_text().splitlines()[len(docker_before):]]
        self.assertFalse([args for args in docker if args[0] in ('run', 'rm', 'ps')])
        self.assertEqual(sorted(report['measurements']['guard_memory']['reused']),
                         sorted(plan['name'] for plan in report['guards']))
        self.assertTrue(report['tunnel']['reused'])
        self.assertEqual(report['resumes'][0]['container'], 'b300-warm-fixture')
        self.assertGreater(len(self.calls), before_calls)  # relay-safety and loadtest ran again
        self.assertIn('NODE READY FOR DRILL', output)
        self.assertEqual(report['teardown_deadline_epoch'], first['teardown_deadline_epoch'])

    async def test_resume_fails_closed_on_any_mismatch(self):
        run_id, first = await self.first_run_fails_at_tunnel()
        inspect = self.folder/'inspect.json'
        good = json.loads(inspect.read_text())
        warm_report = Path(first['warm_runs'][0]['ready']['timing_file'])
        cases = (
            ('missing', lambda: inspect.unlink(), 'no longer exists'),
            ('stopped', lambda: inspect.write_text(json.dumps([{**good[0], 'State': {'Running': False}}])), 'is not running'),
            ('image', lambda: inspect.write_text(json.dumps([{**good[0], 'Image': 'sha256:'+'c'*64}])), 'different image'),
            ('weights', lambda: inspect.write_text(json.dumps([{**good[0], 'Mounts': [
                {'Destination': '/weights', 'Source': '/elsewhere'}]}])), 'different weights directory'),
            ('health', lambda: setattr(self, 'mode', 'engine-down'), 'did not return 200'),
        )
        for name, breaks, message in cases:
            with self.subTest(case=name):
                inspect.write_text(json.dumps(good))
                self.mode = ''
                breaks()
                code, output, _ = await self.invoke(RESUME=run_id)
                self.assertEqual(code, 1, output)
                self.assertIn('FAIL resume: ', output)
                self.assertIn(message, output)
                self.assertNotIn('PASS guards', output)
        self.mode = ''
        inspect.write_text(json.dumps(good))
        record = json.loads(Path(first['report']).read_text())
        record['warm_runs'][0]['ready']['revision'] = 'f'*40
        Path(first['report']).write_text(json.dumps(record))
        code, output, _ = await self.invoke(RESUME=run_id)
        self.assertIn('revision differs', output)
        warm = json.loads(warm_report.read_text())
        warm['image_ref'] = 'other@sha256:'+'d'*64
        warm_report.write_text(json.dumps(warm))
        code, output, _ = await self.invoke(RESUME=run_id)
        self.assertIn('does not match the pinned image', output)
        self.assertEqual(len((self.folder/'warm.jsonl').read_text().splitlines()), 1)

    async def test_resume_rejects_unknown_or_malformed_ids(self):
        for run_id in ('../etc', 'node-run-20261101T000000.000000Z'):
            process = await asyncio.create_subprocess_exec(str(ROOT/'scripts/node-run.sh'), cwd=ROOT,
                        env={**self.env, 'RESUME': run_id}, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(process.communicate(KEY.encode()+b'\n'), 10)
            self.assertEqual(process.returncode, 1)
            self.assertIn('FAIL resume: ', stdout.decode())
        self.assertFalse((self.folder/'warm.jsonl').exists())

    async def test_preflight_removes_stale_containers_and_our_relay(self):
        (self.folder/'containers').write_text('b300-warm-old\nnode-run-old-qwen\nsomeone-else\n')
        relay = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(ROOT/'scripts/sse_relay.py')])
        self.addCleanup(relay.kill)
        code, output, report = await self.invoke()
        self.assertEqual(code, 0, output)
        self.assertEqual(report['cleanup']['containers'], ['b300-warm-old', 'node-run-old-qwen'])
        self.assertEqual(report['cleanup']['relay_pids'], [relay.pid])
        self.assertIsNotNone(relay.wait(timeout=5))
        docker = [json.loads(line) for line in (self.folder/'docker.jsonl').read_text().splitlines()]
        self.assertIn(['rm', '-f', 'b300-warm-old', 'node-run-old-qwen'], docker)
        self.assertLess(docker.index(['rm', '-f', 'b300-warm-old', 'node-run-old-qwen']),
                        next(i for i, args in enumerate(docker) if args[0] == 'run'))
        self.assertIn('CLEANUP removed 2 stale container(s) and stopped 1 old relay(s)', output)

    async def test_guard_bench_failure_fails_guards_and_keeps_engine(self):
        self.mode = 'guard-garbage'
        code, output, report = await self.invoke()
        self.assertEqual(code, 1, output)
        self.assertIn('FAIL guards: guard benchmark: a guard did not return a valid score', output)
        self.assertIn('ENGINE KEPT', output)


class PureNodeTests(unittest.TestCase):
    def test_dry_run_does_not_read_stdin_source_env_or_create_runs(self):
        before = set((ROOT/'runs').glob('*'))
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            config = Path(folder)/'fixture.env'
            marker = Path(folder)/'touched'
            config.write_text('touch "'+str(marker)+'"\n')
            config.chmod(0o600)
            result = subprocess.run([str(ROOT/'scripts/node-run.sh')], capture_output=True, text=True,
                                     env={**os.environ, 'NODE_ENV_FILE': str(config), 'DRY_RUN': '1'}, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
        self.assertEqual(before, set((ROOT/'runs').glob('*')))
        self.assertIn('PLAN handoff', result.stdout)

    def test_key_reader_first_line_crlf_no_prompt(self):
        self.assertEqual(relay_config.read_classifier_key(io.StringIO(KEY+'\r\nignored\n')), KEY)
        for value in ('', '\n', '\r\n', '  \n', 'hello\0world'):
            with self.assertRaises(ValueError):
                relay_config.read_classifier_key(io.StringIO(value))
        with self.assertRaisesRegex(ValueError, 'TTY'):
            relay_config.read_classifier_key(Mock(isatty=Mock(return_value=True)))

    def test_allowlist_matches_bootstrap_and_excludes_other_secrets(self):
        script = (ROOT/'bootstrap.sh').read_text()
        names = set(re.findall(r'\bSAFETY[A-Z_]*\b', script.split('for name in ', 1)[1].split('; do', 1)[0]))
        self.assertEqual(names, set(relay_config.SAFETY_ENV))
        env = {name: 'fixture' for name in names}
        env.update(SAFETY='1', SAFETY_CLASSIFIER_KEY=KEY, HF_TOKEN='unrelated', INFER_UPSTREAM_KEY='unrelated',
                   CF_TUNNEL_TOKEN=TUNNEL_TOKEN, TUNNEL_TOKEN=TUNNEL_TOKEN)
        result = relay_config.relay_environment(env, 8000, 8001)
        self.assertEqual(result['SAFETY_CLASSIFIER_KEY'], KEY)
        self.assertNotIn('HF_TOKEN', result)
        self.assertNotIn('INFER_UPSTREAM_KEY', result)
        self.assertNotIn('CF_TUNNEL_TOKEN', result)
        self.assertNotIn('TUNNEL_TOKEN', result)
        self.assertNotIn('SAFETY_CLASSIFIER_KEY', relay_config.relay_environment({**env, 'SAFETY': '0'}, 8000, 8001))

    def test_fit_token_count_does_not_corrupt_json_redaction(self):
        from redact import redact, secret_values
        values = secret_values({'FIT_TOKENS_PER_STREAM': '32768', 'SAFETY_CLASSIFIER_KEY': KEY,
                                'NODE_RUN_STDIN': '1'})
        report = {'tokens_per_stream': 32768, 'started_epoch': 1234, 'fixture': KEY}
        saved = json.loads(redact(json.dumps(report), values))
        self.assertEqual(saved['tokens_per_stream'], 32768)
        self.assertEqual(saved['started_epoch'], 1234)
        self.assertFalse(KEY in saved['fixture'])

    def test_warm_relay_receives_key_but_report_and_helpers_do_not(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, patch('warm.ROOT', Path(folder)):
            env = {'TUNNEL_SLOT': 'a', 'WINDOW_END': str(int(time.time())+10000), 'SAFETY': '1',
                   'SAFETY_CLASSIFIER_KEY': KEY, 'SAFETY_MODEL_REVISION': PIN, 'SAFETY_QWEN_REVISION': PIN}
            with patch.dict(os.environ, {'SAFETY_CLASSIFIER_KEY': KEY}):
                obj = warm.Warm(env)
            process = Mock(pid=1234, poll=Mock(return_value=None))
            with patch('warm.subprocess.Popen', return_value=process) as spawn, patch('socket.create_connection'):
                obj.start_relay(selected())
            child_env = spawn.call_args.kwargs['env']
            self.assertEqual(child_env['SAFETY_CLASSIFIER_KEY'], KEY)
            self.assertEqual(child_env['SAFETY'], '1')
            self.assertNotIn('SAFETY_CLASSIFIER_KEY', obj.child_env)
            self.assertFalse(KEY in repr(spawn.call_args.args), 'key in relay argv')
            obj.publish_ready = Mock()
            for path in Path(folder).rglob('*'):
                if path.is_file():
                    self.assertFalse(KEY.encode() in path.read_bytes(), 'key persisted')

    def test_resume_reuses_engine_fallback_without_restarting_relay_or_extending_window(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, patch('warm.ROOT', Path(folder)), \
             redirect_stdout(io.StringIO()):
            env = {'TUNNEL_SLOT': 'a', 'WINDOW_END': str(int(time.time())+10000),
                   'WARM_TIMEOUT_SECONDS': '60', 'SAFETY': '1', 'SAFETY_CLASSIFIER_KEY': KEY}
            first = warm.Warm(env)
            first.record.update(first.image_meta, config=selected(), state='ready',
                                container=first.name, image_id='sha256:'+'b'*64,
                                concurrency={'requested': 128, 'effective': 128, 'fit': {'passed': True}})
            first.save()
            (first.run/'engine.log').write_text(pool_log(6000000))
            with self.assertRaisesRegex(ValueError, 'original WINDOW_END'):
                warm.Warm.resume_at_64(first.path, {**env, 'WINDOW_END': str(int(env['WINDOW_END'])+1)})
            obj, config = warm.Warm.resume_at_64(first.path, env)
            obj.command = Mock(return_value='fixture-start')
            obj.health, obj.start_relay, obj.weights = Mock(), Mock(), Mock()

            def launch(image, selected_config):
                self.assertEqual(selected_config['MAX_RUNNING_REQUESTS'], '64')
                self.assertEqual(selected_config['WARM_GRAPH_BS'], '1,2,4,8,16,32,64')
                (obj.run/'engine.log').write_text(pool_log(6000000, 64))

            obj.launch = Mock(side_effect=launch)
            obj.run_fallback(config)
            obj.start_relay.assert_not_called()
            obj.weights.assert_not_called()
            obj.command.assert_any_call(['docker', 'rm', '-f', first.name])
            self.assertTrue((obj.run/'engine-128.log').exists())
            ready = json.loads(obj.ready.read_text())
            self.assertEqual(ready['max_running_requests_effective'], 64)
            self.assertTrue(obj.record['concurrency']['fallback_fit']['passed'])
            self.assertLessEqual(obj.end, time.time()+60)
            with self.assertRaises(ValueError):
                warm.Warm.resume_at_64(obj.path, env)
            self.assertFalse(KEY in obj.path.read_text(), 'key in fallback report')

    def test_guard_plans_use_one_pin_loopback_no_restart_and_revisions(self):
        env = {'SAFETY': '1', 'SAFETY_MODEL_REVISION': PIN, 'SAFETY_QWEN_REVISION': PIN,
               'WEIGHTS_DIR': '/tmp/weights'}
        image = serve_plan.image_reference({})
        plan = guards.plans(env, image, 'rehearsal')
        self.assertEqual([p['port'] for p in plan], [8600, 8601])
        for p in plan:
            self.assertIn(image['image_ref'], p['argv'])
            self.assertEqual(p['argv'][p['argv'].index('--host')+1], '127.0.0.1')
            self.assertNotIn('--restart', p['argv'])
            self.assertEqual(p['gpu'], 0)
        for name in ('SAFETY_MODEL_REVISION', 'SAFETY_QWEN_REVISION'):
            with self.assertRaises(ValueError):
                guards.plans({**env, name: 'main'}, image, 'rehearsal')

    def test_literal_leak_detector_fails_without_printing_value(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, patch('node_run.ROOT', Path(folder)):
            obj = node_run.NodeRun()
            obj.key = KEY
            obj.tunnel_token = TUNNEL_TOKEN
            # In-memory stream models a file leak without storing the sentinel on disk.
            obj.run.joinpath('fixture.log').touch()
            real_open = Path.open

            for value in (KEY, TUNNEL_TOKEN):
                def opened(path, *args, **kwargs):
                    if path.name == 'fixture.log':
                        return io.BytesIO(b'x'*(1024*1024-5)+value.encode())
                    return real_open(path, *args, **kwargs)

                with patch.object(Path, 'open', opened), self.assertRaises(node_run.GateFailure) as caught:
                    obj.check_key_leak()
                self.assertFalse(value in str(caught.exception), 'failure exposes credential')

    def test_fit_decision_and_strict_64_floor_without_network(self):
        for fail_kind in ('none', 'kv', 'clamp', 'rank', 'latency', 'floor'):
            with self.subTest(fail_kind=fail_kind), tempfile.TemporaryDirectory(dir='/tmp') as folder, \
                 patch('node_run.ROOT', Path(folder)), redirect_stdout(io.StringIO()):
                obj = node_run.NodeRun()
                obj.current, obj.context, obj.tokens, obj.headroom = 128, 262144, 32768, 4096
                obj.pool_value = parse = warm.parse_pool(pool_log(3000000 if fail_kind == 'kv' else 6000000,
                                                                  96 if fail_kind == 'clamp' else 128))
                obj.load = safety_loadtest.LoadResult(50, 1, 0, 0, 1, 2, 101 if fail_kind == 'latency' else 3)
                good = [{'index': i, 'free_mib': 10000} for i in range(8)]
                bad = [dict(r, free_mib=1000 if r['index'] == 7 else 10000) for r in good]
                obj.memory = Mock(side_effect=[bad if fail_kind in ('rank', 'floor') else good,
                                               bad if fail_kind == 'floor' else good])

                def fallback(**kwargs):
                    obj.current = 64
                    obj.pool_value = {**parse, 'engine_max_running_requests': 64}
                    obj.load = safety_loadtest.LoadResult(50, 1, 0, 0, 1, 2, 3)

                obj.warm, obj.pool, obj.loadtest = Mock(side_effect=fallback), Mock(), Mock()
                obj.relay_safety = AsyncMock()
                if fail_kind == 'floor':
                    with self.assertRaises(node_run.GateFailure):
                        obj.fit()
                else:
                    obj.fit()
                    self.assertEqual(obj.record['decision'], 128 if fail_kind == 'none' else 64)
                self.assertEqual(obj.warm.call_count, 0 if fail_kind == 'none' else 1)

    def test_provisioning_epoch_and_iso(self):
        self.assertEqual(node_run.provisioned('1000', 2000), 1000)
        self.assertEqual(node_run.provisioned('1970-01-01T00:16:40Z', 2000), 1000)
        with self.assertRaises(node_run.GateFailure):
            node_run.provisioned('1970-01-01T00:16:40', 2000)

    def test_full_runner_with_mock_transport_when_tcp_is_unavailable(self):
        """Separate control-flow coverage; the real HTTP rehearsal above stays mandatory."""
        for mode in ('happy', 'kv', 'rank', 'latency', 'seven', 'empty', 'pin', 'cap',
                     'tunnel-echo', 'tunnel-timeout', 'tunnel-failure'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                scratch = Path(folder)
                for name, body in [('nvidia-smi', NVIDIA), ('docker', DOCKER), ('warm.sh', WARM)]:
                    executable = scratch/name
                    executable.write_text('#!'+sys.executable+'\n'+body)
                    executable.chmod(0o700)
                env = {'PATH': folder+':'+os.environ['PATH'], 'HOME': folder, 'PYTHONPATH': USER_SITE,
                       'NODE_ENV_FILE': str(tunnel_fixture(scratch)), 'NODE_RUN_TESTING': '1',
                       'NODE_RUN_WARM_CMD': str(scratch/'warm.sh'), 'FAKE_STATE': folder,
                       'WEIGHTS_DIR': str(scratch/'weights'), 'GUARD_WEIGHTS_DIR': str(scratch/'cache'),
                       'DISK_SPACE_ACTION': 'warn', 'TUNNEL_SLOT': 'a', 'INFER_UPSTREAM_KEY': 'fixture',
                       'SAFETY_MODEL_REVISION': PIN, 'SAFETY_QWEN_REVISION': PIN,
                       'LOADTEST_DURATION_SECONDS': '0.01', 'FIT_TOKENS_PER_STREAM': '32768',
                       'TUNNEL_CONNECT_TIMEOUT': '2', **cloudflared_env(scratch)}
                if mode == 'tunnel-echo': (scratch/'echo-token').touch()
                if mode == 'tunnel-failure': (scratch/'fail-tunnel').touch()
                if mode == 'kv': env['FAKE_SMALL_POOL'] = '1'
                if mode == 'rank': env['FAKE_HEADROOM'] = '1'
                if mode == 'seven': env['FAKE_GPU_COUNT'] = '7'
                if mode == 'pin': env['ENGINE_IMAGE'] = 'fixture:latest'
                if mode == 'cap': env['PROVISIONED_AT'] = str(time.time()-10000)
                with patch.dict(os.environ, env, clear=True):
                    obj = node_run.NodeRun()
                try:
                    original_preflight = obj.preflight

                    def preflight():
                        with patch('node_run.socket.socket'):
                            original_preflight()

                    async def measured(*args, **kwargs):
                        return safety_loadtest.LoadResult(10, 0.1, 0, 0, 1, 2,
                                                          101 if mode == 'latency' and obj.current == 128 else 3)

                    obj.preflight = preflight
                    obj.guards_healthy, obj.relay_safety = AsyncMock(), AsyncMock()
                    obj.guard_bench = AsyncMock(return_value={})
                    output = io.StringIO()
                    with redirect_stdout(output), patch('sys.stdin', io.StringIO('' if mode == 'empty' else KEY+'\n')), \
                         patch('safety_loadtest.run_live_load_test', side_effect=measured), \
                         patch('node_tunnel.reachable', new=AsyncMock(return_value=mode != 'tunnel-timeout')):
                        if mode in ('seven', 'empty', 'pin', 'tunnel-timeout', 'tunnel-failure'):
                            with self.assertRaises(node_run.GateFailure):
                                obj.run_all()
                            if mode.startswith('tunnel'):
                                self.assertIn('FAIL tunnel: '+('tunnel connected but' if mode == 'tunnel-timeout'
                                                               else 'tunnel helper failed'), output.getvalue())
                        else:
                            obj.run_all()
                            self.assertEqual(obj.record['decision'], 64 if mode in ('kv', 'rank', 'latency') else 128)
                            self.assertEqual(output.getvalue().count('NODE READY FOR DRILL '), 1)
                            self.assertEqual(obj.record['warm_timeout_seconds'],
                                             3600 if mode != 'cap' else obj.record['warm_runs'][0]['warm_timeout_seconds'])
                            if mode == 'cap': self.assertLess(obj.record['warm_timeout_seconds'], 500)
                            self.assertIn('TUNNEL UP url=', output.getvalue())
                            if mode == 'tunnel-echo':
                                self.assertEqual((obj.run/'tunnel.log').read_text().count('[REDACTED]'), 2)
                    self.assertFalse(KEY in output.getvalue(), 'key leaked to stdout')
                    self.assertFalse(TUNNEL_TOKEN in output.getvalue(), 'tunnel token leaked to stdout')
                    saved = json.loads(obj.path.read_text())
                    self.assertEqual(saved['gates'], obj.record['gates'])
                    for path in [obj.path, *obj.run.rglob('*')]:
                        if path.is_file():
                            self.assertFalse(KEY.encode() in path.read_bytes(), 'key persisted')
                            self.assertFalse(TUNNEL_TOKEN.encode() in path.read_bytes(), 'tunnel token persisted')
                finally:
                    import signal
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    if obj.lock is not None: obj.lock.close()
                    obj.path.unlink(missing_ok=True)
                    shutil.rmtree(obj.run)

    def test_prerequisite_failures_end_to_end_without_network(self):
        for mode in ('missing script', 'nonexecutable script', 'missing token', 'empty token'):
            before = set((ROOT/'runs').glob('node-run-*'))
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir='/tmp') as folder:
                home = Path(folder)
                config = tunnel_fixture(home)
                script = home/'node-tunnel.sh'
                if mode == 'missing script': script.unlink()
                if mode == 'nonexecutable script': script.chmod(0o600)
                if mode == 'missing token': config.write_text('')
                if mode == 'empty token': config.write_text('CF_TUNNEL_TOKEN=""\n')
                for name, body in (('docker', DOCKER), ('warm.sh', WARM)):
                    (home/name).write_text('#!'+sys.executable+'\n'+body)
                    (home/name).chmod(0o700)
                env = {'PATH': folder+':'+os.environ['PATH'], 'HOME': folder, 'PYTHONPATH': USER_SITE,
                       'NODE_ENV_FILE': str(config), 'NODE_RUN_TESTING': '1', 'FAKE_STATE': folder,
                       'NODE_RUN_WARM_CMD': str(home/'warm.sh'), 'CF_TUNNEL_TOKEN': TUNNEL_TOKEN,
                       'PROVISIONED_AT': str(time.time()-100)}
                try:
                    proc = subprocess.run([str(ROOT/'scripts/node-run.sh')], env=env, input=KEY+'\n',
                                          capture_output=True, text=True, timeout=3)
                    output = proc.stdout+proc.stderr
                    self.assertNotEqual(proc.returncode, 0)
                    expected = ('tunnel script ~/node-tunnel.sh missing or not executable' if 'script' in mode else
                                'CF_TUNNEL_TOKEN missing or empty in '+str(config))
                    self.assertIn('FAIL preflight: '+expected, output)
                    self.assertIn('TEARDOWN BY ', output)
                    self.assertNotIn('NODE READY FOR DRILL ', output)
                    self.assertNotIn('TUNNEL NOT STARTED', output)
                    self.assertFalse((home/'docker.jsonl').exists())
                    self.assertFalse((home/'warm.jsonl').exists())
                    self.assertFalse(TUNNEL_TOKEN in output, 'tunnel token leaked')
                    reports = set((ROOT/'runs').glob('node-run-*.json'))-before
                    self.assertEqual(len(reports), 1)
                    report = json.loads(reports.pop().read_text())
                    self.assertEqual(report['tunnel']['preflight']['status'], 'FAIL')
                    self.assertLess(report['teardown_deadline_epoch'], report['started_epoch']+10560)
                finally:
                    for path in set((ROOT/'runs').glob('node-run-*'))-before:
                        if path.is_dir(): shutil.rmtree(path)
                        else: path.unlink()

    def test_tunnel_needs_only_sha256sum(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            config = tunnel_fixture(Path(folder))
            env = {'HOME': folder, 'NODE_ENV_FILE': str(config), 'PATH': folder}
            result = node_tunnel.prerequisites(env, TUNNEL_TOKEN)
            self.assertEqual((result['status'], result['missing_tools']), ('FAIL', ['sha256sum']))
            env['PATH'] = os.environ['PATH']
            self.assertEqual(node_tunnel.prerequisites(env, TUNNEL_TOKEN)['status'], 'PASS')

    def test_cloudflared_comes_from_the_mirror_then_github_and_is_always_verified(self):
        import hashlib
        import http.server
        import threading
        good, bad = b'pinned cloudflared bytes', b'tampered bytes'
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get('X-Mirror-Key')))
                routes = getattr(self.server, 'routes')
                if self.path not in routes:
                    self.send_response(404); self.end_headers(); return
                status, body = routes[self.path]
                self.send_response(status)
                if status == 302:
                    self.send_header('Location', '/elsewhere')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        mirror_path = '/mirror/'+node_tunnel.MIRROR_OBJECT
        self.assertEqual(node_tunnel.MIRROR_OBJECT, 'tools/cloudflared/2026.10.7/cloudflared-linux-amd64')
        with tempfile.TemporaryDirectory(dir='/tmp') as folder, \
             patch('node_tunnel.platform.machine', return_value='x86_64'):
            dest = Path(folder)/'cloudflared'
            env = {'NODE_RUN_TESTING': '1', 'CLOUDFLARED_BIN': str(dest), 'MIRROR_URL': base+'/mirror',
                   'MIRROR_AUTH_HEADER': 'X-Mirror-Key: fixture-only', 'CLOUDFLARED_GITHUB_URL': base+'/github',
                   'CLOUDFLARED_SHA256': hashlib.sha256(good).hexdigest()}
            for routes, source, failed in (
                ({mirror_path: (200, good)}, 'mirror', []),
                ({mirror_path: (200, bad), '/github': (200, good)}, 'github', ['mirror: checksum mismatch']),
                ({mirror_path: (302, b''), '/elsewhere': (200, good), '/github': (200, good)}, 'github',
                 ['mirror: download failed']),
                ({'/github': (200, good)}, 'github', ['mirror: download failed']),
            ):
                dest.unlink(missing_ok=True)
                server.routes, seen[:] = routes, []
                result = node_tunnel.stage_binary(env, timeout=5)
                self.assertEqual((result['source'], result['failed']), (source, failed))
                self.assertEqual(dest.read_bytes(), good)
                self.assertTrue(os.access(dest, os.X_OK))
                self.assertEqual(seen[0], (mirror_path, 'fixture-only'))
                # The mirror credential never follows a redirect or goes to GitHub.
                self.assertTrue(all(key is None for path, key in seen[1:]))
            self.assertEqual(node_tunnel.stage_binary(env)['source'], 'cached')
            dest.write_bytes(bad)
            server.routes = {mirror_path: (200, bad), '/github': (200, bad)}
            with self.assertRaises(ValueError) as caught:
                node_tunnel.stage_binary(env, timeout=5)
            self.assertIn('no verified cloudflared', str(caught.exception))
            self.assertNotIn('fixture-only', str(caught.exception))
            self.assertNotIn('127.0.0.1', str(caught.exception))
            self.assertEqual(dest.read_bytes(), bad)  # not replaced, and not executed by us
            self.assertEqual(list(Path(folder).glob('*.new')), [])
            with self.assertRaises(ValueError):
                node_tunnel.stage_binary({**env, 'CLOUDFLARED_SHA256': 'nope'})
        self.assertEqual(node_tunnel.CLOUDFLARED_SHA256,
                         '1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872')

    def test_registered_reads_only_new_log_lines(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            log = Path(folder)/'cloudflared.log'
            self.assertFalse(node_tunnel.registered(log, 0))
            log.write_bytes(b'INF Registered tunnel connection connIndex=0\n')
            offset = node_tunnel.log_size(log)
            self.assertTrue(node_tunnel.registered(log, 0))
            self.assertFalse(node_tunnel.registered(log, offset))
            with log.open('ab') as stream:
                stream.write(b'x'*(1024*1024-10)+b'INF Registered tunnel connection connIndex=1\n')
            self.assertTrue(node_tunnel.registered(log, offset))

    def test_percentile_nearest_rank(self):
        self.assertIsNone(node_run.percentile([], 0.5))
        values = sorted(float(i) for i in range(1, 101))
        self.assertEqual((node_run.percentile(values, 0.5), node_run.percentile(values, 0.99)), (50.0, 99.0))
        self.assertEqual(node_run.percentile([7.0], 0.99), 7.0)

    def test_private_tunnel_probe_accepts_an_engine_401_on_models(self):
        for status, expected in ((401, True), (404, False), (530, False)):
            response = Mock(status=status)
            request = AsyncMock()
            request.__aenter__.return_value = response
            session = Mock(get=Mock(return_value=request))
            context = AsyncMock()
            context.__aenter__.return_value = session
            with patch('aiohttp.ClientSession', return_value=context), \
                 patch('node_tunnel.time', Mock(monotonic=Mock(side_effect=itertools.chain([0], itertools.repeat(2))))), \
                 patch('node_tunnel.asyncio.sleep', new=AsyncMock()):
                self.assertEqual(asyncio.run(node_tunnel.reachable('https://example.invalid', 1, private=True)), expected)
            self.assertEqual(session.get.call_args.args[0], 'https://example.invalid/v1/models')
            self.assertNotIn('headers', session.get.call_args.kwargs)

    def test_tunnel_probe_requires_relay_401_without_credentials(self):
        for status, payload, expected in ((401, {'error': 'unauthorized'}, True),
                                          (401, {}, False), (530, {}, False), (502, {}, False)):
            response = Mock(status=status, json=AsyncMock(return_value=payload))
            request = AsyncMock()
            request.__aenter__.return_value = response
            session = Mock(post=Mock(return_value=request))
            context = AsyncMock()
            context.__aenter__.return_value = session
            with patch('aiohttp.ClientSession', return_value=context), \
                 patch('node_tunnel.time', Mock(monotonic=Mock(side_effect=itertools.chain([0], itertools.repeat(2))))), \
                 patch('node_tunnel.asyncio.sleep', new=AsyncMock()):
                self.assertEqual(asyncio.run(node_tunnel.reachable('https://example.invalid', 1)), expected)
            kwargs = session.post.call_args.kwargs
            self.assertNotIn('auth', kwargs)
            self.assertNotIn('headers', kwargs)
            self.assertFalse(kwargs['allow_redirects'])
            self.assertEqual(kwargs['json'], {'text': 'hello world', 'phase': 'input'})


if __name__ == '__main__':
    unittest.main()
