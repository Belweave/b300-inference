#!/usr/bin/env python3
"""Off-node kill/drain handoff stub. Prints a plan or rehearses with fakes only.

There is no live adapter, credential loading, command execution, or network I/O.
This is deliberately not connected to boot.sh, node_run, or the claim watcher.
"""
import argparse
import json
import re

GATEWAY_SCOPE = 'hackathon-*'


class StubFailure(Exception):
    pass


def plan(window_id, node_id, drain_seconds=120):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', window_id):
        raise ValueError('window-id must be 1-64 letters, digits, underscores or hyphens')
    if not re.fullmatch(r'[0-9a-f]{8,64}', node_id):
        raise ValueError('node-id must be the 8-64 lowercase hex ID from status.sh --json')
    if type(drain_seconds) is not int or not 1 <= drain_seconds <= 120:
        raise ValueError('drain-seconds must be an integer from 1 to 120')
    return {
        'mode': 'plan', 'window_id': window_id, 'node_id': node_id,
        'drain_seconds': drain_seconds,
        'steps': ['revoke_window', 'close_admission', 'drain', 'stop_engine', 'stop_tunnel'],
        'gateway_command': ['keys.sh', 'kill-window'],
        'gateway_scope': GATEWAY_SCOPE,
        'gateway_ack_required': {'window_id': window_id, 'scope': GATEWAY_SCOPE,
                                 'window_closed': True, 'revoked': True},
        'drain_rule': 'running AND queued zero for 3 consecutive samples; unknown is not zero',
        'at_deadline': 'force stop engine, then tunnel; report drain_timeout',
        'live_execution_supported': False,
    }


def idle(sample):
    """Missing, invalid, negative or non-finite gauges cannot establish a drain."""
    if not isinstance(sample, dict):
        return False
    values = [sample.get('running'), sample.get('queued')]
    return all(type(v) in (int, float) and v == 0 for v in values)


def kill_and_drain(window_id, node_id, actions, *, clock, sleep, drain_seconds=120):
    """Exercise the future adapter's ordering with injected actions and time.

    Only StubActions is wired by the CLI. A future live adapter must implement
    event-key revocation and verified node-local admission closure. window_id is
    a local correlation ID: keys.sh kill-window affects ALL hackathon-* keys.
    """
    result = plan(window_id, node_id, drain_seconds)
    ack = actions.revoke_window(window_id)
    if (not isinstance(ack, dict) or ack.get('window_id') != window_id
            or ack.get('scope') != GATEWAY_SCOPE or ack.get('window_closed') is not True
            or ack.get('revoked') is not True):
        raise StubFailure('gateway revocation was not acknowledged for this window')
    if actions.close_admission(node_id) is not True:
        raise StubFailure('node admission closure was not acknowledged')

    end = clock() + drain_seconds
    zeros = 0
    while clock() < end:
        try:
            sample = actions.metrics(node_id)
        except Exception:
            sample = None
        zeros = zeros + 1 if idle(sample) else 0
        if zeros >= 3 and clock() < end:
            break
        sleep(min(2, max(0, end - clock())))
    drained = zeros >= 3 and clock() < end
    # Withdrawal is still attempted if engine shutdown fails. Never re-enable
    # gateway admission or erase registration as part of this one-way handoff.
    try:
        actions.stop_engine(node_id)
    finally:
        actions.stop_tunnel(node_id)
    result.update(mode='rehearsal', outcome='drained' if drained else 'drain_timeout',
                  forced=not drained)
    return result


class StubActions:
    """In-memory actions and virtual time. No real clock waits or side effects."""
    def __init__(self, scenario):
        self.scenario = scenario
        self.events = []
        self.seconds = 0

    def clock(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds

    def revoke_window(self, window_id):
        self.events.append('revoke_window')
        return {'window_id': window_id, 'scope': GATEWAY_SCOPE, 'window_closed': True,
                'revoked': self.scenario != 'revoke-failed'}

    def close_admission(self, node_id):
        self.events.append('close_admission')
        return True

    def metrics(self, node_id):
        self.events.append('metrics')
        if self.scenario == 'unknown':
            return None
        return {'running': 0, 'queued': 1 if self.scenario == 'busy' else 0}

    def stop_engine(self, node_id):
        self.events.append('stop_engine')

    def stop_tunnel(self, node_id):
        self.events.append('stop_tunnel')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0], allow_abbrev=False)
    parser.add_argument('--window-id', required=True)
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--drain-seconds', type=int, default=120)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--dry-run', action='store_true', help='print the plan (default)')
    modes.add_argument('--rehearse', choices=('drained', 'busy', 'unknown', 'revoke-failed'),
                       help='run ONLY in-memory fakes and a virtual clock')
    args = parser.parse_args(argv)
    try:
        result = plan(args.window_id, args.node_id, args.drain_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    code = 0
    if args.rehearse:
        stub = StubActions(args.rehearse)
        try:
            result = kill_and_drain(args.window_id, args.node_id, stub,
                                    clock=stub.clock, sleep=stub.sleep, drain_seconds=args.drain_seconds)
            code = 0 if result['outcome'] == 'drained' else 2
        except StubFailure as exc:
            result.update(mode='rehearsal', outcome='blocked', error=str(exc))
            code = 1
        result.update(mock_events=stub.events, simulated_seconds=stub.seconds)
    print(json.dumps(result, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
