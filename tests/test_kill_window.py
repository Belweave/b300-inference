"""Off-node kill/drain stub: mocked actions, virtual time, no GPU or transport."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, call

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import kill_window

WINDOW = 'saturday-fixture'
NODE = 'a1b2c3d4'


class KillWindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = kill_window.StubActions('drained')
        self.actions = Mock()
        self.ack = {'window_id': WINDOW, 'scope': 'hackathon-*', 'window_closed': True, 'revoked': True}
        self.actions.revoke_window.return_value = self.ack
        self.actions.close_admission.return_value = True
        self.actions.metrics.return_value = {'running': 0, 'queued': 0}

    def run_stub(self, **options):
        return kill_window.kill_and_drain(WINDOW, NODE, self.actions,
                                         clock=self.clock.clock, sleep=self.clock.sleep, **options)

    def test_revoke_ack_precedes_admission_drain_engine_and_tunnel(self):
        self.assertEqual(self.run_stub()['outcome'], 'drained')
        self.assertEqual(self.actions.mock_calls, [call.revoke_window(WINDOW), call.close_admission(NODE),
                         call.metrics(NODE), call.metrics(NODE), call.metrics(NODE),
                         call.stop_engine(NODE), call.stop_tunnel(NODE)])
        self.assertEqual(self.clock.seconds, 4)

    def test_wrong_window_or_unconfirmed_revocation_never_touches_node(self):
        for ack in (None, {}, {**self.ack, 'window_id': 'other'},
                    {**self.ack, 'revoked': False}, {**self.ack, 'revoked': 1},
                    {**self.ack, 'scope': '*'}, {**self.ack, 'window_closed': False},
                    {**self.ack, 'window_closed': 1}):
            with self.subTest(ack=ack):
                self.actions.reset_mock()
                self.actions.revoke_window.return_value = ack
                with self.assertRaises(kill_window.StubFailure):
                    self.run_stub()
                self.assertEqual(self.actions.mock_calls, [call.revoke_window(WINDOW)])

    def test_revocation_exception_never_touches_node(self):
        self.actions.revoke_window.side_effect = RuntimeError('mock adapter failed')
        with self.assertRaises(RuntimeError):
            self.run_stub()
        self.assertEqual(self.actions.mock_calls, [call.revoke_window(WINDOW)])

    def test_admission_failure_does_not_claim_drain_or_undo_revocation(self):
        self.actions.close_admission.return_value = False
        with self.assertRaises(kill_window.StubFailure):
            self.run_stub()
        self.assertEqual(self.actions.mock_calls, [call.revoke_window(WINDOW), call.close_admission(NODE)])

    def test_queued_work_and_missing_metrics_reset_zero_streak(self):
        self.actions.metrics.side_effect = [
            {'running': 0, 'queued': 0}, {'running': 0, 'queued': 1},
            {'running': 0, 'queued': 0}, None,
            *[{'running': 0, 'queued': 0}] * 3,
        ]
        self.assertEqual(self.run_stub()['outcome'], 'drained')
        self.assertEqual(self.actions.metrics.call_count, 7)
        self.assertEqual(self.clock.seconds, 12)

    def test_missing_invalid_nonfinite_and_negative_gauges_are_not_idle(self):
        for sample in (None, [], {}, {'running': 0}, {'queued': 0},
                       *({'running': 0, 'queued': v} for v in (None, False, '0', -1, 1, 10**1000,
                                                             float('inf'), float('nan'))),
                       {'running': -1, 'queued': 1}, {'running': 1, 'queued': 0}):
            with self.subTest(sample=sample):
                self.assertFalse(kill_window.idle(sample))
        self.assertTrue(kill_window.idle({'running': 0.0, 'queued': 0}))

    def test_busy_drain_stops_at_exact_bound_and_reports_forced_stop(self):
        self.actions.metrics.return_value = {'running': 0, 'queued': 1}
        result = self.run_stub(drain_seconds=5)
        self.assertEqual(result['outcome'], 'drain_timeout')
        self.assertTrue(result['forced'])
        self.assertEqual(self.clock.seconds, 5)
        self.assertEqual(self.actions.metrics.call_count, 3)
        self.assertEqual(self.actions.mock_calls[-2:], [call.stop_engine(NODE), call.stop_tunnel(NODE)])

    def test_metrics_exceptions_never_mean_drained(self):
        self.actions.metrics.side_effect = OSError('fixture metrics unavailable')
        self.assertEqual(self.run_stub(drain_seconds=5)['outcome'], 'drain_timeout')
        self.assertEqual(self.clock.seconds, 5)

    def test_tunnel_withdrawal_is_attempted_even_when_engine_stop_fails(self):
        self.actions.stop_engine.side_effect = RuntimeError('fixture stop failed')
        with self.assertRaises(RuntimeError):
            self.run_stub()
        self.actions.stop_tunnel.assert_called_once_with(NODE)

    def test_invalid_scope_and_bounds_are_rejected_before_actions(self):
        for window, node, bound in (('../other', NODE, 120), ('-all', NODE, 120),
                                    (WINDOW, 'hostname.invalid', 120), (WINDOW, NODE, 0),
                                    (WINDOW, NODE, 121), (WINDOW, NODE, True)):
            with self.subTest(window=window, node=node, bound=bound), self.assertRaises(ValueError):
                kill_window.kill_and_drain(window, node, self.actions, clock=self.clock.clock,
                                          sleep=self.clock.sleep, drain_seconds=bound)
        self.actions.assert_not_called()
        self.assertEqual(self.actions.mock_calls, [])

    def cli(self, *args):
        # Even hostile/live-looking environment switches cannot enable an adapter.
        env = {'PATH': os.environ['PATH'], 'DRY_RUN': '0', 'EXECUTE': '1',
               'DRAIN_HOOK': '/must-not-run', 'RELAY_SH': '/must-not-run',
               'INFER_UPSTREAM_KEY': 'fixture-not-a-secret', 'ADMIN': 'fixture-not-a-secret'}
        return subprocess.run(['bash', str(ROOT/'scripts/kill-window.sh'), '--window-id', WINDOW,
                               '--node-id', NODE, *args], env=env, capture_output=True, text=True, timeout=10)

    def test_cli_defaults_to_plan_ignores_live_environment_and_has_no_execute_mode(self):
        for args in ((), ('--dry-run',)):
            proc = self.cli(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result['mode'], 'plan')
            self.assertFalse(result['live_execution_supported'])
            self.assertEqual(result['gateway_command'], ['keys.sh', 'kill-window'])
            self.assertEqual(result['gateway_scope'], 'hackathon-*')
            self.assertNotIn('mock_events', result)
            self.assertNotIn('fixture-not-a-secret', proc.stdout + proc.stderr)
        self.assertEqual(self.cli('--execute').returncode, 2)

    def test_cli_mock_scenarios_have_explicit_outcomes_and_exit_codes(self):
        for scenario, code, outcome in (('drained', 0, 'drained'), ('busy', 2, 'drain_timeout'),
                                        ('unknown', 2, 'drain_timeout'), ('revoke-failed', 1, 'blocked')):
            with self.subTest(scenario=scenario):
                proc = self.cli('--rehearse', scenario)
                self.assertEqual(proc.returncode, code, proc.stderr)
                result = json.loads(proc.stdout)
                self.assertEqual(result['outcome'], outcome)
                self.assertEqual(result['mode'], 'rehearsal')
                self.assertFalse(result['live_execution_supported'])
                if scenario == 'revoke-failed':
                    self.assertEqual(result['mock_events'], ['revoke_window'])


if __name__ == '__main__':
    unittest.main()
