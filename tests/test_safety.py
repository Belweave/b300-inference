"""Offline scorer contract tests: inert canary and benign text only."""
import asyncio
from dataclasses import replace
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import test_sse_relay as relay_tests
try:
    from aiohttp import ClientError, web
    from aiohttp.test_utils import TestClient, TestServer
    import safety
    import safety_eval
    import safety_loadtest
    import sse_relay
except ImportError:
    safety = None


def logprobs(*entries):
    return {'choices': [{'logprobs': {'content': [{'top_logprobs': [
        {'token': token, 'logprob': value} for token, value in entries]}]}}]}


def fake_classifier(config, score=0.01):
    return Mock(config=config, score=AsyncMock(return_value=[score]), healthy=AsyncMock(return_value=True))


def engine_response(status=200, payload=None):
    response = Mock(status=status, json=AsyncMock(return_value=payload))
    return Mock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))


@unittest.skipIf(safety is None, 'aiohttp not installed')
class ConfigAndScoreTests(unittest.TestCase):
    def test_config_defaults_and_overrides(self):
        config = safety.SafetyConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual(config.url, 'http://127.0.0.1:8600')
        self.assertEqual((config.threshold, config.timeout_seconds, config.max_inflight), (0.5, 3.0, 128))
        self.assertEqual(config.classifier_key, '')
        self.assertEqual(config.model_revision, safety.POLICY_VERSION)
        self.assertEqual(safety.POLICY_VERSION, 'sha256:' + hashlib.sha256(safety.POLICY.encode()).hexdigest()[:16])
        self.assertEqual(config.model_id, 'mistralai/Shieldstral-1.0-3B@' + safety.POLICY_VERSION)
        self.assertEqual(config.output_guard, 'qwen3guard')
        self.assertEqual(config.qwen_url, 'http://127.0.0.1:8601')
        self.assertEqual(config.qwen_threshold, 0.5)
        self.assertEqual(config.qwen_model_id, 'Qwen/Qwen3Guard-Gen-0.6B@unpinned')
        config = safety.SafetyConfig.from_env({'SAFETY': '1', 'SAFETY_URL': 'http://localhost:8601',
                                             'SAFETY_MODEL': 'test', 'SAFETY_MODEL_REVISION': 'revision',
                                             'SAFETY_THRESHOLD': '0.7', 'SAFETY_TIMEOUT_SECONDS': '0.2',
                                             'SAFETY_MAX_INFLIGHT': '2', 'SAFETY_CLASSIFIER_KEY': 'test-only',
                                             'SAFETY_OUTPUT_GUARD': 'shieldstral',
                                             'SAFETY_QWEN_URL': 'http://localhost:8602/',
                                             'SAFETY_QWEN_MODEL': 'qwen-test', 'SAFETY_QWEN_REVISION': 'rev',
                                             'SAFETY_QWEN_THRESHOLD': '0.75'})
        self.assertTrue(config.enabled)
        self.assertEqual((config.model_id, config.threshold, config.timeout_seconds, config.max_inflight),
                         ('test@revision', 0.7, 0.2, 2))
        self.assertEqual(config.classifier_key, 'test-only')
        self.assertNotIn(config.classifier_key, repr(config))
        self.assertEqual((config.output_guard, config.qwen_url, config.qwen_model_id, config.qwen_threshold),
                         ('shieldstral', 'http://localhost:8602/', 'qwen-test@rev', 0.75))

    def test_config_rejects_bad_values(self):
        bad = {'SAFETY': ['true', '', '2'],
               'SAFETY_URL': ['http://example.test', 'http://127.0.0.2', 'http://127.0.0.1.example.test',
                              'file:///tmp/test', 'http://localhost:0', 'http://localhost:bad',
                              'http://user@localhost', 'http://localhost/?q=1', 'http://localhost/#hello',
                              'http://[bad'],
               'SAFETY_THRESHOLD': ['0', '1', '-1', 'nan', 'inf', 'bad'],
               'SAFETY_MAX_INFLIGHT': ['0', '-1', '1.5', 'bad'],
               'SAFETY_TIMEOUT_SECONDS': ['0', '-1', 'inf', 'nan', 'bad'],
               'SAFETY_MODEL': ['', ' ', '\0'], 'SAFETY_MODEL_REVISION': ['', ' ', '\0'],
               'SAFETY_CLASSIFIER_KEY': ['test\nonly', 'test\ronly', '\0', '\ud800', None]}
        bad.update({'SAFETY_QWEN_URL': bad['SAFETY_URL'] + ['', ' ', '\0', '\n', None],
                    'SAFETY_QWEN_THRESHOLD': bad['SAFETY_THRESHOLD'] + [None],
                    'SAFETY_OUTPUT_GUARD': ['', 'other', 'Qwen3Guard', 'shieldstral\n', None],
                    'SAFETY_QWEN_MODEL': ['', ' ', '\0', '\r', '\ud800', None],
                    'SAFETY_QWEN_REVISION': ['', ' ', '\0', '\n', '\ud800', None]})
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    safety.SafetyConfig.from_env({name: value})
        self.assertEqual(safety.SafetyConfig.from_env({'SAFETY_CLASSIFIER_KEY': ''}).classifier_key, '')

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
                        logprobs(('yes', float('inf'))), logprobs(('yes', True)),
                        logprobs(('yes', 'bad')), logprobs((None, -1))):
            with self.subTest(payload=payload), self.assertRaises(safety.ClassifierUnavailable):
                safety.parse_score(payload)

    def test_parse_qwen_scores_only_the_sexual_category(self):
        cases = (
            ('Safety: Unsafe\nCategories: Sexual Content or Sexual Acts', 1.0),
            ('Safety: Controversial\nCategories: Sexual Content or Sexual Acts', 0.5),
            ('Safety: Unsafe\nCategories: Violent', 0.0),
            ('Safety: Safe\nCategories: None', 0.0),
            ('Safety: Safe', 0.0),
            ('Safety: Unsafe', 0.0),
            ('Safety: Safe\nCategories: Sexual Content or Sexual Acts', 0.0),
            ('Safety: Unsafe\nCategories: Violent, Sexual Content or Sexual Acts, PII\nRefusal: No', 1.0),
            ('Safety: Unsafe\r\nCategories: Sexual Content or Sexual Acts\r\n', 1.0),
            ('Safety: Unsafe\nCategories: Not Sexual Content or Sexual Acts', 0.0),
            ('Safety: Unsafe\nCategories: Violent\nSexual Content or Sexual Acts', 0.0),
        )
        for content, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(safety.parse_qwen(content), expected)
        for content in ('', 'Categories: Sexual Content or Sexual Acts', 'Safety: Unknown', None,
                        'Safety: Safely', 'Safety: Unsafe extra'):
            with self.subTest(content=content), self.assertRaises(safety.ClassifierUnavailable):
                safety.parse_qwen(content)


@unittest.skipIf(safety is None, 'aiohttp not installed')
class ScoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = safety.SafetyConfig(enabled=True, output_guard='shieldstral')
        self.classifier = fake_classifier(self.config)
        self.scorer = safety.SafetyScorer(self.config, self.classifier, self.classifier, self.classifier)

    async def test_canary_short_circuits_without_model_or_health(self):
        self.classifier.score.side_effect = safety.ClassifierUnavailable('unavailable')
        for phase in ('input', 'output'):
            result = await safety.score_text(self.scorer, 'hello world ' + safety.CANARY + ' hello', phase)
            self.assertEqual(result, (True, 1.0, self.config.model_id))
        self.classifier.score.assert_not_called()
        self.classifier.healthy.assert_not_called()

    async def test_benign_text_uses_inclusive_threshold_for_both_phases(self):
        for score in (0, 0.49, 0.5, 0.51, 1):
            self.classifier.score.return_value = [score]
            for phase in ('input', 'output'):
                result = await safety.score_text(self.scorer, 'hello world', phase)
                self.assertEqual(result, (score >= self.config.threshold, float(score), self.config.model_id))
                self.assertIs(type(result[1]), float)
                self.classifier.score.assert_awaited_with(['hello world'])
        self.classifier.healthy.assert_not_called()

    async def test_unavailability_propagates(self):
        error = safety.ClassifierUnavailable('unavailable')
        self.classifier.score.side_effect = error
        with self.assertRaises(safety.ClassifierUnavailable) as raised:
            await safety.score_text(self.scorer, 'hello world', 'input')
        self.assertIs(raised.exception, error)

    async def test_invalid_model_scores_are_unavailable(self):
        for scores in (None, [], [0, 1], [True], ['0.5'], [math.nan], [math.inf], [-0.1], [1.1], [10**1000]):
            self.classifier.score.return_value = scores
            with self.subTest(scores=scores), self.assertRaises(safety.ClassifierUnavailable):
                await safety.score_text(self.scorer, 'hello world', 'input')

    async def test_shieldstral_adapter_and_cached_health(self):
        session = Mock(post=Mock(return_value=engine_response(payload=logprobs(('yes', -2), ('no', -1)))),
                       get=Mock(return_value=engine_response()))
        classifier = safety.ShieldstralClassifier(self.config, session)
        self.assertAlmostEqual((await classifier.score(['hello world']))[0], 1 / (1 + math.exp(1)))
        args, kwargs = session.post.call_args
        self.assertEqual(args, (self.config.url + '/v1/chat/completions',))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'].total, 3.0)
        self.assertNotIn('headers', kwargs)
        payload = kwargs['json']
        self.assertEqual(payload['model'], self.config.model)
        self.assertEqual((payload['max_tokens'], payload['logprobs'], payload['top_logprobs']), (1, True, 20))
        self.assertEqual(payload['messages'], [
            {'role': 'system', 'content': safety.SYSTEM},
            {'role': 'user', 'content': safety.USER_TEMPLATE.format(
                instruct=safety.INSTRUCT, query=safety.QUERY, text='hello world')}])
        self.assertTrue(await classifier.healthy())
        self.assertTrue(await classifier.healthy())
        session.get.assert_called_once()

    async def test_shieldstral_failures_and_timeouts_are_sanitized(self):
        session = Mock(post=Mock(return_value=engine_response(status=503)))
        classifier = safety.ShieldstralClassifier(replace(self.config, timeout_seconds=0.01), session)
        with self.assertRaises(safety.ClassifierUnavailable):
            await classifier.score(['hello world'])
        for failure in (ClientError('hello world'), ValueError('hello world'), RuntimeError('hello world')):
            session.post.side_effect = failure
            with self.assertRaises(safety.ClassifierUnavailable) as raised:
                await classifier.score(['hello world'])
            self.assertNotIn('hello world', str(raised.exception))
        async def slow(text):
            await asyncio.sleep(1)
        with patch.object(classifier, '_score', side_effect=slow):
            with self.assertRaises(safety.ClassifierUnavailable):
                await classifier.score(['hello world'])
        session.get.side_effect = ClientError('unavailable')
        self.assertFalse(await classifier.healthy())


@unittest.skipIf(safety is None, 'aiohttp not installed')
class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = safety.SafetyConfig(enabled=True, output_guard='shieldstral')
        self.classifier = fake_classifier(self.config)

    async def start(self, config=None, classifier=None, upstream='http://127.0.0.1:1', scorer=None):
        config = config or self.config
        classifier = classifier or self.classifier
        app = sse_relay.make_app(upstream, relay_tests.HB, safety_config=config,
                                 classifier=classifier, scorer=scorer)
        relay = app.on_startup[-1].__self__
        client = TestClient(TestServer(app, host='127.0.0.1', handler_cancellation=True))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client, relay

    async def post(self, client, text='hello world', phase='input', **kwargs):
        async with client.post('/v1/safety/csam', json={'text': text, 'phase': phase}, **kwargs) as response:
            return response.status, await response.json()

    async def test_happy_path_exact_response_and_input_sizes(self):
        client, relay = await self.start()
        with patch.object(relay.session, 'request', side_effect=AssertionError('inference contacted')):
            for phase, text in (('input', 'hello world'), ('input', 'hello world ' * 1333 + 'hell'),
                                ('output', 'hello world ' * 53 + 'hell'), ('output', '')):
                status, body = await self.post(client, text, phase)
                self.assertEqual(status, 200)
                self.assertEqual(body, {'flag': False, 'score': 0.01, 'model': self.config.model_id})
                self.assertIs(type(body['flag']), bool)
                self.assertIs(type(body['score']), float)
                self.assertTrue(math.isfinite(body['score']))
                self.classifier.score.assert_awaited_with([text])
        self.classifier.score.return_value = [0.5]
        self.assertTrue((await self.post(client))[1]['flag'])

    async def test_auth_exact_match_and_no_key_in_logs_or_errors(self):
        config = replace(self.config, classifier_key='test-classifier-key')
        self.classifier.config = config
        client, _ = await self.start(config)
        with self.assertLogs(level='DEBUG') as captured:
            logging.getLogger(__name__).debug('capture enabled')
            for headers in ({}, {'Authorization': 'Bearer wrong-test-key'},
                            {'Authorization': 'Bearer hello \u2600'},
                            {'Authorization': 'bearer ' + config.classifier_key},
                            {'Authorization': 'Bearer ' + config.classifier_key + 'extra'}):
                status, body = await self.post(client, safety.CANARY, headers=headers)
                self.assertEqual((status, body), (401, {'error': 'unauthorized'}))
            self.classifier.score.assert_not_called()
            status, body = await self.post(client, headers={'Authorization': 'Bearer ' + config.classifier_key})
            self.assertEqual(status, 200)
            self.assertNotIn(config.classifier_key, json.dumps(body))
            self.classifier.score.side_effect = safety.ClassifierUnavailable(config.classifier_key)
            status, body = await self.post(client, headers={'Authorization': 'Bearer ' + config.classifier_key})
            self.assertEqual((status, body), (503, {'error': 'classifier unavailable'}))
        self.assertNotIn(config.classifier_key, '\n'.join(captured.output))
        self.assertNotIn('wrong-test-key', '\n'.join(captured.output))

    async def test_engine_session_is_reused_and_never_receives_inbound_auth(self):
        config = replace(self.config, classifier_key='test-classifier-key')
        app = sse_relay.make_app('http://127.0.0.1:1', safety_config=config)
        relay = app.on_startup[-1].__self__
        async with TestClient(TestServer(app, host='127.0.0.1', handler_cancellation=True)) as client:
            self.assertIsNot(relay.safety_session, relay.session)
            self.assertIs(relay.classifier.session, relay.safety_session)
            response = engine_response(payload=logprobs(('yes', -2), ('no', -1)))
            with patch.object(relay.safety_session, 'post', return_value=response) as post:
                for _ in range(2):
                    status, _ = await self.post(client, headers={'Authorization': 'Bearer ' + config.classifier_key})
                    self.assertEqual(status, 200)
                self.assertEqual(post.call_count, 2)
                self.assertNotIn(config.classifier_key, repr(post.call_args_list))
                self.assertNotIn('Authorization', relay.safety_session.headers)
        self.assertTrue(relay.safety_session.closed)
        self.assertTrue(relay.session.closed)

    async def test_invalid_bodies_and_phases(self):
        client, _ = await self.start()
        bodies = [None, [], {}, {'text': 'hello world'}, {'phase': 'input'},
                  {'text': 1, 'phase': 'input'}, {'text': None, 'phase': 'input'}]
        bodies += [{'text': 'hello world', 'phase': phase}
                   for phase in ('other', None, [], {}, True, 0)]
        bodies.append({'text': '\ud800', 'phase': 'input'})
        for body in bodies:
            async with client.post('/v1/safety/csam', data=json.dumps(body),
                                   headers={'Content-Type': 'application/json'}) as response:
                self.assertEqual(response.status, 400)
                self.assertEqual(await response.json(), {'error': 'invalid request'})
        for raw, content_type in ((b'not json', 'application/json'), (b'\xff', 'application/json'),
                                  (b'[' * 2000 + b']' * 2000, 'application/json'),
                                  (b'{}', 'text/plain'), (b'', 'application/json')):
            async with client.post('/v1/safety/csam', data=raw, headers={'Content-Type': content_type}) as response:
                self.assertEqual(response.status, 400)
        self.classifier.score.assert_not_called()

    async def test_methods_and_unknown_paths(self):
        client, relay = await self.start()
        with patch.object(relay.session, 'request', side_effect=AssertionError('inference contacted')):
            for method in ('GET', 'PUT', 'DELETE', 'OPTIONS', 'HEAD'):
                async with client.request(method, '/v1/safety/csam') as response:
                    self.assertEqual(response.status, 405)
                    self.assertEqual(response.headers['Allow'], 'POST')
            for path in ('/v1/safety/other', '/v1/safety/csam/extra', '/v1/safety/'):
                for method in ('GET', 'POST'):
                    async with client.request(method, path) as response:
                        self.assertEqual(response.status, 404)
        self.classifier.score.assert_not_called()

    async def test_unavailable_and_canary_without_model(self):
        client, _ = await self.start()
        for failure in (safety.ClassifierUnavailable('hello world'), RuntimeError('hello world')):
            self.classifier.score.side_effect = failure
            status, body = await self.post(client)
            self.assertEqual((status, body), (503, {'error': 'classifier unavailable'}))
        self.classifier.score.reset_mock()
        for phase in ('input', 'output'):
            status, body = await self.post(client, 'hello world ' + safety.CANARY, phase)
            self.assertEqual((status, body), (200, {'flag': True, 'score': 1.0, 'model': self.config.model_id}))
        self.classifier.score.assert_not_called()
        self.classifier.healthy.assert_not_called()

    async def test_invalid_score_is_503(self):
        client, _ = await self.start()
        for scores in ([math.nan], [math.inf], [True], [], [0, 1]):
            self.classifier.score.return_value = scores
            self.assertEqual((await self.post(client))[0], 503)

    async def test_timeout_includes_queueing_and_releases_slot(self):
        config = replace(self.config, max_inflight=1, timeout_seconds=0.05)
        self.classifier.config = config
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def slow(texts):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.classifier.score.side_effect = slow
        client, relay = await self.start(config)
        first = asyncio.create_task(self.post(client))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(self.post(client))
        results = await asyncio.wait_for(asyncio.gather(first, second), 0.5)
        self.assertEqual([status for status, _ in results], [503, 503])
        self.assertTrue(cancelled.is_set())
        self.assertFalse(relay.inflight.locked())
        self.classifier.score.side_effect = None
        self.assertEqual((await self.post(client))[0], 200)
        # An occupied queue must time out even without reaching the classifier.
        self.classifier.score.reset_mock()
        async with relay.inflight:
            self.assertEqual((await self.post(client))[0], 503)
        self.classifier.score.assert_not_called()

    async def test_inflight_bound_is_shared_between_apps(self):
        config = replace(self.config, max_inflight=2)
        self.classifier.config = config
        active = peak = 0
        full, release = asyncio.Event(), asyncio.Event()

        async def blocked(texts):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                full.set()
            try:
                await release.wait()
                return [0.01]
            finally:
                active -= 1

        self.classifier.score.side_effect = blocked
        first, relay1 = await self.start(config)
        second, relay2 = await self.start(config)
        self.assertIs(relay1.inflight, relay2.inflight)
        tasks = [asyncio.create_task(self.post(first if i % 2 else second)) for i in range(8)]
        try:
            await asyncio.wait_for(full.wait(), 1)
            self.assertEqual(peak, 2)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(peak, 2)
        self.assertTrue(all(status == 200 for status, _ in results))

    async def test_enabled_and_disabled_inference_passthrough(self):
        for enabled in (False, True):
            seen = []
            engine = relay_tests.fake_engine(seen, asyncio.Event(), reply='hello world')
            async with TestServer(engine, host='127.0.0.1', handler_cancellation=True) as server:
                with patch.dict(os.environ, {'SAFETY': '1' if enabled else '0'}), \
                     patch.object(safety, 'ShieldstralClassifier', side_effect=AssertionError('model constructed')):
                    config = self.config if enabled else None
                    app = sse_relay.make_app(str(server.make_url('/')), relay_tests.HB,
                                            safety_config=config, classifier=self.classifier)
                    relay = app.on_startup[-1].__self__
                    self.assertIs(type(relay), sse_relay.SafetyRelay if enabled else sse_relay.Relay)
                    async with TestClient(TestServer(app, host='127.0.0.1', handler_cancellation=True)) as client:
                        for stream in (False, True):
                            body = {'stream': stream, 'messages': [{'role': 'user', 'content': safety.CANARY}]}
                            async with client.post('/v1/chat/completions', json=body,
                                                   headers={'Authorization': 'Bearer inference-test-only'}) as response:
                                self.assertEqual(response.status, 200)
                                data = await response.read()
                            if stream:
                                self.assertEqual(data, b''.join(relay_tests.event(i) for i in range(3))
                                                 + b'data: [DONE]\n\n')
                            else:
                                self.assertEqual(json.loads(data)['choices'][0]['message']['content'], 'hello world')
                            self.assertEqual(seen[-1][0]['Authorization'], 'Bearer inference-test-only')
                            self.assertEqual(seen[-1][1], body)
                        async with client.get('/health') as response:
                            self.assertEqual((response.status, await response.text()), (200, 'ok'))
        self.classifier.score.assert_not_called()
        self.classifier.healthy.assert_not_called()

    async def test_make_app_env_wiring_and_disabled_ignores_classifier_config(self):
        with patch.dict(os.environ, {'SAFETY': '1', 'SAFETY_MODEL': 'test', 'SAFETY_MODEL_REVISION': 'revision'}):
            app = sse_relay.make_app('http://127.0.0.1:1', classifier=self.classifier)
            relay = app.on_startup[-1].__self__
            self.assertIsInstance(relay, sse_relay.SafetyRelay)
            self.assertEqual(relay.config.model_id, 'test@revision')
        with patch.dict(os.environ, {'SAFETY': '0', 'SAFETY_THRESHOLD': 'bad'}), \
             patch.object(safety.SafetyConfig, 'from_env', side_effect=AssertionError('disabled config read')):
            app = sse_relay.make_app('http://127.0.0.1:1')
            self.assertIs(type(app.on_startup[-1].__self__), sse_relay.Relay)
        with patch.dict(os.environ, {'SAFETY': 'true'}), self.assertRaises(ValueError):
            sse_relay.make_app('http://127.0.0.1:1')


@unittest.skipIf(safety is None, 'aiohttp not installed')
class OutputScoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = safety.SafetyConfig(enabled=True)
        self.minors = fake_classifier(self.config, 0.8)
        self.qwen = fake_classifier(self.config, 0.0)
        self.scorer = safety.SafetyScorer(self.config, self.minors, self.qwen, self.minors)

    async def test_input_still_uses_only_minors_policy(self):
        self.assertEqual(await safety.score_text(self.scorer, 'hello world', 'input'),
                         (True, 0.8, self.config.model_id))
        self.minors.score.assert_awaited_once_with(['hello world'])
        self.qwen.score.assert_not_called()

    async def test_qwen_false_negatives_cannot_bypass_minors_policy(self):
        for score in (0.0, 0.49):
            self.qwen.score.return_value = [score]
            self.assertEqual(await safety.score_text(self.scorer, 'hello world', 'output'),
                             (True, 0.8, self.config.qwen_model_id+'+confirm:'+self.config.model_id))
        self.assertEqual(self.minors.score.await_count, 2)

    async def test_qwen_threshold_cannot_disable_minors_confirmation(self):
        config = replace(self.config, qwen_threshold=0.75)
        scorer = replace(self.scorer, config=config)
        self.qwen.score.return_value = [0.5]
        self.assertEqual(await safety.score_text(scorer, 'hello world', 'output'),
                         (True, 0.8, config.qwen_model_id+'+confirm:'+config.model_id))
        self.minors.score.assert_awaited_once()

    async def test_qwen_requires_minors_confirmation_and_reports_authoritative_score(self):
        # These are category scores, not actual sexual content. A low minors
        # score represents adult content or any other out-of-scope material.
        model = self.config.qwen_model_id + '+confirm:' + self.config.model_id
        for qwen_score in (0.5, 1.0):
            for minors_score in (0.0, 0.49, 0.5, 0.8):
                with self.subTest(qwen=qwen_score, minors=minors_score):
                    self.qwen.score.return_value = [qwen_score]
                    self.minors.score.return_value = [minors_score]
                    self.assertEqual(await safety.score_text(self.scorer, 'adult topic label', 'output'),
                                     (minors_score >= self.config.threshold, minors_score, model))
                    self.minors.score.assert_awaited_with(['adult topic label'])

    async def test_required_backend_failure_and_timeout_propagate(self):
        for failed in (self.qwen, self.minors):
            for error in (safety.ClassifierUnavailable('unavailable'), asyncio.TimeoutError()):
                self.qwen.score.return_value = [1.0]
                failed.score.side_effect = error
                with self.assertRaises(type(error)):
                    await safety.score_text(self.scorer, 'hello world', 'output')
                if failed is self.qwen:
                    self.minors.score.assert_awaited_with(['hello world'])
                failed.score.side_effect = None

    async def test_invalid_qwen_or_confirmation_scores_fail_closed(self):
        for backend in (self.qwen, self.minors):
            for invalid in ([], [True], [math.nan], [math.inf], [-1.0], [2.0], ['1.0']):
                self.qwen.score.return_value = [1.0]
                self.minors.score.return_value = [0.8]
                backend.score.return_value = invalid
                with self.assertRaises(safety.ClassifierUnavailable):
                    await safety.score_text(self.scorer, 'hello world', 'output')

    async def test_output_canary_short_circuits_all_backends(self):
        for backend in (self.qwen, self.minors):
            backend.score.side_effect = AssertionError('backend must not run')
        self.assertEqual(await safety.score_text(self.scorer, 'prefix ' + safety.CANARY + ' suffix', 'output'),
                         (True, 1.0, self.config.qwen_model_id))
        for backend in (self.qwen, self.minors):
            backend.score.assert_not_called()
            backend.healthy.assert_not_called()

    async def test_qwen_response_form_and_cached_health_share_inflight(self):
        payload = {'choices': [{'message': {'content':
                   'Safety: Unsafe\nCategories: Sexual Content or Sexual Acts'}}]}
        session = Mock(post=Mock(return_value=engine_response(payload=payload)),
                       get=Mock(return_value=engine_response()))
        qwen = safety.Qwen3GuardClassifier(self.config, session)
        minors = safety.ShieldstralClassifier(self.config, session)
        self.assertIs(qwen.inflight, minors.inflight)
        async with qwen.inflight:
            self.assertEqual(await qwen.score(['hello world']), [1.0])
        args, kwargs = session.post.call_args
        self.assertEqual(args, (self.config.qwen_url + '/v1/chat/completions',))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('headers', kwargs)
        self.assertEqual(kwargs['timeout'].total, self.config.timeout_seconds)
        self.assertEqual(kwargs['json'], {
            'model': self.config.qwen_model, 'max_tokens': 64, 'temperature': 0,
            'messages': [{'role': 'user', 'content': 'Please provide a response.'},
                         {'role': 'assistant', 'content': 'hello world'}]})
        self.assertTrue(await qwen.healthy())
        self.assertTrue(await qwen.healthy())
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.args, (self.config.qwen_url + '/health',))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    async def test_qwen_errors_and_missing_safety_label_are_unavailable(self):
        session = Mock()
        qwen = safety.Qwen3GuardClassifier(replace(self.config, timeout_seconds=0.01), session)
        for payload in (None, {}, {'choices': []}, {'choices': [{'message': {'content': None}}]},
                        {'choices': [{'message': {'content': 'Categories: Sexual Content or Sexual Acts'}}]}):
            session.post.return_value = engine_response(payload=payload)
            with self.assertRaises(safety.ClassifierUnavailable):
                await qwen.score(['hello world'])
        for status in (301, 401, 500, 503):
            session.post.return_value = engine_response(status=status)
            with self.assertRaises(safety.ClassifierUnavailable):
                await qwen.score(['hello world'])
        for error in (ClientError('private engine text'), ValueError('private engine text'),
                      RuntimeError('private engine text'), asyncio.TimeoutError('private engine text')):
            session.post.side_effect = error
            with self.assertRaises(safety.ClassifierUnavailable) as raised:
                await qwen.score(['hello world'])
            self.assertNotIn('private engine text', str(raised.exception))
        cancelled = asyncio.Event()

        async def slow(text):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(qwen, '_score', side_effect=slow), self.assertRaises(safety.ClassifierUnavailable):
            await qwen.score(['hello world'])
        self.assertTrue(cancelled.is_set())
        session.get.side_effect = ClientError('private engine text')
        self.assertFalse(await qwen.healthy())


@unittest.skipIf(safety is None, 'aiohttp not installed')
class OutputEndpointTests(unittest.IsolatedAsyncioTestCase):
    start = EndpointTests.start
    post = EndpointTests.post

    def setUp(self):
        self.config = safety.SafetyConfig(enabled=True)
        self.classifier = fake_classifier(self.config, 0.8)
        self.qwen = fake_classifier(self.config, 1.0)
        self.scorer = safety.SafetyScorer(self.config, self.classifier, self.qwen, self.classifier)

    async def test_injected_scorer_contract_and_required_failures(self):
        with patch.object(safety, 'ShieldstralClassifier', side_effect=AssertionError('unexpected backend')), \
             patch.object(safety, 'Qwen3GuardClassifier', side_effect=AssertionError('unexpected backend')):
            client, relay = await self.start(scorer=self.scorer)
        self.assertIs(relay.scorer, self.scorer)
        self.assertEqual(await self.post(client, phase='output'), (200, {
            'flag': True, 'score': 0.8, 'model': self.config.qwen_model_id + '+confirm:' + self.config.model_id}))
        self.classifier.score.return_value = [0.1]
        self.assertEqual((await self.post(client, phase='output'))[1]['flag'], False)
        self.assertEqual((await self.post(client, phase='output'))[1]['score'], 0.1)
        with self.assertLogs(level='DEBUG') as captured:
            logging.getLogger(__name__).debug('capture enabled')
            for backend in (self.qwen, self.classifier):
                for error in (safety.ClassifierUnavailable('private engine text'),
                              asyncio.TimeoutError('private engine text')):
                    self.classifier.score.reset_mock()
                    backend.score.side_effect = error
                    self.assertEqual(await self.post(client, phase='output'),
                                     (503, {'error': 'classifier unavailable'}))
                    if backend is self.qwen:
                        self.classifier.score.assert_awaited_with(['hello world'])
                    backend.score.side_effect = None
        self.assertNotIn('private engine text', '\n'.join(captured.output))
        self.qwen.score.side_effect = safety.ClassifierUnavailable('unavailable')
        self.assertEqual(await self.post(client, safety.CANARY, 'output'),
                         (200, {'flag': True, 'score': 1.0, 'model': self.config.qwen_model_id}))

    async def test_a_guard_that_is_not_up_blocks_requests(self):
        """The steady boot does not wait for the guards; until both answer, the relay refuses."""
        async def shieldstral(request):
            return web.json_response(logprobs(('no', -0.01), ('yes', -6.0)))
        app = web.Application()
        app.router.add_post('/v1/chat/completions', shieldstral)
        guard = TestServer(app, host='127.0.0.1')
        await guard.start_server()
        self.addAsyncCleanup(guard.close)
        closed = TestServer(web.Application(), host='127.0.0.1')
        await closed.start_server()
        down = str(closed.make_url('')).rstrip('/')
        await closed.close()  # Nothing listens here now, like a guard still loading.
        config = replace(self.config, url=str(guard.make_url('')).rstrip('/'), qwen_url=down, timeout_seconds=1)
        app = sse_relay.make_app('http://127.0.0.1:1', safety_config=config)
        async with TestClient(TestServer(app, host='127.0.0.1', handler_cancellation=True)) as client:
            self.assertEqual((await self.post(client, phase='input'))[0], 200)
            self.assertEqual(await self.post(client, phase='output'), (503, {'error': 'classifier unavailable'}))
            await guard.close()
            self.assertEqual(await self.post(client, phase='input'), (503, {'error': 'classifier unavailable'}))

    async def test_real_adapters_share_one_session_and_one_semaphore(self):
        config = replace(self.config, max_inflight=1, classifier_key='test-only-key')
        app = sse_relay.make_app('http://127.0.0.1:1', safety_config=config)
        relay = app.on_startup[-1].__self__
        responses = [engine_response(payload={'choices': [{'message': {'content':
                     'Safety: Unsafe\nCategories: Sexual Content or Sexual Acts'}}]}),
                     engine_response(payload=logprobs(('yes', -2), ('no', -1)))]
        async with TestClient(TestServer(app, host='127.0.0.1', handler_cancellation=True)) as client:
            self.assertIs(relay.scorer.input_backend, relay.scorer.confirm_backend)
            for backend in (relay.scorer.input_backend, relay.scorer.output_backend):
                self.assertIs(backend.session, relay.safety_session)
                self.assertIs(backend.inflight, relay.inflight)
            with patch.object(relay.safety_session, 'post', side_effect=responses) as post:
                status, body = await self.post(client, phase='output',
                                               headers={'Authorization': 'Bearer ' + config.classifier_key})
            self.assertEqual(status, 200)  # With one slot, nested acquisition would time out.
            self.assertFalse(body['flag'])
            self.assertAlmostEqual(body['score'], 1 / (1 + math.exp(1)))
            self.assertEqual([call.args[0] for call in post.call_args_list],
                             [config.qwen_url + '/v1/chat/completions', config.url + '/v1/chat/completions'])
            self.assertNotIn(config.classifier_key, repr(post.call_args_list))
            self.assertNotIn('Authorization', relay.safety_session.headers)
        self.assertTrue(relay.safety_session.closed)

    async def test_confirmation_occupies_shared_slot_and_is_cancelled_on_timeout(self):
        config = replace(self.config, max_inflight=1, timeout_seconds=0.1)
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(texts):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.classifier.score.side_effect = blocked
        scorer = replace(self.scorer, config=config)
        client, relay = await self.start(config, scorer=scorer)
        output = asyncio.create_task(self.post(client, phase='output'))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(relay.inflight.locked())
        self.assertEqual((await output)[0], 503)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(relay.inflight.locked())
        self.qwen.score.assert_awaited_once()
        self.classifier.score.side_effect = None
        self.assertEqual((await self.post(client, phase='input'))[0], 200)


@unittest.skipIf(safety is None, 'aiohttp not installed')
class EvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_metrics_and_gates(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            metrics = await safety_eval.evaluate(safety_eval.mock_scorer(), safety_eval.SYNTHETIC_DATASET)
        self.assertEqual(output.getvalue(), '')
        self.assertTrue(safety_eval.passes_gates(metrics))
        for values in metrics['phases'].values():
            self.assertEqual(values['confusion_matrix'], {'tp': 2, 'fp': 0, 'tn': 6, 'fn': 0})
            self.assertEqual((values['precision'], values['recall'], values['fpr'], values['fnr']),
                             (1.0, 1.0, 0.0, 0.0))
        self.assertTrue(all(example['matched'] for example in metrics['examples']))

    async def test_missed_positive_fails_recall_gate(self):
        with patch.object(safety_eval.MockBackend, 'score', new=AsyncMock(return_value=[0.0])):
            metrics = await safety_eval.evaluate(safety_eval.mock_scorer(), safety_eval.SYNTHETIC_DATASET)
        self.assertFalse(safety_eval.passes_gates(metrics))
        for values in metrics['phases'].values():
            self.assertEqual(values['recall'], 0.5)  # Only the model-independent canary is caught.
            self.assertEqual(values['fnr'], 0.5)
        self.assertTrue(safety_eval.passes_gates(metrics, min_recall=0.5))

    async def test_false_positive_fails_fpr_gate(self):
        with patch.object(safety_eval.MockBackend, 'score', new=AsyncMock(return_value=[1.0])):
            metrics = await safety_eval.evaluate(safety_eval.mock_scorer(), safety_eval.SYNTHETIC_DATASET)
        self.assertFalse(safety_eval.passes_gates(metrics))
        for values in metrics['phases'].values():
            self.assertEqual(values['fpr'], 1.0)
            self.assertEqual(values['precision'], 0.25)

    async def test_each_phase_needs_both_classes(self):
        for dataset in ([], safety_eval.SYNTHETIC_DATASET[:8],
                        [row for row in safety_eval.SYNTHETIC_DATASET if row['expected']],
                        [row for row in safety_eval.SYNTHETIC_DATASET if not row['expected']]):
            metrics = await safety_eval.evaluate(safety_eval.mock_scorer(), dataset)
            self.assertFalse(safety_eval.passes_gates(metrics))

    def test_cli_mock_default_json_and_text_never_print_examples(self):
        for args in ([], ['--json']):
            with patch('sys.stdout', new_callable=io.StringIO) as output, \
                 patch('sys.stderr', new_callable=io.StringIO) as errors, \
                 patch.object(safety_eval, 'ClientSession', side_effect=AssertionError('network attempted')):
                self.assertEqual(safety_eval.main(args), 0)
            combined = output.getvalue() + errors.getvalue()
            for example in safety_eval.SYNTHETIC_DATASET:
                self.assertNotIn(example['text'], combined)
                self.assertNotIn(example['note'], combined)
            self.assertNotIn(safety_eval.TEST_TRIGGER, combined)
            self.assertNotIn(safety.CANARY, combined)
            self.assertIn('Preetham/counsel-approved', errors.getvalue())
            if args:
                summary = json.loads(output.getvalue())
                self.assertTrue(summary['passed'])
                self.assertEqual((summary['backend'], summary['dataset']), ('mock', 'synthetic'))
                self.assertEqual(summary['phases']['output']['recall'], 1.0)
            else:
                self.assertIn('gates=PASS', output.getvalue())

    def test_cli_failure_exit_and_sanitized_errors(self):
        with patch.object(safety_eval.MockBackend, 'score', new=AsyncMock(return_value=[0.0])), \
             patch('sys.stdout', new_callable=io.StringIO) as output, \
             patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(safety_eval.main(['--json']), 1)
        self.assertFalse(json.loads(output.getvalue())['passed'])
        for failure in (safety.ClassifierUnavailable('private example text'),
                        asyncio.TimeoutError('private example text')):
            with patch.object(safety_eval, 'run_evaluation', side_effect=failure), \
                 patch('sys.stdout', new_callable=io.StringIO) as output, \
                 patch('sys.stderr', new_callable=io.StringIO) as errors:
                self.assertEqual(safety_eval.main(['--json']), 2)
            self.assertFalse(json.loads(output.getvalue())['passed'])
            self.assertNotIn('private example text', output.getvalue() + errors.getvalue())

    def test_dataset_paths_and_schema(self):
        self.assertIs(safety_eval.load_dataset(), safety_eval.SYNTHETIC_DATASET)
        inside = safety_eval.REPO_ROOT / 'scripts' / 'safety_eval.py'
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / 'synthetic.jsonl'
            outside.write_text('\n'.join(json.dumps(row) for row in safety_eval.SYNTHETIC_DATASET),
                               encoding='utf-8')
            self.assertEqual(safety_eval.load_dataset(outside), list(safety_eval.SYNTHETIC_DATASET))
            link = Path(directory) / 'inside.jsonl'
            link.symlink_to(inside)
            for path in (inside, link, inside.parent / '..' / 'safety_eval.py'):
                with patch.object(Path, 'open', side_effect=AssertionError('repo file read')), \
                     self.assertRaises(ValueError):
                    safety_eval.load_dataset(path)
            for invalid in ('not json', '{}', '[]', '{"text": "benign", "phase": "other", "expected": true}',
                            '{"text": "benign", "phase": "input", "expected": 1}'):
                outside.write_text(invalid, encoding='utf-8')
                with self.assertRaises(ValueError):
                    safety_eval.load_dataset(outside)
            outside.write_text('private invalid dataset text', encoding='utf-8')
            with patch('sys.stdout', new_callable=io.StringIO) as output, \
                 patch('sys.stderr', new_callable=io.StringIO) as errors:
                self.assertEqual(safety_eval.main(['--dataset', str(outside), '--json']), 2)
            self.assertNotIn('private invalid dataset text', output.getvalue() + errors.getvalue())

    def test_cli_gate_bounds(self):
        for option in ('--min-recall', '--max-fpr'):
            for value in ('-0.1', '1.1', 'nan', 'inf'):
                with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as raised:
                    safety_eval.main([option, value])
                self.assertEqual(raised.exception.code, 2)


@unittest.skipIf(safety is None, 'aiohttp not installed')
class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_offline_load(self):
        result = await safety_loadtest.run_load_test(rps=200, concurrency=64, duration_seconds=0.5,
                                                    mock_latency_ms=0.2, mock_p99_ms=0.5)
        self.assertEqual(result.requests, 100)
        self.assertEqual((result.non_200, result.invalid_responses), (0, 0))
        self.assertLess(result.p99_ms, 100)
        self.assertLessEqual(result.p50_ms, result.p95_ms)
        self.assertLessEqual(result.p95_ms, result.p99_ms)
        self.assertTrue(result.passed(100))

    async def test_load_test_counts_failures(self):
        with patch.object(safety_loadtest.MockClassifier, 'score', side_effect=safety.ClassifierUnavailable):
            result = await safety_loadtest.run_load_test(rps=200, duration_seconds=0.05)
        self.assertEqual(result.requests, 10)
        self.assertEqual(result.non_200, 9)  # The first request is the model-independent canary.
        self.assertFalse(result.passed(100))

    def test_cli_budget_and_status_exit_codes(self):
        passing = safety_loadtest.LoadResult(100, 0.5, 0, 0, 1, 2, 3)
        for result in (passing, replace(passing, p99_ms=101), replace(passing, non_200=1),
                       replace(passing, invalid_responses=1)):
            with patch.object(safety_loadtest, 'run_load_test', return_value=result), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(safety_loadtest.main([]), 0 if result == passing else 1)
            self.assertIn('throughput=', output.getvalue())
            self.assertIn('p99=', output.getvalue())
            self.assertIn('non_200=', output.getvalue())


if __name__ == '__main__':
    unittest.main()
