"""Live-client contract tests against an in-process server with mocked verdicts."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import safety
import safety_loadtest as load


class LiveLoadTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, kind='valid', **options):
        self.seen = []

        async def respond(request):
            self.assertEqual(request.headers.get('Authorization'), 'Bearer fixture-auth')
            body = await request.json()
            self.seen.append(body)
            if kind == 'non200':
                return web.json_response({}, status=503)
            if kind == 'redirect':
                return web.Response(status=302, headers={'Location': '/unexpected'})
            if kind == 'timeout':
                await asyncio.sleep(4)
            if kind == 'json':
                return web.Response(text='hello world', content_type='application/json')
            reply = {'flag': body['text'] == safety.CANARY, 'score': 0.73, 'model': 'mock@revision'}
            if kind == 'canary':
                reply['flag'] = False
            if kind == 'shape':
                reply['score'] = True
            if kind == 'benign-flag':
                reply['flag'] = True
            return web.json_response(reply)

        app = web.Application()
        app.router.add_post('/v1/safety/csam', respond)
        async with TestServer(app, host='127.0.0.1', handler_cancellation=True) as server:
            return await load.run_live_load_test(str(server.make_url('/')), 'fixture-auth',
                                                 **{'rps': 1000, 'duration_seconds': 0.051, **options})

    async def test_mix_canary_rate_and_variable_scores(self):
        result = await self.run_fixture()
        self.assertEqual(result.requests, 51)
        self.assertEqual((result.non_200, result.invalid_responses), (0, 0))
        self.assertEqual(sum(r['text'] == safety.CANARY for r in self.seen), 2)
        self.assertEqual(sum(r['phase'] == 'input' for r in self.seen), 6)
        self.assertEqual({len(r['text']) for r in self.seen if r['text'] != safety.CANARY}, {640, 16000})
        self.assertTrue((await self.run_fixture('benign-flag')).passed(1000))

    async def test_non200_redirect_invalid_json_shape_and_canary(self):
        for kind in ('non200', 'redirect', 'shape', 'json', 'canary'):
            result = await self.run_fixture(kind)
            self.assertFalse(result.passed(100))
            if kind in ('non200', 'redirect'):
                self.assertEqual(result.non_200, result.requests)
            else:
                self.assertGreater(result.invalid_responses, 0)
                self.assertEqual(result.non_200, 0)

    async def test_three_second_deadline_includes_semaphore_queue(self):
        result = await self.run_fixture('timeout', rps=1000, duration_seconds=0.002, concurrency=1)
        self.assertEqual(result.non_200, 2)
        self.assertEqual(result.invalid_responses, 0)
        self.assertLess(result.elapsed_seconds, 3.5)
        self.assertGreaterEqual(result.p99_ms, 2900)


class LiveContractTests(unittest.TestCase):
    def test_shape_rejects_nonfinite_bool_missing_and_false_canary(self):
        body = {'flag': False, 'score': 0.2, 'model': 'mock@revision'}
        self.assertTrue(load.valid_response(body))
        self.assertFalse(load.valid_response(body, canary=True))
        for score in (True, math.nan, math.inf, '0.2', None):
            self.assertFalse(load.valid_response({**body, 'score': score}))
        for candidate in (None, {}, [], {**body, 'flag': 1}, {**body, 'model': ''}):
            self.assertFalse(load.valid_response(candidate))
        result = load.LoadResult(1, 1, 0, 0, 1, 2, 100)
        self.assertFalse(result.passed(100))
        self.assertTrue(replace(result, p99_ms=99.9).passed(100))

    def test_cli_live_uses_env_key_and_60_second_default(self):
        result = load.LoadResult(1, 1, 0, 0, 1, 2, 3)
        with patch.dict(os.environ, {'SAFETY_CLASSIFIER_KEY': 'fixture-auth'}), \
             patch.object(load, 'run_live_load_test', new=AsyncMock(return_value=result)) as live, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(load.main(['--url', 'http://127.0.0.1:8000', '--json']), 0)
        live.assert_awaited_once_with('http://127.0.0.1:8000', 'fixture-auth', 130, 128, 60)
        self.assertNotIn('fixture-auth', output.getvalue())


class DriveTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_and_validation_with_mock_transport(self):
        for kind in ('valid', 'non200', 'invalid', 'canary', 'timeout'):
            seen = []

            class Response:
                status = 503 if kind == 'non200' else 200

                def __init__(self, body):
                    self.body = body

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def json(self):
                    await asyncio.sleep(3.1 if kind == 'timeout' else 0.001)
                    return {'flag': False if kind == 'canary' else self.body['text'] == safety.CANARY,
                            'score': True if kind == 'invalid' else 0.02, 'model': 'mock@fixture'}

            class Client:
                def post(self, endpoint, json, **kwargs):
                    seen.append(json)
                    return Response(json)

            timeout = kind == 'timeout'
            result = await load.drive(Client(), '/v1/safety/csam', 1000, 1 if timeout else 32,
                                      0.002 if timeout else 0.051)
            self.assertEqual(result.requests, 2 if timeout else 51)
            self.assertEqual(result.non_200, result.requests if kind in ('non200', 'timeout') else 0)
            self.assertEqual(result.invalid_responses, 51 if kind == 'invalid' else 2 if kind == 'canary' else 0)
            if kind == 'valid':
                self.assertEqual(sum(r['text'] == safety.CANARY for r in seen), 2)
                self.assertEqual(sum(r['phase'] == 'input' for r in seen), 6)
            if timeout:
                self.assertLess(result.elapsed_seconds, 3.5)


if __name__ == '__main__':
    unittest.main()
