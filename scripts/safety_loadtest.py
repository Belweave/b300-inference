"""Benign endpoint load test: offline mocks by default, --url for a live relay."""
import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import math
import os
import random

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from aiohttp.test_utils import TestClient, TestServer

import safety
import sse_relay


class MockClassifier:
    def __init__(self, config, latency_ms=5.0, p99_ms=15.0):
        if not (math.isfinite(latency_ms) and math.isfinite(p99_ms) and 0 < latency_ms <= p99_ms):
            raise ValueError('mock latencies must be finite with 0 < latency <= p99')
        self.config = config
        self.random = random.Random(0)
        self.mu = math.log(latency_ms / 1000)
        self.sigma = math.log(p99_ms / latency_ms) / 2.326347874

    async def score(self, texts):
        await asyncio.sleep(self.random.lognormvariate(self.mu, self.sigma))
        return [0.01 for _ in texts]

    async def healthy(self):
        return True


def percentile(samples, fraction):
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


@dataclass(frozen=True)
class LoadResult:
    requests: int
    elapsed_seconds: float
    non_200: int
    invalid_responses: int
    p50_ms: float
    p95_ms: float
    p99_ms: float

    @property
    def throughput(self):
        return self.requests / self.elapsed_seconds

    def passed(self, max_p99_ms):
        return self.non_200 == 0 and self.invalid_responses == 0 and self.p99_ms < max_p99_ms


def valid_response(body, canary=False):
    """Contract only: live scores and benign flags are not deterministic."""
    try:
        return (isinstance(body, dict) and set(body) == {'flag', 'score', 'model'}
                and type(body['flag']) is bool
                and (not canary or body['flag'])
                and type(body['score']) in (int, float) and math.isfinite(body['score'])
                and isinstance(body['model'], str) and bool(body['model'].strip()))
    except (ValueError, OverflowError):
        return False


async def drive(client, endpoint, rps, concurrency, duration_seconds, config=None):
    if not all(math.isfinite(value) and value > 0 for value in (rps, duration_seconds)):
        raise ValueError('rps and duration must be positive and finite')
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
        raise ValueError('concurrency must be a positive integer')
    slots = asyncio.Semaphore(concurrency)
    latencies = []
    non_200 = invalid_responses = 0
    loop = asyncio.get_running_loop()

    async def request(index, scheduled_at):
        nonlocal non_200, invalid_responses
        canary = index % 50 == 0
        phase = 'input' if index % 10 == 0 else 'output'
        size = 16000 if phase == 'input' else 640
        text = safety.CANARY if canary else ('hello world ' * math.ceil(size / 12))[:size]

        async def send():
            async with slots:
                async with client.post(endpoint, json={'text': text, 'phase': phase},
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        return 'non_200'
                    try:
                        body = await response.json()
                    except (ValueError, ClientError):
                        return 'invalid'
                    valid = valid_response(body, canary)
                    if config is not None:  # Preserve exact offline mock assertions.
                        model = config.model_id if phase == 'input' else config.qwen_model_id
                        if phase == 'output' and not canary:
                            model += '+confirm:' + config.model_id
                        valid = valid and body == {'flag': canary, 'score': 1.0 if canary else 0.01, 'model': model}
                    return 'ok' if valid else 'invalid'
        try:
            # Scheduling lag, semaphore queueing and response decoding share 3 seconds.
            remaining = 3 - (loop.time() - scheduled_at)
            if remaining <= 0:
                raise asyncio.TimeoutError
            outcome = await asyncio.wait_for(send(), remaining)
            non_200 += outcome == 'non_200'
            invalid_responses += outcome == 'invalid'
        except (ClientError, OSError, asyncio.TimeoutError):
            non_200 += 1
        finally:
            latencies.append((loop.time() - scheduled_at) * 1000)

    tasks = []
    started = loop.time()
    try:
        for index in range(math.ceil(rps * duration_seconds)):
            scheduled_at = started + index / rps
            await asyncio.sleep(max(0, scheduled_at - loop.time()))
            tasks.append(asyncio.create_task(request(index, scheduled_at)))
        await asyncio.gather(*tasks)
        elapsed = max(duration_seconds, loop.time() - started)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return LoadResult(len(latencies), elapsed, non_200, invalid_responses,
                      *(percentile(latencies, fraction) for fraction in (0.50, 0.95, 0.99)))


async def run_load_test(rps=130.0, concurrency=128, duration_seconds=5.0,
                        mock_latency_ms=5.0, mock_p99_ms=15.0):
    config = safety.SafetyConfig(enabled=True, model='mock', model_revision='offline-v1')
    classifier = MockClassifier(config, mock_latency_ms, mock_p99_ms)
    scorer = safety.SafetyScorer(config, classifier, classifier, classifier)
    app = sse_relay.make_app('http://127.0.0.1:1', safety_config=config, scorer=scorer)
    server = TestServer(app, host='127.0.0.1', handler_cancellation=True)
    async with TestClient(server, connector=TCPConnector(limit=concurrency),
                          timeout=ClientTimeout(total=3), trust_env=False) as client:
        return await drive(client, '/v1/safety/csam', rps, concurrency, duration_seconds, config)


async def run_live_load_test(url, key, rps=130.0, concurrency=128, duration_seconds=60.0):
    safety.validate_url(url)
    if not key or any(c in key for c in ('\r', '\n', '\0')):
        raise ValueError('SAFETY_CLASSIFIER_KEY must be nonempty and single-line')
    async with ClientSession(headers={'Authorization': 'Bearer ' + key},
                             connector=TCPConnector(limit=concurrency),
                             timeout=ClientTimeout(total=3), trust_env=False) as client:
        return await drive(client, url.rstrip('/') + '/v1/safety/csam', rps, concurrency, duration_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rps', type=float, default=130)
    parser.add_argument('--concurrency', type=int, default=128)
    parser.add_argument('--duration-seconds', type=float, help='default: 5 offline, 60 live')
    parser.add_argument('--url', help='live loopback relay URL; key from SAFETY_CLASSIFIER_KEY only')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--mock-latency-ms', type=float, default=5,
                        help='median simulated model latency (default: 5 ms)')
    parser.add_argument('--mock-p99-ms', type=float, default=15,
                        help='p99 simulated model latency (default: 15 ms)')
    parser.add_argument('--max-p99-ms', type=float, default=100)
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_p99_ms) or args.max_p99_ms <= 0:
        parser.error('max-p99-ms must be positive and finite')
    try:
        duration = args.duration_seconds if args.duration_seconds is not None else (60 if args.url else 5)
        if args.url:
            result = asyncio.run(run_live_load_test(args.url, os.environ.get('SAFETY_CLASSIFIER_KEY', ''),
                                                   args.rps, args.concurrency, duration))
        else:
            result = asyncio.run(run_load_test(args.rps, args.concurrency, duration,
                                              args.mock_latency_ms, args.mock_p99_ms))
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps({**asdict(result), 'invalid': result.invalid_responses,
                          'throughput': result.throughput, 'passed': result.passed(args.max_p99_ms)}))
        return 0 if result.passed(args.max_p99_ms) else 1
    print(f'requests={result.requests} throughput={result.throughput:.1f}/s '
          f'p50={result.p50_ms:.2f}ms p95={result.p95_ms:.2f}ms p99={result.p99_ms:.2f}ms '
          f'non_200={result.non_200} invalid_responses={result.invalid_responses}')
    return 0 if result.passed(args.max_p99_ms) else 1


if __name__ == '__main__':
    raise SystemExit(main())
