"""Loopback SSE heartbeat relay: the tunnel's 127.0.0.1:PORT (8000) -> engine 127.0.0.1:ENGINE_PORT.

Cloudflare returns 524 when the origin sends nothing for 100 s. A queued request or a long
prefill can be silent that long, so for streaming requests (JSON body with "stream": true)
this relay guarantees bytes on the wire:
  * if the engine has not sent response headers within HEARTBEAT seconds, it sends
    200 text/event-stream headers itself, then ": keepalive" comments;
  * while the engine body is silent, it sends ": keepalive\\n\\n" every HEARTBEAT seconds,
    only between SSE events (never inside one);
  * engine bytes pass through unbuffered; there is no total-duration timeout;
  * a client disconnect closes the engine connection, which aborts the engine request;
  * an engine error after synthesized headers becomes one `data: {"error": ...}` event.
Everything else (health, models, metrics, non-streaming completions) passes through
unchanged. A non-streaming request whose engine takes >100 s to first byte can still
hit 524 at Cloudflare; clients should use stream=true for long work.
Authorization passes through unchanged; raw keys are never logged or stored.
With SAFETY=1, /v1/safety/csam scores text in-process via the classifier adapter.
The gateway owns enforcement and evidence. Other paths still pass through.
"""
import asyncio
import hmac
import json
import os
import sys

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
       'trailers', 'transfer-encoding', 'upgrade', 'content-length', 'host'}
KEEPALIVE = b': keepalive\n\n'
SSE_HEADERS = {'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache',
               'X-Accel-Buffering': 'no'}


def wants_stream(method, content_type, body):
    if method != 'POST' or 'json' not in (content_type or ''):
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get('stream') is True


def forward_headers(headers):
    return {k: v for k, v in headers.items() if k.lower() not in HOP}


def error_event(status, text):
    try:
        data = json.loads(text)
        error = data.get('error', data) if isinstance(data, dict) else data
    except ValueError:
        error = None
    if not isinstance(error, dict):
        error = {'message': str(error if error is not None else text) or f'engine status {status}',
                 'type': 'engine_error'}
    return b'data: ' + json.dumps({'error': {'code': status, **error}}).encode() + b'\n\n'


def unavailable():
    return web.json_response({'error': {'message': 'engine unavailable', 'type': 'relay_error', 'code': 502}},
                             status=502)


class Relay:
    def __init__(self, upstream, heartbeat=15.0):
        self.upstream = upstream.rstrip('/')
        self.heartbeat = heartbeat
        self.session = None

    async def start(self, app):
        # No total/read timeout: long generations must never be cut by the relay.
        self.session = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10),
                                     connector=TCPConnector(limit=0), auto_decompress=False, trust_env=False)

    async def stop(self, app):
        await self.session.close()

    def request(self, request, body, stream):
        headers = forward_headers(request.headers)
        if stream:
            headers['Accept-Encoding'] = 'identity'  # compressed SSE would hide event boundaries
        return self.session.request(request.method, self.upstream + request.rel_url.path_qs, headers=headers,
                                    data=body or None, allow_redirects=False,
                                    skip_auto_headers=('User-Agent', 'Accept-Encoding', 'Content-Type'))

    async def handle(self, request):
        body = await request.read()
        stream = wants_stream(request.method, request.headers.get('Content-Type'), body)
        if not stream:
            return await self.passthrough(request, body)
        return await self.stream(request, body)

    async def passthrough(self, request, body):
        try:
            resp = await self.request(request, body, False)
        except (ClientError, OSError):
            return unavailable()
        try:
            out = web.StreamResponse(status=resp.status, reason=resp.reason, headers=forward_headers(resp.headers))
            if resp.content_length is not None:
                out.content_length = resp.content_length
            await out.prepare(request)
            async for chunk in resp.content.iter_any():
                await out.write(chunk)
            await out.write_eof()
            return out
        finally:
            resp.close()

    async def stream(self, request, body):
        pending = asyncio.ensure_future(self.request(request, body, True))
        out = resp = read = None
        try:
            done, _ = await asyncio.wait({pending}, timeout=self.heartbeat)
            if not done:
                # Engine is quiet before headers (queued / prefill): answer now, keep the line warm.
                out = web.StreamResponse(status=200, headers=SSE_HEADERS)
                await out.prepare(request)
                await out.write(KEEPALIVE)
                while not done:
                    done, _ = await asyncio.wait({pending}, timeout=self.heartbeat)
                    if not done:
                        await out.write(KEEPALIVE)
            try:
                resp = pending.result()
            except (ClientError, OSError):
                if out is None:
                    return unavailable()
                await out.write(error_event(502, 'engine unavailable'))
                await out.write_eof()
                return out
            event_stream = 'text/event-stream' in resp.headers.get('Content-Type', '')
            if out is None and (resp.status != 200 or not event_stream):
                # Fast non-SSE answer (usually an error): pass it through exactly.
                out = web.StreamResponse(status=resp.status, reason=resp.reason,
                                         headers=forward_headers(resp.headers))
                if resp.content_length is not None:
                    out.content_length = resp.content_length
                await out.prepare(request)
                async for chunk in resp.content.iter_any():
                    await out.write(chunk)
                await out.write_eof()
                return out
            if out is None:
                headers = forward_headers(resp.headers)
                headers.setdefault('Cache-Control', 'no-cache')
                headers['X-Accel-Buffering'] = 'no'
                out = web.StreamResponse(status=resp.status, headers=headers)
                await out.prepare(request)
            elif resp.status != 200 or not event_stream:
                await out.write(error_event(resp.status, (await resp.read()).decode(errors='replace')))
                await out.write_eof()
                return out
            boundary = True  # nothing sent yet, or the last bytes sent ended an event
            tail = b'\n\n'
            while True:
                read = asyncio.ensure_future(resp.content.readany())
                done = set()
                while not done:
                    done, _ = await asyncio.wait({read}, timeout=self.heartbeat)
                    if not done and boundary:
                        await out.write(KEEPALIVE)
                try:
                    chunk = read.result()
                except (ClientError, OSError):
                    if boundary:
                        await out.write(error_event(502, 'engine stream failed'))
                    break
                read = None
                if not chunk:
                    break
                await out.write(chunk)
                tail = (tail + chunk)[-4:]
                boundary = tail.endswith(b'\n\n') or tail.endswith(b'\r\n\r\n')
            await out.write_eof()
            return out
        finally:
            # Client gone (handler cancelled / write failed) or done: close the engine
            # connection so the engine aborts the request instead of generating on.
            for task in (pending, read):
                if task is not None and not task.done():
                    task.cancel()
            if resp is not None:
                resp.close()


class SafetyRelay(Relay):
    def __init__(self, upstream, heartbeat, config, classifier=None):
        super().__init__(upstream, heartbeat)
        import safety
        self.safety, self.config, self.classifier = safety, config, classifier
        self.safety_session = None
        self.inflight = None

    async def start(self, app):
        await super().start(app)
        self.inflight = self.safety.inflight_semaphore(self.config.max_inflight)
        self.safety_session = ClientSession(timeout=ClientTimeout(total=self.config.timeout_seconds),
                                            connector=TCPConnector(limit=0), trust_env=False)
        if self.classifier is None:
            self.classifier = self.safety.ShieldstralClassifier(self.config, self.safety_session)

    async def stop(self, app):
        await self.safety_session.close()
        await super().stop(app)

    async def score(self, request):
        if request.method != 'POST':
            return web.json_response({'error': 'method not allowed'}, status=405, headers={'Allow': 'POST'})
        if self.config.classifier_key:
            expected = ('Bearer ' + self.config.classifier_key).encode('utf-8')
            supplied = request.headers.get('Authorization', '').encode('utf-8', errors='surrogatepass')
            if not hmac.compare_digest(supplied, expected):
                return web.json_response({'error': 'unauthorized'}, status=401)
        try:
            if request.content_type != 'application/json':
                raise ValueError
            body = json.loads((await request.read()).decode('utf-8'))
            if (not isinstance(body, dict) or not isinstance(body.get('text'), str)
                    or body.get('phase') not in ('input', 'output')):
                raise ValueError
            # JSON escapes must also represent valid UTF-8 text.
            body['text'].encode('utf-8')
        except (ValueError, RecursionError, web.HTTPRequestEntityTooLarge):
            return web.json_response({'error': 'invalid request'}, status=400)

        async def bounded_score():
            async with self.inflight:
                return await self.safety.score_text(self.classifier, body['text'], body['phase'])

        try:
            # Queue time counts toward the same per-call deadline as model time.
            flag, score, model = await asyncio.wait_for(bounded_score(), self.config.timeout_seconds)
        except Exception:
            # Includes ClassifierUnavailable/timeouts; never log text, auth or engine errors.
            return web.json_response({'error': 'classifier unavailable'}, status=503)
        return web.json_response({'flag': flag, 'score': score, 'model': model})

    async def unknown_safety_path(self, request):
        return web.json_response({'error': 'not found'}, status=404)


def make_app(upstream, heartbeat=15.0, safety_config=None, classifier=None):
    flag = os.environ.get('SAFETY', '0')
    if flag not in ('0', '1'):
        raise ValueError('SAFETY must be 0 or 1')
    if safety_config is None and flag == '1':
        from safety import SafetyConfig
        safety_config = SafetyConfig.from_env(os.environ)
    if safety_config is not None and safety_config.enabled:
        relay = SafetyRelay(upstream, heartbeat, safety_config, classifier)
    else:
        relay = Relay(upstream, heartbeat)
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app.on_startup.append(relay.start)
    app.on_cleanup.append(relay.stop)
    if isinstance(relay, SafetyRelay):
        app.router.add_route('*', '/v1/safety/csam', relay.score)
        app.router.add_route('*', '/v1/safety/{tail:.*}', relay.unknown_safety_path)
    app.router.add_route('*', '/{tail:.*}', relay.handle)
    return app


async def serve(host, port, upstream, heartbeat):
    runner = web.AppRunner(make_app(upstream, heartbeat), handler_cancellation=True, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port, reuse_address=True).start()
    return runner


def main():
    port = int(os.environ.get('PORT', '8000'))
    engine = int(os.environ.get('ENGINE_PORT', '8001'))
    if port == engine:
        raise SystemExit('ENGINE_PORT must differ from PORT when the SSE relay is enabled')
    heartbeat = float(os.environ.get('SSE_HEARTBEAT_SECONDS', '15'))
    if not 1 <= heartbeat <= 60:
        raise SystemExit('SSE_HEARTBEAT_SECONDS must be 1..60 (Cloudflare cuts at 100 s of silence)')

    async def run():
        await serve('127.0.0.1', port, f'http://127.0.0.1:{engine}', heartbeat)
        print(f'sse_relay listening 127.0.0.1:{port} -> 127.0.0.1:{engine}, heartbeat {heartbeat}s', flush=True)
        await asyncio.Event().wait()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    sys.exit(main())
