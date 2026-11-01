"""SSE heartbeat relay: keepalives during engine silence, pass-through, errors, disconnects."""
import asyncio
import json
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
try:
    import aiohttp
    from aiohttp import web
    import sse_relay
except ImportError:  # pragma: no cover - node image always has aiohttp
    aiohttp = None

HB = 0.2


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def event(n):
    return b'data: ' + json.dumps({'choices': [{'delta': {'content': str(n)}}]}).encode() + b'\n\n'


@unittest.skipIf(aiohttp is None, 'aiohttp not installed')
class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.cancelled = asyncio.Event()

        async def chat(request):
            body = await request.json()
            self.seen.append((dict(request.headers), body))
            mode = body.get('mode')
            if mode in ('late_headers', 'late_error'):
                await asyncio.sleep(HB*4)
            if mode in ('error', 'late_error'):
                return web.json_response({'error': {'message': 'too long', 'type': 'BadRequestError'}}, status=400)
            if not body.get('stream'):
                await asyncio.sleep(HB*3)
                return web.json_response({'choices': [{'message': {'content': 'hi'}}], 'usage': {'completion_tokens': 1}})
            out = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await out.prepare(request)
            try:
                if mode == 'silent':
                    await asyncio.sleep(HB*4)
                if mode == 'split':
                    await out.write(b'data: {"a":')
                    await asyncio.sleep(HB*4)
                    await out.write(b'1}\n\n')
                if mode == 'forever':
                    for n in range(10_000):
                        await out.write(event(n)); await asyncio.sleep(HB/4)
                for n in range(3):
                    await out.write(event(n))
                await out.write(b'data: [DONE]\n\n')
            except asyncio.CancelledError:
                self.cancelled.set(); raise
            return out

        async def health(request):
            return web.Response(text='ok')

        engine = web.Application()
        engine.router.add_post('/v1/chat/completions', chat)
        engine.router.add_get('/health', health)
        self.engine_port = free_port()
        self.engine = web.AppRunner(engine, handler_cancellation=True)
        await self.engine.setup()
        await web.TCPSite(self.engine, '127.0.0.1', self.engine_port).start()
        self.port = free_port()
        self.relay = await sse_relay.serve('127.0.0.1', self.port, f'http://127.0.0.1:{self.engine_port}', HB)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self.url = f'http://127.0.0.1:{self.port}'

    async def asyncTearDown(self):
        await self.client.close()
        await self.relay.cleanup()
        await self.engine.cleanup()

    async def post(self, **body):
        async with self.client.post(self.url+'/v1/chat/completions', json=body,
                                    headers={'Authorization': 'Bearer test-only'}) as r:
            return r.status, r.headers.get('Content-Type', ''), await r.read()

    async def test_engine_silent_before_headers_gets_synthesized_headers_and_keepalives(self):
        status, ctype, data = await self.post(stream=True, mode='late_headers')
        self.assertEqual(status, 200)
        self.assertIn('text/event-stream', ctype)
        self.assertTrue(data.startswith(b': keepalive\n\n'))
        self.assertGreaterEqual(data.count(b': keepalive'), 2)
        self.assertTrue(data.endswith(event(2)+b'data: [DONE]\n\n'))

    async def test_silent_body_gets_keepalives_between_events_only(self):
        status, _, data = await self.post(stream=True, mode='silent')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data.count(b': keepalive\n\n'), 2)
        status, _, data = await self.post(stream=True, mode='split')
        self.assertIn(b'data: {"a":1}\n\n', data)  # never injected inside an event

    async def test_fast_stream_is_byte_identical(self):
        status, _, data = await self.post(stream=True)
        self.assertEqual(data, event(0)+event(1)+event(2)+b'data: [DONE]\n\n')
        headers = self.seen[-1][0]
        self.assertEqual(headers['Authorization'], 'Bearer test-only')
        self.assertEqual(headers['Accept-Encoding'], 'identity')

    async def test_errors_keep_status_when_fast_and_become_events_when_late(self):
        status, ctype, data = await self.post(stream=True, mode='error')
        self.assertEqual((status, json.loads(data)['error']['message']), (400, 'too long'))
        status, ctype, data = await self.post(stream=True, mode='late_error')
        self.assertEqual(status, 200)
        last = [l for l in data.split(b'\n\n') if l.startswith(b'data:')][-1]
        self.assertEqual(json.loads(last[5:])['error'], {'code': 400, 'message': 'too long', 'type': 'BadRequestError'})

    async def test_non_stream_and_health_pass_through(self):
        status, ctype, data = await self.post(stream=False)
        self.assertEqual((status, json.loads(data)['usage']['completion_tokens']), (200, 1))
        self.assertNotIn(b'keepalive', data)
        async with self.client.get(self.url+'/health') as r:
            self.assertEqual((r.status, await r.text()), (200, 'ok'))

    async def test_engine_down_is_502_for_the_gateway_no_node_path(self):
        await self.engine.cleanup()
        status, _, data = await self.post(stream=True)
        self.assertEqual(status, 502)
        async with self.client.get(self.url+'/health') as r:
            self.assertEqual(r.status, 502)
        self.engine = web.AppRunner(web.Application()); await self.engine.setup()

    async def test_client_disconnect_aborts_engine_request(self):
        async with self.client.post(self.url+'/v1/chat/completions', json={'stream': True, 'mode': 'forever'}) as r:
            await r.content.readany()
        await asyncio.wait_for(self.cancelled.wait(), 5)

    def test_wants_stream_only_for_json_stream_true(self):
        self.assertTrue(sse_relay.wants_stream('POST', 'application/json', b'{"stream": true}'))
        for args in (('GET', 'application/json', b'{"stream": true}'), ('POST', 'text/plain', b'{"stream": true}'),
                     ('POST', 'application/json', b'{"stream": "yes"}'), ('POST', 'application/json', b'not json'),
                     ('POST', 'application/json', b'[1]')):
            self.assertFalse(sse_relay.wants_stream(*args))


if __name__ == '__main__':
    unittest.main()
