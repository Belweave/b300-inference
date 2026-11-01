"""Stateless node scorers; the gateway owns enforcement and evidence.

Input uses only Shieldstral's minors policy. Output defaults to Qwen3Guard-Gen
as a sexual-category first pass, followed by that same minors policy whenever
Qwen signals sexual content. Qwen alone never blocks; a confirmation reports the
authoritative minors score. Model errors propagate so the relay can fail closed.

The text-in/decision-out contract fits OpenAI-compatible Qwen3Guard-Gen-0.6B
served by SGLang/vLLM. Qwen3Guard-Stream's token-level head needs a custom
per-token harness; it is a future optimization for gateway rolling output calls,
not a drop-in stateless scorer. Neither model's predictions guarantee accuracy;
the policy scope remains ONLY sexual content involving minors.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import math
import re
import time
from typing import Protocol
from urllib.parse import urlsplit
import weakref

from aiohttp import ClientError, ClientTimeout

SYSTEM = ('Judge whether the Document meets the requirements based on the Query and the Instruction provided. '
          'Note that the answer can only be "yes" or "no".')
INSTRUCT = ('Act as a strict child-safety moderator with a low tolerance threshold. The ONLY category in scope '
            'is sexual content involving a minor (a real or fictional person under 18), including content '
            'that sexualizes a minor or requests/solicits such content. Adult sexual content, non-sexual '
            'content about or for children (education, parenting, medical, abuse-prevention, reporting, '
            'survivor support), and all software, security, coding and other topics are NOT violations '
            'and must be judged "no". If prompt context and assistant output are labelled separately, '
            'judge only the assistant output, using the prompt context only to interpret it.')
QUERY = ('Does this content sexualize a minor, depict or describe sexual activity involving a minor, '
         'or request/solicit such content?')
USER_TEMPLATE = '<Instruct>: {instruct}\n\n<Query>: {query}\n\n<Document>: {text}'
POLICY = SYSTEM + '\n\n' + USER_TEMPLATE.format(instruct=INSTRUCT, query=QUERY, text='')
POLICY_VERSION = 'sha256:' + hashlib.sha256(POLICY.encode()).hexdigest()[:16]
CANARY = 'CANARY-CSAM-TEST-7f3a'
_INFLIGHT = weakref.WeakKeyDictionary()


def inflight_semaphore(max_inflight):
    """Share one limit across all scorer instances in the process's serving loop."""
    loop = asyncio.get_running_loop()
    if loop not in _INFLIGHT:
        _INFLIGHT[loop] = asyncio.Semaphore(max_inflight)
    return _INFLIGHT[loop]


def validate_url(value):
    try:
        url = urlsplit(value)
        valid = (url.scheme in ('http', 'https') and url.hostname in ('127.0.0.1', 'localhost')
                 and url.username is None and url.password is None and not url.query and not url.fragment)
        if url.port is not None and not 0 < url.port < 65536:
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('invalid safety URL (must use 127.0.0.1 or localhost)')


@dataclass(frozen=True)
class SafetyConfig:
    enabled: bool = False
    url: str = 'http://127.0.0.1:8600'
    model: str = 'mistralai/Shieldstral-1.0-3B'
    model_revision: str = POLICY_VERSION
    threshold: float = 0.5
    timeout_seconds: float = 3.0
    max_inflight: int = 128
    classifier_key: str = field(default='', repr=False)
    output_guard: str = 'qwen3guard'
    qwen_url: str = 'http://127.0.0.1:8601'
    qwen_model: str = 'Qwen/Qwen3Guard-Gen-0.6B'
    qwen_revision: str = 'unpinned'
    qwen_threshold: float = 0.5

    @property
    def model_id(self):
        return f'{self.model}@{self.model_revision}'

    @property
    def qwen_model_id(self):
        return f'{self.qwen_model}@{self.qwen_revision}'

    @classmethod
    def from_env(cls, env):
        flag = env.get('SAFETY', '0')
        if flag not in ('0', '1'):
            raise ValueError('SAFETY must be 0 or 1')
        values = {'enabled': flag == '1'}
        for name in ('url', 'model', 'model_revision', 'threshold', 'timeout_seconds',
                     'max_inflight', 'classifier_key', 'output_guard', 'qwen_url',
                     'qwen_model', 'qwen_revision', 'qwen_threshold'):
            raw = env.get('SAFETY_' + name.upper(), getattr(cls, name))
            try:
                if name in ('threshold', 'timeout_seconds', 'qwen_threshold'):
                    raw = float(raw)
                elif name == 'max_inflight':
                    raw = int(raw)
                elif (not isinstance(raw, str) or (not raw.strip() and name != 'classifier_key')
                      or any(char in raw for char in ('\0', '\r', '\n'))):
                    raise ValueError
                else:
                    raw.encode('utf-8')
            except (ValueError, TypeError, OverflowError):
                raise ValueError('invalid SAFETY_' + name.upper()) from None
            values[name] = raw
        config = cls(**values)
        validate_url(config.url)
        validate_url(config.qwen_url)
        if config.output_guard not in ('shieldstral', 'qwen3guard'):
            raise ValueError('SAFETY_OUTPUT_GUARD must be shieldstral or qwen3guard')
        if not 0 < config.threshold < 1:
            raise ValueError('SAFETY_THRESHOLD must be between 0 and 1')
        if not 0 < config.qwen_threshold < 1:
            raise ValueError('SAFETY_QWEN_THRESHOLD must be between 0 and 1')
        if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
            raise ValueError('SAFETY_TIMEOUT_SECONDS must be positive and finite')
        if config.max_inflight <= 0:
            raise ValueError('SAFETY_MAX_INFLIGHT must be positive')
        return config


class ClassifierUnavailable(Exception):
    pass


class Classifier(Protocol):
    config: SafetyConfig

    async def score(self, texts: list[str]) -> list[float]: ...

    async def healthy(self) -> bool: ...


@dataclass(frozen=True)
class SafetyScorer:
    config: SafetyConfig
    input_backend: Classifier
    output_backend: Classifier
    confirm_backend: Classifier


def parse_qwen(content: str) -> float:
    """Read Qwen's Safety/Categories format, scoring ONLY its sexual category.

    Use the Gen model card's Safety label regex and category-name matching,
    restricted to the Categories line. Missing Safety is an unavailable result,
    never a benign fallback. Absence of our category always scores zero.
    """
    if not isinstance(content, str):
        raise ClassifierUnavailable('invalid classifier response')
    label = re.search(r'^Safety:[ \t]*(Safe|Unsafe|Controversial)[ \t]*\r?$', content, re.MULTILINE)
    if label is None:
        raise ClassifierUnavailable('invalid classifier response')
    categories = re.search(r'^Categories:[ \t]*(.*)$', content, re.MULTILINE)
    if categories is None or 'Sexual Content or Sexual Acts' not in {
            category.strip() for category in categories.group(1).split(',')}:
        return 0.0
    return {'Unsafe': 1.0, 'Controversial': 0.5, 'Safe': 0.0}[label.group(1)]


def parse_score(payload):
    """Renormalize the best yes/no tokens; never include response data in errors."""
    try:
        entries = payload['choices'][0]['logprobs']['content'][0]['top_logprobs']
        if not isinstance(entries, list):
            raise ValueError
        best = {}
        for entry in entries:
            token = entry['token'].strip().lower()
            value = entry['logprob']
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError
            for label in ('yes', 'no'):
                if token in (label, label + '.', '"' + label + '"', "'" + label + "'"):
                    best[label] = max(best.get(label, -math.inf), value)
        if not best:
            raise ValueError
        yes, no = best.get('yes', -10.0), best.get('no', -10.0)
        peak = max(yes, no)
        return math.exp(yes - peak) / (math.exp(yes - peak) + math.exp(no - peak))
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError):
        raise ClassifierUnavailable('invalid classifier response') from None


class ShieldstralClassifier:
    def __init__(self, config, session):
        self.config, self.session = config, session
        self.inflight = inflight_semaphore(config.max_inflight)
        self.health_until, self.health_ok = 0.0, False
        self.health_lock = asyncio.Lock()

    async def _score(self, text):
        # The endpoint holds the shared semaphore; do not acquire it twice.
        payload = {'model': self.config.model, 'messages': [
            {'role': 'system', 'content': SYSTEM},
            {'role': 'user', 'content': USER_TEMPLATE.format(instruct=INSTRUCT, query=QUERY, text=text)}],
            'max_tokens': 1, 'temperature': 0, 'logprobs': True, 'top_logprobs': 20}
        async with self.session.post(self.config.url.rstrip('/') + '/v1/chat/completions',
                                     json=payload, allow_redirects=False,
                                     timeout=ClientTimeout(total=self.config.timeout_seconds)) as response:
            if response.status != 200:
                raise ClassifierUnavailable('classifier HTTP failure')
            return parse_score(await response.json())

    async def score(self, texts: list[str]) -> list[float]:
        async def one(text):
            try:
                return await asyncio.wait_for(self._score(text), self.config.timeout_seconds)
            except ClassifierUnavailable:
                raise
            except Exception:  # Never expose engine response data or exception details.
                raise ClassifierUnavailable('classifier unavailable') from None
        tasks = [asyncio.create_task(one(text)) for text in texts]
        try:
            return await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _health(self):
        async with self.inflight:
            async with self.session.get(self.config.url.rstrip('/') + '/health', allow_redirects=False,
                                        timeout=ClientTimeout(total=self.config.timeout_seconds)) as response:
                return response.status == 200

    async def healthy(self) -> bool:
        async with self.health_lock:
            if time.monotonic() >= self.health_until:
                try:
                    self.health_ok = await asyncio.wait_for(self._health(), self.config.timeout_seconds)
                except (ClientError, OSError, asyncio.TimeoutError):
                    self.health_ok = False
                self.health_until = time.monotonic() + 5
            return self.health_ok


class Qwen3GuardClassifier(ShieldstralClassifier):
    """Reuse Shieldstral's async deadlines, cancellation and cached health checks."""

    async def _score(self, text):
        # The Gen chat template moderates a response when an assistant turn is
        # present. A user-only message would instead moderate a prompt.
        payload = {'model': self.config.qwen_model, 'messages': [
            {'role': 'user', 'content': 'Please provide a response.'},
            {'role': 'assistant', 'content': text}], 'max_tokens': 64, 'temperature': 0}
        async with self.session.post(self.config.qwen_url.rstrip('/') + '/v1/chat/completions',
                                     json=payload, allow_redirects=False,
                                     timeout=ClientTimeout(total=self.config.timeout_seconds)) as response:
            if response.status != 200:
                raise ClassifierUnavailable('classifier HTTP failure')
            payload = await response.json()
            return parse_qwen(payload['choices'][0]['message']['content'])

    async def _health(self):
        async with self.inflight:
            async with self.session.get(self.config.qwen_url.rstrip('/') + '/health', allow_redirects=False,
                                        timeout=ClientTimeout(total=self.config.timeout_seconds)) as response:
                return response.status == 200


async def _score_one(classifier: Classifier, text: str) -> float:
    scores = await classifier.score([text])
    try:
        if not isinstance(scores, list) or len(scores) != 1:
            raise ValueError
        score = scores[0]
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError
        score = float(score)
    except (ValueError, TypeError, OverflowError):
        raise ClassifierUnavailable('invalid classifier score') from None
    return score


async def score_text(scorer: SafetyScorer, text: str, phase: str) -> tuple[bool, float, str]:
    """Caller holds the shared semaphore and an overall deadline, including confirm."""
    if phase not in ('input', 'output'):
        raise ValueError('invalid safety phase')
    config = scorer.config
    qwen_output = phase == 'output' and config.output_guard == 'qwen3guard'
    model = config.qwen_model_id if qwen_output else config.model_id
    if CANARY in text:
        return True, 1.0, model
    backend = scorer.input_backend if phase == 'input' else scorer.output_backend
    score = await _score_one(backend, text)
    if qwen_output:
        if score < config.qwen_threshold:
            return False, score, model
        score = await _score_one(scorer.confirm_backend, text)
        model += '+confirm:' + config.model_id
    return score >= config.threshold, score, model
