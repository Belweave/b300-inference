"""Authenticated loopback requests without keys in argv or output."""
import os
import sys
import urllib.request
req = urllib.request.Request(os.environ['ENDPOINT'] + sys.argv[1], headers={
    'Authorization': 'Bearer ' + os.environ.get('INFER_UPSTREAM_KEY', '')})
with urllib.request.urlopen(req, timeout=5) as response:
    sys.stdout.buffer.write(response.read())
