"""Wait for running AND queued requests, never treat unknown metrics as zero."""
import os
import re
import time
import urllib.request

deadline = time.monotonic() + int(os.environ.get('DRAIN_SECONDS', '120'))
zeros = 0
while time.monotonic() < deadline:
    try:
        req = urllib.request.Request(os.environ['ENDPOINT']+'/metrics', headers={
            'Authorization': 'Bearer '+os.environ.get('INFER_UPSTREAM_KEY', '')})
        with urllib.request.urlopen(req, timeout=3) as response:
            text = response.read().decode()
        names = ('sglang:num_running_reqs', 'sglang:num_queue_reqs') if os.environ['ENGINE']=='sglang' else ('vllm:num_requests_running', 'vllm:num_requests_waiting')
        totals = []
        for name in names:
            values = re.findall(r'^'+re.escape(name)+r'(?:\{[^\n]*\})?\s+([0-9.eE+-]+)', text, re.M)
            if not values:
                raise ValueError('Missing metric')
            totals.append(sum(map(float, values)))
        zeros = zeros + 1 if sum(totals)==0 else 0
        if zeros >= 3:
            print('Running and queued requests are zero for three samples')
            break
    except Exception:
        zeros = 0
    time.sleep(min(2, max(0, deadline-time.monotonic())))
else:
    print('Drain bound reached (active requests or unavailable metrics)')
