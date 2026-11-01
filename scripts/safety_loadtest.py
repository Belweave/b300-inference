"""Offline endpoint load test with simulated classifier latency and benign text only."""
import argparse
import asyncio
from dataclasses import dataclass
import math
import random

from aiohttp import ClientError, ClientTimeout, TCPConnector
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
        return self.non_200 == 0 and self.invalid_responses == 0 and self.p99_ms <= max_p99_ms


async def run_load_test(rps=130.0, concurrency=128, duration_seconds=5.0,
                        mock_latency_ms=5.0, mock_p99_ms=15.0):
    if not all(math.isfinite(value) and value > 0 for value in (rps, duration_seconds)):
        raise ValueError('rps and duration must be positive and finite')
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
        raise ValueError('concurrency must be a positive integer')
    config = safety.SafetyConfig(enabled=True, model='mock', model_revision='offline-v1')
    classifier = MockClassifier(config, mock_latency_ms, mock_p99_ms)
    app = sse_relay.make_app('http://127.0.0.1:1', safety_config=config, classifier=classifier)
    server = TestServer(app, host='127.0.0.1', handler_cancellation=True)
    client = TestClient(server, connector=TCPConnector(limit=concurrency),
                        timeout=ClientTimeout(total=config.timeout_seconds + 1), trust_env=False)
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
        try:
            async with slots:
                async with client.post('/v1/safety/csam', json={'text': text, 'phase': phase}) as response:
                    if response.status != 200:
                        non_200 += 1
                        await response.read()
                    else:
                        body = await response.json()
                        expected = {'flag': canary, 'score': 1.0 if canary else 0.01, 'model': config.model_id}
                        if body != expected:
                            invalid_responses += 1
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            non_200 += 1
        finally:
            # Include scheduler lag and client queueing, so overload cannot hide in p99.
            latencies.append((loop.time() - scheduled_at) * 1000)

    tasks = []
    async with client:
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rps', type=float, default=130)
    parser.add_argument('--concurrency', type=int, default=128)
    parser.add_argument('--duration-seconds', type=float, default=5)
    parser.add_argument('--mock-latency-ms', type=float, default=5,
                        help='median simulated model latency (default: 5 ms)')
    parser.add_argument('--mock-p99-ms', type=float, default=15,
                        help='p99 simulated model latency (default: 15 ms)')
    parser.add_argument('--max-p99-ms', type=float, default=100)
    args = parser.parse_args(argv)
    if not math.isfinite(args.max_p99_ms) or args.max_p99_ms <= 0:
        parser.error('max-p99-ms must be positive and finite')
    try:
        result = asyncio.run(run_load_test(args.rps, args.concurrency, args.duration_seconds,
                                          args.mock_latency_ms, args.mock_p99_ms))
    except ValueError as error:
        parser.error(str(error))
    print(f'requests={result.requests} throughput={result.throughput:.1f}/s '
          f'p50={result.p50_ms:.2f}ms p95={result.p95_ms:.2f}ms p99={result.p99_ms:.2f}ms '
          f'non_200={result.non_200} invalid_responses={result.invalid_responses}')
    return 0 if result.passed(args.max_p99_ms) else 1


if __name__ == '__main__':
    raise SystemExit(main())
