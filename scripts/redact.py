"""Filter secret values before subprocess output is written to disk."""
import os
import sys
secrets = [v for k, v in os.environ.items() if v and any(s in k for s in ('TOKEN', 'KEY', 'PASSWORD', 'SECRET'))]
for line in sys.stdin:
    for value in sorted(secrets, key=len, reverse=True):
        line = line.replace(value, '[REDACTED]')
    sys.stdout.write(line)
    sys.stdout.flush()
