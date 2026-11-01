"""Authenticated loopback requests without keys in argv or output."""
import os
import sys
import urllib.request
headers = {} if sys.argv[1] == '/health' else {'Authorization': 'Bearer ' + os.environ.get('INFER_UPSTREAM_KEY', '')}
req = urllib.request.Request(os.environ['ENDPOINT'] + sys.argv[1], headers=headers)
with urllib.request.urlopen(req, timeout=5) as response:
    sys.stdout.buffer.write(response.read())
