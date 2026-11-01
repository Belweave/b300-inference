"""Relay-only environment and noninteractive classifier-key transport."""
import os


# Keep in sync with bootstrap.sh's relay_safety_env (tested).
SAFETY_ENV = (
    'SAFETY', 'SAFETY_URL', 'SAFETY_MODEL', 'SAFETY_MODEL_REVISION', 'SAFETY_THRESHOLD',
    'SAFETY_TIMEOUT_SECONDS', 'SAFETY_MAX_INFLIGHT', 'SAFETY_CLASSIFIER_KEY',
    'SAFETY_OUTPUT_GUARD', 'SAFETY_QWEN_URL', 'SAFETY_QWEN_MODEL',
    'SAFETY_QWEN_REVISION', 'SAFETY_QWEN_THRESHOLD',
)


def read_classifier_key(stream):
    if stream.isatty():
        raise ValueError('classifier key requires piped stdin; TTY refused')
    key = stream.readline(65538).rstrip('\r\n')
    if not key or not key.strip():
        raise ValueError('classifier key is empty')
    if len(key) > 65536 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError('classifier key must be a single HTTP header value')
    return key


def relay_environment(env, port, engine_port):
    result = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
              'HOME': os.environ.get('HOME', '/'), 'PORT': str(port),
              'ENGINE_PORT': str(engine_port),
              'SSE_HEARTBEAT_SECONDS': env.get('SSE_HEARTBEAT_SECONDS', '15')}
    if env.get('SAFETY') == '1':
        result.update({name: env[name] for name in SAFETY_ENV if name in env})
    return result
