"""Warm deadline accounting and cleanup without a running engine."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import warm
from test_concurrency_fit import selected


class WarmTimeoutTests(unittest.TestCase):
    def test_budget_is_minimum_and_reports_which_bound_expired(self):
        for window, timeout, end, bound in [
                ('10000', None, 2200, 'WARM_TIMEOUT_SECONDS=1200'),
                ('10000', '60', 1060, 'WARM_TIMEOUT_SECONDS=60'),
                ('2000', '1200', 1220, 'WINDOW_END minus 13 minutes')]:
            with self.subTest(window=window, timeout=timeout), tempfile.TemporaryDirectory() as folder, \
                 patch('warm.ROOT', Path(folder)), patch('warm.time.time', return_value=1000) as clock:
                env = {'TUNNEL_SLOT':'a', 'WINDOW_END':window}
                if timeout is not None: env['WARM_TIMEOUT_SECONDS'] = timeout
                obj = warm.Warm(env)
                self.assertEqual(obj.end, end)
                self.assertEqual(obj.record['deadline_epoch'], end)
                self.assertEqual(obj.record['started_epoch'], 1000)
                self.assertEqual(obj.record['warm_timeout_seconds'], int(timeout or '1200'))
                self.assertEqual(obj.remaining(), end-1000)
                clock.return_value = end
                with self.assertRaisesRegex(TimeoutError, bound): obj.remaining()

    def test_invalid_timeouts_fail_before_work(self):
        for value in ('0', '-1', '', '1.5', 'abc', '1_200', ' 1200'):
            with self.subTest(value=value), patch('warm.load_manifest') as load:
                with self.assertRaisesRegex(ValueError, 'WARM_TIMEOUT_SECONDS must be a positive integer'):
                    warm.Warm({'WARM_TIMEOUT_SECONDS':value})
                load.assert_not_called()

    def test_fallback_spends_remaining_budget_without_reset(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT', Path(folder)), \
             patch('warm.time.time', return_value=1000) as clock, redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            obj = warm.Warm({'TUNNEL_SLOT':'a', 'WINDOW_END':'10000', 'WARM_TIMEOUT_SECONDS':'60'})
            ends = []

            def launch(image, config):
                ends.append(obj.end)

            def health():
                clock.return_value += 40

            obj.launch = Mock(side_effect=launch)
            obj.health = Mock(side_effect=health)
            obj.command = Mock(return_value='')
            with self.assertRaisesRegex(TimeoutError, 'WARM_TIMEOUT_SECONDS=60'):
                obj.launch_with_fit_fallback('fixture', selected())
            self.assertEqual(ends, [1060, 1060])
            self.assertEqual(obj.end, 1060)
            self.assertFalse(obj.ready.exists())

    def test_main_timeout_removes_container_relay_and_ready_file(self):
        cases = [
            (TimeoutError('expired'), False, 'timeout'),
            (subprocess.TimeoutExpired('fixture', 1), False, 'timeout'),
            (urllib.error.URLError(TimeoutError('timed out')), True, 'timeout'),
            (urllib.error.URLError(TimeoutError('timed out')), False, 'error'),
            (RuntimeError('engine error'), True, 'error'),
        ]
        for exc, expired, reason in cases:
            with self.subTest(exc=type(exc).__name__, expired=expired), tempfile.TemporaryDirectory() as folder, \
                 patch('warm.ROOT', Path(folder)), patch('warm.time.time', return_value=1000) as clock, \
                 patch.dict(os.environ, {'TUNNEL_SLOT':'a', 'WINDOW_END':'10000'}, clear=True), \
                 patch('warm.subprocess.run') as command:
                relay = Mock()
                relay.poll.return_value = None
                objects = []

                def fail(obj):
                    objects.append(obj)
                    obj.created = True
                    obj.relay = relay
                    obj.ready.write_text('{}')
                    if expired: clock.return_value = obj.end
                    raise exc

                err = io.StringIO()
                with patch.object(warm.Warm, 'run_all', autospec=True, side_effect=fail), redirect_stderr(err):
                    self.assertEqual(warm.main([]), 1)
                obj = objects[0]
                record = json.loads(obj.path.read_text())
                self.assertEqual((record['state'], record['reason']), ('failed', reason))
                self.assertFalse(obj.ready.exists())
                self.assertEqual(command.call_args.args[0], ['docker', 'rm', '-f', obj.name])
                relay.terminate.assert_called_once()
                if reason == 'timeout':
                    self.assertIn('WARM FAILED: timeout; inspect '+str(obj.path), err.getvalue())

    def test_command_uses_remaining_budget(self):
        with tempfile.TemporaryDirectory() as folder, patch('warm.ROOT', Path(folder)), \
             patch('warm.time.time', return_value=1000) as clock:
            obj = warm.Warm({'TUNNEL_SLOT':'a', 'WINDOW_END':'10000', 'WARM_TIMEOUT_SECONDS':'60'})
            clock.return_value = 1050
            with patch('warm.subprocess.run', side_effect=subprocess.TimeoutExpired('fixture', 10)) as command:
                with self.assertRaises(subprocess.TimeoutExpired): obj.command(['fixture'])
            self.assertEqual(command.call_args.kwargs['timeout'], 10)


class BootstrapTimeoutTests(unittest.TestCase):
    def test_health_budget_is_created_once_before_first_launch(self):
        script = (ROOT/'bootstrap.sh').read_text()
        self.assertIn('${HEALTH_TIMEOUT_SECONDS:-1200}', script)
        self.assertEqual(len(re.findall(r'^\s*health_end=', script, re.M)), 1)
        launch = script.split('launch_with_fit_fallback() {', 1)[1].split('\n}', 1)[0]
        self.assertLess(launch.index('health_end='), launch.index('\n    launch_engine'))
        wait = script.split('wait_health() {', 1)[1].split('\n}', 1)[0]
        self.assertNotIn('health_end=', wait)

    def test_deadline_calls_failed_before_any_ready_signal(self):
        script = (ROOT/'bootstrap.sh').read_text()
        check = script.split('check_health_deadline() {', 1)[1].split('\n}', 1)[0]
        failed = script.split('failed() {', 1)[1].split('\n}', 1)[0]
        with tempfile.TemporaryDirectory() as folder:
            teardown = Path(folder, 'teardown.sh')
            teardown.write_text('#!/bin/bash\necho teardown\n')
            teardown.chmod(0o755)
            # The window timer calls failed via SIGTERM; health expiry calls it directly.
            for call, window in [('check_health_deadline', '99999999999'), ('failed', '1')]:
                with self.subTest(call=call):
                    Path(folder, 'failure.json').unlink(missing_ok=True)
                    harness = ('set -eu\nlog() { :; }\nflock() { :; }\njobs_to_stop=()\n'
                               'status() { echo "$2" > "$RUN_DIR/status.txt"; }\n'
                               'failed() {'+failed+'\n}\ncheck_health_deadline() {'+check+'\n}\n'
                               'health_end=0\n'+call+'\necho READY\n')
                    result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True,
                                            env={'PATH':os.defpath, 'RUN_DIR':folder, 'KIT_ROOT':folder,
                                                 'WINDOW_END':window})
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout.strip(), 'teardown')
                    self.assertEqual(Path(folder, 'status.txt').read_text().strip(), 'error')
                    self.assertTrue(Path(folder, 'stop').exists())
                    self.assertEqual(json.loads(Path(folder, 'failure.json').read_text())['reason'], 'timeout')
