"""Filter secret values before subprocess output is written to disk."""
import os
import json
import re
import sys

API_KEY = re.compile(r'''(?i)(["']?(?:api[_-]?key|admin_api_key)["']?\s*[:=]\s*)(?:\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s,}\])]+)''')
BEARER = re.compile(r'''(?i)(Bearer\s+)[^\s"',;]+''')
# Rotated tokens may occur in interleaved replica logs without matching current env.
CLOUDFLARE_TOKEN = re.compile(r'\beyJ[A-Za-z0-9_+/\-]{40,}={0,2}')


class SecretValue(str):
    def __repr__(self):
        return '[REDACTED]'


def secret_values(env=None):
    env = os.environ if env is None else env
    values = {v for k, v in env.items() if v and any(s in k for s in ('TOKEN', 'KEY', 'PASSWORD', 'SECRET'))}
    header = env.get('MIRROR_AUTH_HEADER')
    if header:
        values.add(header)
        # Also mask the value when an HTTP library prints a headers mapping.
        value = header.partition(':')[2].strip(' \t')
        if value: values.add(value)
    return [SecretValue(v) for v in sorted(values, key=len, reverse=True)]


def redact(line, secrets):
    for value in secrets:
        # Timing JSON and exception reprs may escape quotes, tabs or newlines.
        forms = {str(value), json.dumps(str(value))[1:-1], repr(str(value))[1:-1]}
        for form in sorted(forms, key=len, reverse=True):
            line = line.replace(form, '[REDACTED]')
    line = API_KEY.sub(r'\1[REDACTED]', line)
    line = BEARER.sub(r'\1[REDACTED]', line)
    return CLOUDFLARE_TOKEN.sub('[REDACTED]', line)


if __name__ == '__main__':
    secrets = secret_values()
    for line in sys.stdin:
        sys.stdout.write(redact(line, secrets))
        sys.stdout.flush()
