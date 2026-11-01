"""Loopback fake SSE server: verify real token usage, chunking, and failure handling."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
import asyncio

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    aiohttp = None


@unittest.skipIf(aiohttp is None, 'aiohttp not installed')
class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_are_not_tokens(self):
        path = Path(__file__).resolve().parents[1]/'scripts/bench.py'
        spec=importlib.util.spec_from_file_location('bench',path)
        bench=importlib.util.module_from_spec(spec); spec.loader.exec_module(bench)
        async def handler(request):
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await response.prepare(request)
            for text in ['several words in one chunk', 'more text']:
                data=('data: '+json.dumps({'choices':[{'delta':{'content':text}}]})+'\n\n').encode()
                # Split JSON across TCP writes.
                await response.write(data[:12]); await response.write(data[12:])
                await asyncio.sleep(.02)
            await response.write(b'data: {"usage":{"completion_tokens":20,"prompt_tokens":30},"choices":[]}\n\ndata: [DONE]\n\n')
            return response
        app=web.Application(); app.router.add_post('/v1/chat/completions',handler)
        runner=web.AppRunner(app); await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        previous=dict(os.environ)
        os.environ.update(MODEL='fake', ENDPOINT=f'http://127.0.0.1:{port}')
        try:
            async with aiohttp.ClientSession() as session:
                result=await bench.request(session,0)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['output_tokens'],20)
            self.assertEqual(result['sse_content_events'],2)
            self.assertGreater(result['tpot_ms'],0)
        finally:
            os.environ.clear(); os.environ.update(previous)
            await runner.cleanup()
