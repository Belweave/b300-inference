"""Filter secret values before subprocess output is written to disk."""
import os
import re
import sys

API_KEY = re.compile(r'''(?i)(["']?(?:api[_-]?key|admin_api_key)["']?\s*[:=]\s*)(?:\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s,}\])]+)''')
BEARER = re.compile(r'''(?i)(Bearer\s+)[^\s"',;]+''')


def secret_values(env=None):
    env = os.environ if env is None else env
    return sorted({v for k, v in env.items() if v and any(s in k for s in ('TOKEN', 'KEY', 'PASSWORD', 'SECRET'))}, key=len, reverse=True)


def redact(line, secrets):
    for value in secrets:
        line = line.replace(value, '[REDACTED]')
    line = API_KEY.sub(r'\1[REDACTED]', line)
    return BEARER.sub(r'\1[REDACTED]', line)


if __name__ == '__main__':
    secrets = secret_values()
    for line in sys.stdin:
        sys.stdout.write(redact(line, secrets))
        sys.stdout.flush()
