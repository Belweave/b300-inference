"""Verify public health + protected model listing on the engine's main port."""
import json
import os
import urllib.error
import urllib.request


def check(endpoint, key, model):
    # Intentionally no Authorization header: this is the Netops relay health contract.
    with urllib.request.urlopen(endpoint+'/health', timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('Health is not ready')
    try:
        with urllib.request.urlopen(endpoint+'/v1/models', timeout=5):
            raise RuntimeError('Unauthenticated model listing unexpectedly succeeded')
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code != 401:
            raise RuntimeError('Expected 401 for unauthenticated model listing') from None
    req = urllib.request.Request(endpoint+'/v1/models', headers={'Authorization': 'Bearer '+key})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.load(response)
    if model not in [x['id'] for x in data.get('data', [])]:
        raise RuntimeError('Authenticated model listing does not contain the configured model')


if __name__ == '__main__':
    check(os.environ['ENDPOINT'], os.environ['INFER_UPSTREAM_KEY'], os.environ['MODEL'])
    print('Readiness passed: public /health; protected /v1/models; configured model present')
