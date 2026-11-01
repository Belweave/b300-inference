"""Opt-in child-safety gate. Evidence leaves memory only through the age OS binary.

Node-local spool is a staging copy; durable write-once storage and the 90-day-purge
exemption belong to the gateway/R2 (Netops). No private keys are read or generated.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
import tempfile
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
LOG = logging.getLogger(__name__)
_INFLIGHT = weakref.WeakKeyDictionary()


def validate_url(value, loopback=False):
    try:
        url = urlsplit(value)
        valid = (url.scheme in ('http', 'https') and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment)
        if url.port is not None and not 0 < url.port < 65536:
            valid = False
        if loopback and url.hostname not in ('127.0.0.1', 'localhost'):
            valid = False
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('invalid safety URL' + (' (must use 127.0.0.1 or localhost)' if loopback else ''))


@dataclass(frozen=True)
class SafetyConfig:
    enabled: bool = False
    url: str = 'http://127.0.0.1:8600'
    model: str = 'mistralai/Shieldstral-1.0-3B'
    threshold: float = 0.5
    chunk_tokens: int = 64
    window_chars: int = 16000
    overlap_chars: int = 1000
    timeout_seconds: float = 5
    max_inflight: int = 64
    spool_dir: str = '/data/safety-spool'
    age_recipients_file: str = '/etc/b300/safety/recipients.txt'
    age_bin: str = 'age'
    incident_webhook: str = ''
    policy_version: str = field(default=POLICY_VERSION, init=False)

    @classmethod
    def from_env(cls, env):
        flag = env.get('SAFETY', '0')
        if flag not in ('0', '1'):
            raise ValueError('SAFETY must be 0 or 1')
        values = {'enabled': flag == '1'}
        for name in ('url', 'model', 'threshold', 'chunk_tokens', 'window_chars', 'overlap_chars',
                     'timeout_seconds', 'max_inflight', 'spool_dir', 'age_recipients_file',
                     'age_bin', 'incident_webhook'):
            raw = env.get('SAFETY_' + name.upper(), getattr(cls, name))
            if name in ('threshold', 'timeout_seconds'):
                raw = float(raw)
            elif name in ('chunk_tokens', 'window_chars', 'overlap_chars', 'max_inflight'):
                raw = int(raw)
            elif not isinstance(raw, str) or (not raw.strip() and name != 'incident_webhook') or '\0' in raw:
                raise ValueError('invalid SAFETY_' + name.upper())
            values[name] = raw
        config = cls(**values)
        validate_url(config.url, loopback=True)
        if config.incident_webhook:
            validate_url(config.incident_webhook)
        if not 0 < config.threshold < 1:
            raise ValueError('SAFETY_THRESHOLD must be between 0 and 1')
        if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
            raise ValueError('SAFETY_TIMEOUT_SECONDS must be positive and finite')
        if min(config.chunk_tokens, config.window_chars, config.max_inflight) <= 0:
            raise ValueError('safety chunk, window and inflight limits must be positive')
        if not 0 <= config.overlap_chars < config.window_chars:
            raise ValueError('SAFETY_OVERLAP_CHARS must be nonnegative and smaller than the window')
        return config


class ClassifierUnavailable(Exception):
    pass


class Classifier(Protocol):
    config: SafetyConfig

    async def score(self, texts: list[str]) -> list[float]: ...

    async def healthy(self) -> bool: ...


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
        # One semaphore for all classifier instances in the process's serving loop.
        loop = asyncio.get_running_loop()
        self.inflight = _INFLIGHT.setdefault(loop, asyncio.Semaphore(config.max_inflight))
        self.health_until, self.health_ok = 0.0, False
        self.health_lock = asyncio.Lock()

    async def _score(self, text):
        async with self.inflight:
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
            except (ClientError, OSError, asyncio.TimeoutError, ValueError, UnicodeError):
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


def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    return []


def message_text(message):
    content = message.get('content', '')
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                parts.extend(strings(part.get('text', '')))
            else:
                parts.append('[non-text content part]')
    # Request fields are untrusted text; generated reasoning and tool arguments
    # are also checked on the output path, just like assistant content.
    for name in ('reasoning', 'reasoning_content', 'tool_calls', 'function_call'):
        parts.extend(strings(message.get(name)))
    return '\n'.join(part for part in parts if part)


def request_texts(path, body) -> list[str]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        return []
    if not isinstance(data, dict):
        return []
    texts = []
    if path == '/v1/chat/completions':
        messages = data.get('messages', [])
        if not isinstance(messages, list):
            return []
        texts = [str(message.get('role', '')) + ': ' + message_text(message)
                 for message in messages if isinstance(message, dict)]
    elif path == '/v1/completions':
        prompt = data.get('prompt')
        if isinstance(prompt, str):
            texts = [prompt]
        elif isinstance(prompt, list):
            texts = ['\n'.join(text for text in prompt if isinstance(text, str))]
    else:
        return []
    for name in ('reasoning', 'reasoning_content', 'tools', 'tool_calls', 'tool_choice', 'functions', 'function_call'):
        extra = strings(data.get(name))
        if extra:
            texts.append(name + ': ' + '\n'.join(extra))
    return texts


def completion_texts(body):
    try:
        data = json.loads(body)
        choices = data['choices']
        if not isinstance(choices, list):
            raise ValueError
        texts = [message_text(choice['message']) if isinstance(choice.get('message'), dict)
                 else choice.get('text', '') for choice in choices]
        if any(not isinstance(text, str) for text in texts):
            raise ValueError
        return texts
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        raise ClassifierUnavailable('unreadable completion') from None


def windows(text, config):
    step = config.window_chars - config.overlap_chars
    for start in range(0, len(text), step):
        yield text[start:start + config.window_chars]
        if start + config.window_chars >= len(text):
            break


@dataclass
class SafetyVerdict:
    hit: bool
    score: float
    stage: str
    policy_version: str
    model: str
    reason: str = ''
    window: int | None = None

    @property
    def blocked(self):
        return self.hit or self.reason == 'classifier_unavailable'


def verdict(config, score=0.0, stage='input', reason='', window=None):
    hit = score >= config.threshold and reason != 'classifier_unavailable'
    return SafetyVerdict(hit, score, stage, config.policy_version, config.model,
                         'hit' if hit else reason, window)


async def enforce_or_fail(classifier, check, stage='input'):
    """Enabled gates fail closed; an outage is a safety failure, never a policy hit."""
    try:
        if not await classifier.healthy():
            raise ClassifierUnavailable
        return await check()
    except ClassifierUnavailable:
        return verdict(classifier.config, stage=stage, reason='classifier_unavailable')


async def checked_scores(classifier, texts):
    scores = await classifier.score(texts)
    if len(scores) != len(texts) or any(isinstance(s, bool) or not isinstance(s, (int, float))
                                      or not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
        raise ClassifierUnavailable('invalid scores')
    return scores


async def check_input(classifier, texts, stage='input') -> SafetyVerdict:
    async def check():
        chunks = [chunk for text in texts for chunk in windows(text, classifier.config)]
        if not chunks:
            return verdict(classifier.config, stage=stage)
        scores = await checked_scores(classifier, chunks)
        index = max(range(len(scores)), key=scores.__getitem__)
        return verdict(classifier.config, scores[index], stage, window=index)
    return await enforce_or_fail(classifier, check, stage)


class OutputScanner:
    def __init__(self, classifier, config, prompt_context):
        self.classifier, self.config = classifier, config
        self.prompt_context = prompt_context[-2000:]
        self.text, self.scored = '', 0
        self.arguments, self.argument_scored = {}, {}
        self.block = None

    async def feed(self, delta_dict) -> SafetyVerdict | None:
        self.text += message_text(delta_dict)
        # Parallel tool calls may interleave fragments; retain each argument stream
        # as well as the complete generated text so its continuity cannot be lost.
        calls = delta_dict.get('tool_calls') or []
        for index, call in enumerate(calls):
            if isinstance(call, dict) and isinstance(call.get('function'), dict):
                key = call.get('index', index)
                args = ''.join(strings(call['function'].get('arguments')))
                self.arguments[key] = self.arguments.get(key, '') + args
        if math.ceil((len(self.text) - self.scored) / 4) >= self.config.chunk_tokens:
            return await self.flush()
        return self.block

    async def flush(self) -> SafetyVerdict | None:
        if self.block or self.scored == len(self.text):
            return self.block

        async def check():
            # Label context separately: only generated assistant text is judged.
            text = self.text[max(0, self.scored - self.config.overlap_chars):]
            portions = [text]
            if len(self.arguments) > 1:
                for key, args in self.arguments.items():
                    scored = self.argument_scored.get(key, 0)
                    if len(args) > scored:
                        portions.append(args[max(0, scored - self.config.overlap_chars):])
            chunks = ['Prompt context (context only):\n' + self.prompt_context
                      + '\nAssistant output (judge only this portion):\n' + chunk
                      for portion in portions for chunk in windows(portion, self.config)]
            scores = await checked_scores(self.classifier, chunks)
            index = max(range(len(scores)), key=scores.__getitem__)
            return verdict(self.config, scores[index], 'output', window=index)
        result = await enforce_or_fail(self.classifier, check, 'output')
        self.scored = len(self.text)
        self.argument_scored = {key: len(args) for key, args in self.arguments.items()}
        if result.blocked:
            self.block = result
        return self.block


def incident_id():
    return 'inc_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + secrets.token_hex(4)


async def seal_incident(config, incident) -> str:
    ident = incident_id()
    record = {**incident, 'incident_id': ident,
              'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'threshold': config.threshold, 'policy_version': config.policy_version, 'model': config.model}
    incident['sealing_failed'] = True
    process = None
    staging = None
    try:
        # Only public recipients; age never receives an identity/private-key argument.
        recipients = await asyncio.to_thread(Path(config.age_recipients_file).read_text)
        if not any(line.strip() and not line.lstrip().startswith('#') for line in recipients.splitlines()):
            raise OSError('no recipients')
        spool = Path(config.spool_dir)
        await asyncio.to_thread(spool.mkdir, parents=True, exist_ok=True, mode=0o700)
        staging = await asyncio.to_thread(tempfile.TemporaryDirectory, prefix='.seal-', dir=spool)
        encrypted = Path(staging.name) / (ident + '.age')
        process = await asyncio.create_subprocess_exec(
            config.age_bin, '-R', config.age_recipients_file, '-o', str(encrypted),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(process.communicate(json.dumps(record).encode()), config.timeout_seconds)
        if process.returncode != 0:
            raise OSError('age failed')
        await asyncio.to_thread(encrypted.chmod, 0o600)
        # Publish ciphertext atomically without replacing any existing incident.
        await asyncio.to_thread(os.link, encrypted, spool / (ident + '.age'))
        incident['sealing_failed'] = False
    except (OSError, ValueError, asyncio.TimeoutError):
        pass  # Still block and notify; neither plaintext nor subprocess diagnostics go to disk/logs.
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if staging is not None:
            await asyncio.to_thread(staging.cleanup)
        log = LOG.warning if incident['sealing_failed'] else LOG.info
        log('incident_id=%s stage=%s score=%s policy_version=%s', ident, record.get('stage'),
            record.get('score'), config.policy_version)
    return ident


async def notify_incident(config, session, ident):
    if config.incident_webhook:
        try:
            async with session.post(config.incident_webhook, json={'incident_id': ident},
                                    timeout=ClientTimeout(total=config.timeout_seconds), allow_redirects=False):
                pass
        except (ClientError, OSError, asyncio.TimeoutError):
            pass
