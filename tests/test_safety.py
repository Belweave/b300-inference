"""Offline safety tests: synthetic verdicts and inert placeholders only."""
import asyncio
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_sse_relay as relay_tests
try:
    from aiohttp import ClientError
    from aiohttp.test_utils import TestClient, TestServer
    import safety
    import sse_relay
except ImportError:
    safety = None

MARKER = 'TEST_TRIGGER_CSAM_PLACEHOLDER'


class FakeClassifier:
    def __init__(self, config, healthy=True, fail=False):
        self.config, self.ok, self.fail = config, healthy, fail
        self.calls = []

    async def healthy(self):
        return self.ok

    async def score(self, texts):
        self.calls.extend(texts)
        if self.fail:
            raise safety.ClassifierUnavailable
        return [0.9 if MARKER in text else 0.01 for text in texts]


def logprobs(*entries):
    return {'choices': [{'logprobs': {'content': [{'top_logprobs': [
        {'token': token, 'logprob': value} for token, value in entries]}]}}]}


def sse(delta, index=0, ending=b'\n\n'):
    return b'data: ' + json.dumps({'choices': [{'index': index, 'delta': delta}]}).encode() + ending


@unittest.skipIf(safety is None, 'aiohttp not installed')
class ConfigAndTextTests(unittest.TestCase):
    def test_config_defaults_and_overrides(self):
        config = safety.SafetyConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.url, 'http://127.0.0.1:8600')
        self.assertEqual((config.threshold, config.chunk_tokens, config.window_chars, config.overlap_chars),
                         (0.5, 64, 16000, 1000))
        self.assertEqual((config.max_inflight, config.timeout_seconds), (64, 5))
        self.assertEqual(config.policy_version, 'sha256:' + hashlib.sha256(safety.POLICY.encode()).hexdigest()[:16])
        config = safety.SafetyConfig.from_env({'SAFETY': '1', 'SAFETY_URL': 'http://localhost:8601',
                                             'SAFETY_MODEL': 'test', 'SAFETY_THRESHOLD': '0.7',
                                             'SAFETY_CHUNK_TOKENS': '3', 'SAFETY_WINDOW_CHARS': '40',
                                             'SAFETY_OVERLAP_CHARS': '0', 'SAFETY_TIMEOUT_SECONDS': '0.2',
                                             'SAFETY_MAX_INFLIGHT': '2', 'SAFETY_SPOOL_DIR': '/tmp/test',
                                             'SAFETY_AGE_RECIPIENTS_FILE': '/tmp/public.txt',
                                             'SAFETY_AGE_BIN': '/tmp/age',
                                             'SAFETY_INCIDENT_WEBHOOK': 'https://gateway.test/incidents'})
        self.assertTrue(config.enabled)
        self.assertEqual((config.threshold, config.chunk_tokens, config.overlap_chars), (0.7, 3, 0))

    def test_config_rejects_bad_values(self):
        bad = {'SAFETY': ['true', '', '2'],
               'SAFETY_URL': ['http://example.test', 'http://127.0.0.2', 'http://127.0.0.1.example.test',
                              'file:///tmp/test', 'http://localhost:0', 'http://localhost:bad',
                              'http://user@localhost', 'http://localhost/?q=1', 'http://[bad'],
               'SAFETY_THRESHOLD': ['0', '1', '-1', 'nan', 'inf', 'bad'],
               'SAFETY_CHUNK_TOKENS': ['0', '-1', '1.5'], 'SAFETY_WINDOW_CHARS': ['0', '1000'],
               'SAFETY_OVERLAP_CHARS': ['-1', '16000'], 'SAFETY_MAX_INFLIGHT': ['0'],
               'SAFETY_TIMEOUT_SECONDS': ['0', '-1', 'inf', 'nan'], 'SAFETY_MODEL': [''],
               'SAFETY_AGE_BIN': [''], 'SAFETY_SPOOL_DIR': [''],
               'SAFETY_INCIDENT_WEBHOOK': ['file:///tmp/test']}
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    safety.SafetyConfig.from_env({name: value})

    def test_request_texts(self):
        body = {'messages': [{'role': 'user', 'content': 'hello world'},
                             {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hello'},
                                                             {'type': 'image_url', 'image_url': 'INERT_BINARY'}],
                              'reasoning_content': 'reasoning placeholder',
                              'tool_calls': [{'function': {'arguments': 'tool placeholder'}}]}]}
        texts = safety.request_texts('/v1/chat/completions', json.dumps(body))
        self.assertEqual(texts[0], 'user: hello world')
        self.assertEqual(texts[1], 'assistant: hello\n[non-text content part]\nreasoning placeholder\ntool placeholder')
        self.assertNotIn('INERT_BINARY', ''.join(texts))
        body['tools'] = [{'function': {'description': 'tool definition placeholder'}}]
        body['reasoning'] = 'request reasoning placeholder'
        texts = safety.request_texts('/v1/chat/completions', json.dumps(body))
        self.assertIn('tools: tool definition placeholder', texts)
        self.assertIn('reasoning: request reasoning placeholder', texts)
        for prompt, expected in [('hello world', 'hello world'), (['hello', 'world'], 'hello\nworld')]:
            self.assertEqual(safety.request_texts('/v1/completions', json.dumps({'prompt': prompt})), [expected])
        for path, body in [('/health', '{}'), ('/v1/completions', 'bad json'),
                           ('/v1/chat/completions', '[]'), ('/v1/chat/completions', '{"messages": null}'),
                           ('/v1/chat/completions', b'\xff')]:
            self.assertEqual(safety.request_texts(path, body), [])

    def test_score_math_and_token_variants(self):
        self.assertAlmostEqual(safety.parse_score(logprobs(('yes', -2), ('no', -3))), 1 / (1 + math.exp(-1)))
        self.assertAlmostEqual(safety.parse_score(logprobs(('yes', -1002), ('no', -1003))), 1 / (1 + math.exp(-1)))
        for yes in ('yes', ' YES ', 'yes.', '"yes"', "'yes'"):
            for no in ('no', ' NO ', 'no.', '"no"', "'no'"):
                self.assertEqual(safety.parse_score(logprobs((yes, -1), (no, -1))), 0.5)
        self.assertEqual(safety.parse_score(logprobs(('yes', -5), ('yes.', -1), ('no', -1))), 0.5)
        self.assertAlmostEqual(safety.parse_score(logprobs(('yes', -1))), 1 / (1 + math.exp(-9)))
        self.assertAlmostEqual(safety.parse_score(logprobs(('no', -1))), 1 / (1 + math.exp(9)))
        for payload in ({}, None, logprobs(('other', -1)), logprobs(('yes', float('nan'))),
                        logprobs(('yes', 'bad')), logprobs((None, -1))):
            with self.assertRaises(safety.ClassifierUnavailable):
                safety.parse_score(payload)


@unittest.skipIf(safety is None, 'aiohttp not installed')
class GateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = replace(safety.SafetyConfig(), enabled=True)
        self.classifier = FakeClassifier(self.config)

    async def test_input_pass_hit_and_inclusive_threshold(self):
        result = await safety.check_input(self.classifier, ['hello world'])
        self.assertFalse(result.blocked)
        result = await safety.check_input(self.classifier, ['hello', MARKER])
        self.assertEqual((result.hit, result.score, result.stage, result.window, result.reason),
                         (True, 0.9, 'input', 1, 'hit'))
        self.classifier.config = replace(self.config, threshold=0.9)
        self.assertTrue((await safety.check_input(self.classifier, [MARKER])).hit)

    async def test_overlapping_windows(self):
        self.classifier.config = replace(self.config, window_chars=64, overlap_chars=32)
        text = 'a' * 50 + MARKER + 'b' * 80
        result = await safety.check_input(self.classifier, [text])
        self.assertTrue(result.hit)
        self.assertEqual(self.classifier.calls, list(safety.windows(text, self.classifier.config)))
        self.assertTrue(all(len(window) <= 64 for window in self.classifier.calls))
        self.assertEqual(self.classifier.calls[0][-32:], self.classifier.calls[1][:32])
        self.assertEqual(list(safety.windows('abcd', replace(self.config, window_chars=4, overlap_chars=0))), ['abcd'])

    async def test_output_fields_chunks_overlap_and_final_partial(self):
        for field in ('content', 'reasoning', 'reasoning_content', 'tool_calls', 'function_call'):
            with self.subTest(field=field):
                config = replace(self.config, chunk_tokens=4)
                scanner = safety.OutputScanner(self.classifier, config, 'hello world')
                for text in (MARKER[:-3], MARKER[-3:]):
                    value = [{'function': {'arguments': text}}] if field == 'tool_calls' else text
                    result = await scanner.feed({field: value})
                self.assertIsNone(result)
                result = await scanner.flush()
                self.assertEqual((result.hit, result.stage), (True, 'output'))
        scanner = safety.OutputScanner(self.classifier, replace(self.config, chunk_tokens=1), 'hello world')
        self.assertTrue((await scanner.feed({'content': MARKER})).hit)
        scanner = safety.OutputScanner(self.classifier, self.config, 'c' * 2100)
        self.assertIsNone(await scanner.feed({'content': 'hello world'}))
        self.assertIsNone(await scanner.flush())
        self.assertEqual(scanner.prompt_context, 'c' * 2000)
        self.assertIn('Assistant output (judge only this portion):\nhello world', self.classifier.calls[-1])

    async def test_fail_closed_on_unhealthy_or_unavailable(self):
        for classifier in (FakeClassifier(self.config, healthy=False), FakeClassifier(self.config, fail=True)):
            result = await safety.check_input(classifier, ['hello world'])
            self.assertEqual((result.blocked, result.hit, result.reason), (True, False, 'classifier_unavailable'))
            scanner = safety.OutputScanner(classifier, replace(self.config, chunk_tokens=1), '')
            result = await scanner.feed({'content': 'hello world'})
            self.assertEqual((result.blocked, result.hit, result.reason, result.stage),
                             (True, False, 'classifier_unavailable', 'output'))
        result = await safety.check_input(FakeClassifier(self.config, healthy=False), [])
        self.assertTrue(result.blocked)

    async def test_parallel_tool_arguments_keep_their_continuity(self):
        scanner = safety.OutputScanner(self.classifier, replace(self.config, chunk_tokens=1), 'hello world')
        self.assertIsNone(await scanner.feed({'tool_calls': [
            {'index': 0, 'function': {'arguments': MARKER[:10]}},
            {'index': 1, 'function': {'arguments': 'hello world'}}]}))
        result = await scanner.feed({'tool_calls': [
            {'index': 0, 'function': {'arguments': MARKER[10:]}}]})
        self.assertEqual((result.hit, result.stage), (True, 'output'))

    async def test_invalid_scores_fail_closed(self):
        for scores in ([], [float('nan')], [2], ['bad']):
            self.classifier.score = AsyncMock(return_value=scores)
            result = await safety.check_input(self.classifier, ['hello world'])
            self.assertEqual(result.reason, 'classifier_unavailable')


class FakeResponse:
    def __init__(self, payload=None, status=200, error=None):
        self.payload, self.status, self.error = payload, status, error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@unittest.skipIf(safety is None, 'aiohttp not installed')
class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_payload_health_cache_and_failures_without_network(self):
        config = safety.SafetyConfig()
        session = Mock()
        session.post.return_value = FakeResponse(logprobs(('yes', -2), ('no', -1)))
        session.get.return_value = FakeResponse()
        classifier = safety.ShieldstralClassifier(config, session)
        self.assertTrue(await classifier.healthy())
        self.assertTrue(await classifier.healthy())
        self.assertEqual(session.get.call_count, 1)
        scores = await classifier.score(['hello', 'world'])
        self.assertEqual(len(scores), 2)
        url = session.post.call_args.args[0]
        args = session.post.call_args.kwargs
        self.assertEqual(url, config.url + '/v1/chat/completions')
        self.assertNotIn('headers', args)
        self.assertFalse(args['allow_redirects'])
        self.assertEqual(args['json'], {'model': config.model, 'messages': [
            {'role': 'system', 'content': safety.SYSTEM},
            {'role': 'user', 'content': safety.USER_TEMPLATE.format(instruct=safety.INSTRUCT,
                                                                  query=safety.QUERY, text='world')}],
            'max_tokens': 1, 'temperature': 0, 'logprobs': True, 'top_logprobs': 20})
        for response in (FakeResponse(status=503), FakeResponse(ValueError('bad json')),
                         FakeResponse({}), FakeResponse(error=ClientError()),
                         FakeResponse(error=asyncio.TimeoutError())):
            session.post.return_value = response
            with self.assertRaises(safety.ClassifierUnavailable):
                await classifier.score(['hello world'])
        classifier.health_until = 0
        session.get.return_value = FakeResponse(status=503)
        self.assertFalse(await classifier.healthy())
        classifier.health_until = 0
        session.get.return_value = FakeResponse(error=ClientError())
        self.assertFalse(await classifier.healthy())

    async def test_shared_semaphore_and_timeout(self):
        config = replace(safety.SafetyConfig(), max_inflight=2, timeout_seconds=0.05)
        session = Mock()
        active, peak = 0, 0

        class DelayedResponse(FakeResponse):
            async def __aenter__(self):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                return self

            async def json(self):
                await asyncio.sleep(0.005)
                return logprobs(('no', -1))

            async def __aexit__(self, *args):
                nonlocal active
                active -= 1

        session.post.side_effect = lambda *args, **kwargs: DelayedResponse()
        first, second = safety.ShieldstralClassifier(config, session), safety.ShieldstralClassifier(config, session)
        self.assertIs(first.inflight, second.inflight)
        await asyncio.gather(first.score(['hello'] * 4), second.score(['world'] * 4))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        await first.inflight.acquire()
        await first.inflight.acquire()
        with self.assertRaises(safety.ClassifierUnavailable):
            await first.score(['hello'])
        first.inflight.release()
        first.inflight.release()


@unittest.skipIf(safety is None, 'aiohttp not installed')
class IncidentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.recipients = root / 'public.txt'
        self.recipients.write_text('PUBLIC_RECIPIENT_PLACEHOLDER\n')
        # Test double only: its stdin contains exclusively inert test evidence.
        self.age = root / 'fake-age'
        self.age.write_text('#!/bin/sh\n[ "$1" = "-R" ] && [ "$3" = "-o" ] || exit 1\ncat > "$4"\n')
        self.age.chmod(0o700)
        self.config = replace(safety.SafetyConfig(), spool_dir=str(root / 'spool'),
                              age_recipients_file=str(self.recipients), age_bin=str(self.age))
        self.incident = {'stage': 'input', 'score': 0.9, 'reason': 'hit', 'request_path': '/v1/chat/completions',
                         'key_fingerprint': 'hashed-placeholder', 'request_id': 'request-placeholder',
                         'texts': [MARKER], 'generated_output': ['hello world']}

    async def test_seal_permissions_metadata_and_write_once(self):
        with self.assertLogs('safety', level='INFO') as logs:
            ident = await safety.seal_incident(self.config, self.incident)
        self.assertRegex(ident, r'^inc_\d{8}T\d{6}Z_[0-9a-f]{8}$')
        self.assertFalse(self.incident['sealing_failed'])
        target = Path(self.config.spool_dir) / (ident + '.age')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        original = target.read_bytes()
        record = json.loads(original)
        self.assertEqual(record['incident_id'], ident)
        self.assertTrue(record['created_at'].endswith('Z'))
        self.assertEqual(record['threshold'], 0.5)
        self.assertEqual(record['policy_version'], self.config.policy_version)
        self.assertEqual(record['model'], self.config.model)
        self.assertEqual(record['texts'], [MARKER])
        self.assertNotIn(MARKER, ''.join(logs.output))
        self.assertNotIn('hashed-placeholder', ''.join(logs.output))
        second = {**self.incident, 'texts': ['hello world']}
        with patch.object(safety, 'incident_id', return_value=ident), self.assertLogs('safety', level='WARNING'):
            self.assertEqual(await safety.seal_incident(self.config, second), ident)
        self.assertTrue(second['sealing_failed'])
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(Path(self.config.spool_dir).iterdir()), [target])

    async def test_missing_empty_recipients_and_failed_age_leave_no_evidence(self):
        for recipients in (None, '', '# public recipients placeholder\n'):
            if recipients is None:
                self.recipients.unlink()
            else:
                self.recipients.write_text(recipients)
            with self.assertLogs('safety', level='WARNING'):
                await safety.seal_incident(self.config, self.incident)
            self.assertTrue(self.incident['sealing_failed'])
            self.assertFalse(Path(self.config.spool_dir).exists())
        self.recipients.write_text('PUBLIC_RECIPIENT_PLACEHOLDER')
        self.age.write_text('#!/bin/sh\nexit 1\n')
        with self.assertLogs('safety', level='WARNING'):
            await safety.seal_incident(self.config, self.incident)
        self.assertTrue(self.incident['sealing_failed'])
        self.assertEqual(list(Path(self.config.spool_dir).iterdir()), [])

    async def test_webhook_has_only_incident_id(self):
        session = Mock()
        session.post.return_value = FakeResponse()
        config = replace(self.config, incident_webhook='http://127.0.0.1:8602/incidents')
        await safety.notify_incident(config, session, 'inc_placeholder')
        self.assertEqual(session.post.call_args.kwargs['json'], {'incident_id': 'inc_placeholder'})
        self.assertNotIn('headers', session.post.call_args.kwargs)


@unittest.skipIf(safety is None, 'aiohttp not installed')
class SafetyRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen, self.cancelled = [], asyncio.Event()
        self.config = replace(safety.SafetyConfig(), enabled=True, chunk_tokens=1)
        self.classifier = FakeClassifier(self.config)
        self.seal = AsyncMock(return_value='inc_placeholder')
        self.notify = AsyncMock()
        for name, mock in (('seal_incident', self.seal), ('notify_incident', self.notify)):
            patcher = patch.object(safety, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def start(self, reply='hi', deltas=None, enabled=True):
        engine = TestServer(relay_tests.fake_engine(self.seen, self.cancelled, reply, deltas))
        await engine.start_server()
        self.addAsyncCleanup(engine.close)
        config = self.config if enabled else None
        with patch.dict(os.environ, {'SAFETY': '1' if enabled else '0'}):
            app = sse_relay.make_app(str(engine.make_url('/')), relay_tests.HB, config, self.classifier)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, stream=False, text='hello world', path='/v1/chat/completions', **extra):
        payload = {'stream': stream, 'messages': [{'role': 'user', 'content': text}], 'prompt': text, **extra}
        async with self.client.post(path, json=payload, headers={'Authorization': 'Bearer test-only'}) as response:
            return response.status, await response.read()

    def assert_block(self, status, data, stream, code):
        self.assertEqual(status, 200 if stream else 403)
        if stream:
            self.assertTrue(data.endswith(b'data: [DONE]\n\n'))
            events = [line[5:] for line in data.splitlines() if line.startswith(b'data:') and b'[DONE]' not in line]
            error = json.loads(events[-1])['error']
        else:
            error = json.loads(data)['error']
        self.assertEqual(error, {'message': 'blocked by safety policy', 'type': 'safety_block',
                                 'code': code, 'incident_id': 'inc_placeholder'})

    async def test_input_blocks_without_contacting_engine(self):
        await self.start()
        for path in ('/v1/chat/completions', '/v1/completions'):
            for stream in (False, True):
                status, data = await self.post(stream, MARKER, path)
                self.assert_block(status, data, stream, 'content_blocked')
        self.assertEqual(self.seen, [])
        incident = self.seal.call_args.args[1]
        self.assertEqual(incident['stage'], 'input')
        self.assertNotEqual(incident['key_fingerprint'], 'Bearer test-only')
        self.assertEqual(len(incident['key_fingerprint']), 16)
        self.assertEqual(self.notify.await_count, 4)

    async def test_unavailable_blocks_and_sealing_failure_still_notifies(self):
        await self.start()
        for healthy, fail in ((False, False), (True, True)):
            self.classifier.ok, self.classifier.fail = healthy, fail
            for stream in (False, True):
                status, data = await self.post(stream)
                self.assert_block(status, data, stream, 'safety_unavailable')
                self.assertEqual(self.seal.call_args.args[1]['reason'], 'classifier_unavailable')
        self.assertEqual(self.seen, [])
        with tempfile.TemporaryDirectory() as tmp:
            self.config = replace(self.config, age_recipients_file=tmp + '/missing', spool_dir=tmp + '/spool')
            self.classifier.config = self.config
            # Exercise the real missing-recipient path with a separate enabled relay.
            await self.start()
            with patch.object(safety, 'seal_incident', _REAL_SEAL), self.assertLogs('safety', level='WARNING'):
                status, data = await self.post()
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(data)['error']['code'], 'safety_unavailable')
            self.assertFalse(Path(self.config.spool_dir).exists())
            self.assertRegex(self.notify.call_args.args[-1], r'^inc_')

    async def test_nonstream_output_hit_and_safe_passthrough(self):
        await self.start(reply=MARKER)
        for path in ('/v1/chat/completions', '/v1/completions'):
            status, data = await self.post(path=path)
            self.assert_block(status, data, False, 'content_blocked')
            self.assertEqual(self.seal.call_args.args[1]['stage'], 'output')
        await self.start(reply='hello world')
        status, data = await self.post()
        self.assertEqual((status, json.loads(data)['choices'][0]['message']['content']), (200, 'hello world'))
        self.assertEqual(self.seen[-1][0]['Authorization'], 'Bearer test-only')

    async def test_stream_output_hit_aborts_upstream_and_checks_reasoning_and_tools(self):
        for delta in ({'content': MARKER}, {'reasoning': MARKER}, {'reasoning_content': MARKER},
                      {'tool_calls': [{'function': {'arguments': MARKER}}]}):
            self.cancelled = asyncio.Event()
            raw = sse(delta, ending=b'\r\n\r\n')
            await self.start(deltas=[raw[:9], raw[9:]])
            status, data = await self.post(stream=True, mode='forever')
            self.assert_block(status, data, True, 'content_blocked')
            self.assertNotIn(MARKER.encode(), data)
            self.assertEqual(self.seal.call_args.args[1]['generated_output'], [MARKER])
            await asyncio.wait_for(self.cancelled.wait(), 2)

    async def test_stream_final_partial_and_completions_text(self):
        self.config = replace(self.config, chunk_tokens=64)
        self.classifier.config = self.config
        for raw, path in [(sse({'content': MARKER}), '/v1/chat/completions'),
                          (b'data: ' + json.dumps({'choices': [{'text': MARKER}]}).encode() + b'\n\n', '/v1/completions')]:
            await self.start(deltas=[raw])
            status, data = await self.post(stream=True, path=path)
            self.assert_block(status, data, True, 'content_blocked')
            self.assertEqual(data.count(b'[DONE]'), 1)

    async def test_stream_outage_and_independent_choices(self):
        original = self.classifier.score

        async def score(texts):
            if any('Assistant output' in text for text in texts):
                raise safety.ClassifierUnavailable
            return await original(texts)
        self.classifier.score = score
        await self.start(deltas=[sse({'content': 'hello world'})])
        status, data = await self.post(stream=True)
        self.assert_block(status, data, True, 'safety_unavailable')
        self.classifier.score = original
        await self.start(deltas=[sse({'content': MARKER[:10]}, 0), sse({'content': 'hello'}, 1),
                                 sse({'content': MARKER[10:]}, 0)])
        status, data = await self.post(stream=True)
        self.assert_block(status, data, True, 'content_blocked')

    async def test_enabled_benign_stream_and_health(self):
        await self.start()
        status, data = await self.post(stream=True)
        self.assertEqual((status, data), (200, relay_tests.event(0) + relay_tests.event(1)
                                         + relay_tests.event(2) + b'data: [DONE]\n\n'))
        self.classifier.ok = False
        async with self.client.get('/health') as response:
            self.assertEqual((response.status, await response.text()), (200, 'ok'))

    async def test_top_level_tool_input_is_checked(self):
        await self.start()
        status, data = await self.post(tools=[{'function': {'description': MARKER}}])
        self.assert_block(status, data, False, 'content_blocked')
        self.assertEqual(self.seen, [])

    async def test_eof_and_malformed_stream_events_fail_closed(self):
        self.config = replace(self.config, chunk_tokens=64)
        self.classifier.config = self.config
        relay = sse_relay.SafetyRelay('http://127.0.0.1:1', relay_tests.HB, self.config, self.classifier)
        request = Mock(path='/v1/chat/completions', headers={})
        for raw, code in ((sse({'content': MARKER}).rstrip(b'\n'), 'content_blocked'),
                          (b'data: {bad json}', 'safety_unavailable')):
            guard = sse_relay.SafetyStream(relay, request, ['user: hello world'])
            resp = Mock(headers={})
            out = Mock(write=AsyncMock(), write_eof=AsyncMock())
            self.assertTrue(await guard.write(raw, resp, out))
            await guard.finish(resp, out)
            data = b''.join(call.args[0] for call in out.write.call_args_list)
            self.assert_block(200, data, True, code)
            resp.close.assert_called()

    async def test_encoded_stream_fails_closed_before_forwarding(self):
        relay = sse_relay.SafetyRelay('http://127.0.0.1:1', relay_tests.HB, self.config, self.classifier)
        resp = Mock(status=200, headers={'Content-Type': 'text/event-stream', 'Content-Encoding': 'gzip'})
        relay.request = Mock(return_value=asyncio.sleep(0, result=resp))
        relay.block = AsyncMock(return_value='blocked')
        request = Mock(path='/v1/chat/completions')
        guard = sse_relay.SafetyStream(relay, request, ['user: hello world'])
        self.assertEqual(await relay.stream(request, b'{}', guard), 'blocked')
        result = relay.block.call_args.args[3]
        self.assertEqual(result.reason, 'classifier_unavailable')
        resp.close.assert_called()

    async def test_disabled_relay_never_extracts_or_constructs_classifier(self):
        with patch.object(safety, 'request_texts', side_effect=AssertionError('disabled extraction')), \
             patch.object(safety, 'ShieldstralClassifier', side_effect=AssertionError('disabled classifier')):
            await self.start(enabled=False)
            status, data = await self.post(stream=True, text=MARKER)
            self.assertEqual((status, data), (200, relay_tests.event(0) + relay_tests.event(1)
                                             + relay_tests.event(2) + b'data: [DONE]\n\n'))
            status, data = await self.post(text=MARKER)
            self.assertEqual((status, json.loads(data)['choices'][0]['message']['content']), (200, 'hi'))
        self.assertEqual(self.classifier.calls, [])
        self.seal.assert_not_called()
        self.assertEqual(self.seen[-1][0]['Authorization'], 'Bearer test-only')


_REAL_SEAL = safety.seal_incident if safety else None


if __name__ == '__main__':
    unittest.main()
