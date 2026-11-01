"""128-stream staging: fit decision, 64 fallback, graph ladders, ports, ready.json flags."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import serve_plan as plans
import warm
import warm_logic as logic
from warm_download import load_manifest

FULL = '128,96,64,32,16,8,4,2,1'
LADDER_128 = '1,2,4,8,16,32,64,96,128'
LADDER_64 = '1,2,4,8,16,32,64'


def pool_log(tokens, running=128):
    return (f'ServerArgs(max_running_requests=128, context_length=262144)\n'
            f'max_total_num_tokens={tokens}, chunked_prefill_size=8192, max_prefill_tokens=16384, '
            f'max_running_requests={running}, context_len=262144\n')


def selected(**env):
    return warm.settings(load_manifest(), env, plans.host_gate(env, 900_000_000_000))


class FitDecisionTests(unittest.TestCase):
    def test_defaults_stage_128_with_full_graph_ladder_behind_relay(self):
        config = selected()
        self.assertEqual((config['MAX_RUNNING_REQUESTS'], config['CUDA_GRAPH_MAX_BS']), ('128', '128'))
        self.assertEqual(config['WARM_GRAPH_BS'], LADDER_128)
        self.assertEqual((config['ENGINE_PORT'], config['SSE_RELAY']), ('8001', '1'))
        self.assertEqual(selected(SSE_RELAY='0')['ENGINE_PORT'], '8000')
        for bad in ('8000', '8100', '29500'):
            with self.assertRaises(ValueError): selected(ENGINE_PORT=bad)
        self.assertEqual(selected(MAX_RUNNING_REQUESTS='100')['WARM_GRAPH_BS'], '1,2,4,8,16,32,64,96')

    def test_pool_parse_uses_scheduler_line_not_server_args(self):
        pool = logic.parse_pool(pool_log(40_000_000, 96))
        self.assertEqual((pool['max_total_num_tokens'], pool['engine_max_running_requests']), (40_000_000, 96))
        self.assertIsNone(logic.parse_pool('ServerArgs(max_running_requests=128)')['engine_max_running_requests'])

    def test_fit_passes_and_records_pool(self):
        fit = logic.concurrency_fit(logic.parse_pool(pool_log(5_000_000)), 128, 262144, 32768)
        self.assertTrue(fit['passed'])
        self.assertEqual((fit['max_total_num_tokens'], fit['usable_pool_tokens'], fit['full_context_streams']),
                         (5_000_000, 4_500_000, 17))
        self.assertIsNone(fit['reason'])

    def test_fit_fails_for_each_reason(self):
        small = logic.concurrency_fit(logic.parse_pool(pool_log(4_000_000)), 128, 262144, 32768)
        self.assertFalse(small['passed']); self.assertIn('KV pool 4000000', small['reason'])
        clamped = logic.concurrency_fit(logic.parse_pool(pool_log(50_000_000, 100)), 128, 262144, 32768)
        self.assertIn('clamped max_running_requests to 100', clamped['reason'])
        unknown = logic.concurrency_fit(logic.parse_pool('nothing'), 128, 262144, 32768)
        self.assertIn('no KV pool size', unknown['reason'])
        crashed = logic.concurrency_fit({}, 128, 262144, 32768, started=False)
        self.assertIn('did not reach health', crashed['reason'])
        strict = logic.concurrency_fit(logic.parse_pool(pool_log(30_000_000)), 128, 262144, 262144)
        self.assertFalse(strict['passed'])  # full-context residency is a stricter, opt-in bar

    def test_fallback_settings_return_64_graph_ladder(self):
        fb = logic.fallback_settings(selected())
        self.assertEqual((fb['MAX_RUNNING_REQUESTS'], fb['CUDA_GRAPH_MAX_BS'], fb['WARM_GRAPH_BS']), ('64', '64', LADDER_64))
        self.assertIsNone(logic.fallback_settings(selected(MAX_RUNNING_REQUESTS='64')))
        args = subprocess.check_output([sys.executable, str(ROOT/'scripts/engine.py')], env={**os.environ, **fb}).decode().split('\0')
        self.assertEqual(args[args.index('--max-running-requests')+1], '64')
        self.assertEqual(args[args.index('--cuda-graph-max-bs-decode')+1], '64')
        self.assertEqual(args[args.index('--port')+1], '8001')


class WarmFallbackFlowTests(unittest.TestCase):
    def run_flow(self, logs, healthy=(True, True)):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        with patch('warm.ROOT', Path(folder.name)):
            obj = warm.Warm({'TUNNEL_SLOT': 'a', 'WINDOW_END': str(int(time.time())+3600), 'INFER_UPSTREAM_KEY': 'fixture'})
        launches = []

        def launch(image, config):
            launches.append(config)
            (obj.run/'engine.log').write_text(logs[len(launches)-1])
        results = iter(healthy)

        def health():
            if not next(results): raise warm.EngineStopped('Engine stopped before health')
        obj.launch, obj.health, obj.command = Mock(side_effect=launch), Mock(side_effect=health), Mock(return_value='')
        effective = obj.launch_with_fit_fallback('img', selected())
        return obj, launches, effective

    def test_pass_keeps_128(self):
        obj, launches, effective = self.run_flow([pool_log(5_000_000)])
        self.assertEqual(len(launches), 1)
        self.assertEqual(effective['MAX_RUNNING_REQUESTS'], '128')
        self.assertEqual(obj.record['concurrency']['effective'], 128)
        self.assertIsNone(obj.record['concurrency']['fallback_reason'])

    def test_small_pool_relaunches_once_at_64_and_flags_ready(self):
        obj, launches, effective = self.run_flow([pool_log(1_000_000), pool_log(1_000_000, 64)])
        self.assertEqual([c['MAX_RUNNING_REQUESTS'] for c in launches], ['128', '64'])
        self.assertEqual(launches[1]['WARM_GRAPH_BS'], LADDER_64)
        obj.command.assert_any_call(['docker', 'rm', '-f', obj.name])
        self.assertTrue((obj.run/'engine-128.log').exists())
        c = obj.record['concurrency']
        self.assertEqual((c['requested'], c['effective'], c['cuda_graph_max_bs']), (128, 64, 64))
        self.assertIn('KV pool', c['fallback_reason'])
        self.assertEqual(obj.record['config']['MAX_RUNNING_REQUESTS'], '64')
        with patch('builtins.print'):
            obj.publish_ready()
        ready = json.loads(obj.ready.read_text())
        self.assertEqual((ready['max_running_requests_requested'], ready['max_running_requests_effective']), (128, 64))
        self.assertFalse(ready['fit_passed'])
        self.assertIn('KV pool', ready['fallback_reason'])
        self.assertEqual((ready['kv_pool_tokens'], ready['warm_graph_bs']), (1_000_000, LADDER_64))

    def test_engine_crash_at_128_falls_back(self):
        obj, launches, effective = self.run_flow(['oom', pool_log(9_000_000, 64)], healthy=(False, True))
        self.assertEqual(effective['MAX_RUNNING_REQUESTS'], '64')
        self.assertIn('did not reach health', obj.record['concurrency']['fallback_reason'])


class BootstrapFitCliTests(unittest.TestCase):
    def cli(self, folder, log, started, *extra, **env):
        Path(folder, 'engine.log').write_text(log)
        e = {**os.environ, 'MAX_RUNNING_REQUESTS': '128', 'CUDA_GRAPH_MAX_BS': '128', 'CONTEXT_LENGTH': '262144', **env}
        r = subprocess.run([sys.executable, str(ROOT/'scripts/fit.py'), str(Path(folder, 'engine.log')), started,
                            str(Path(folder, 'fit.json')), *extra], env=e, capture_output=True, text=True, check=True)
        return r.stdout.strip(), json.loads(Path(folder, 'fit.json').read_text())

    def test_pass_prints_nothing_and_fail_prints_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            out, record = self.cli(folder, pool_log(5_000_000), '1')
            self.assertEqual((out, record['effective']), ('', 128))
            out, record = self.cli(folder, pool_log(1_000_000), '1')
            self.assertEqual(out, '64'); self.assertIn('KV pool', record['fallback_reason'])
            out, record = self.cli(folder, pool_log(1_000_000, 64), '1', '--after-fallback', MAX_RUNNING_REQUESTS='64')
            self.assertEqual((out, record['requested'], record['effective']), ('', 128, 64))
            self.assertIn('fallback_fit', record)
            out, _ = self.cli(folder, 'crash', '0')
            self.assertEqual(out, '64')


class PortContractTests(unittest.TestCase):
    def test_common_sh_relay_ports(self):
        script = 'source scripts/common.sh; printf "%s %s %s" "$PORT" "$ENGINE_PORT" "$ENDPOINT"'
        env = {k: v for k, v in os.environ.items() if k not in ('PORT', 'ENGINE_PORT', 'SSE_RELAY')}
        out = subprocess.check_output(['bash', '-c', script], cwd=ROOT, env=env).decode()
        self.assertEqual(out, '8000 8001 http://127.0.0.1:8000')
        out = subprocess.check_output(['bash', '-c', script], cwd=ROOT, env=dict(env, SSE_RELAY='0')).decode()
        self.assertEqual(out, '8000 8000 http://127.0.0.1:8000')

    def test_tunnel_ingress_unchanged(self):
        self.assertIn('--url http://127.0.0.1:8000', (ROOT/'vendor/infer-gateway/node-tunnel.sh').read_text())


if __name__ == '__main__':
    unittest.main()
