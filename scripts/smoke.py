import json
import os
import urllib.request

body = {'model': os.environ['MODEL'], 'messages': [{'role': 'user', 'content': 'Reply with the word ready.'}],
        'max_tokens': 64, 'temperature': 0, 'stream': False}
headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['INFER_UPSTREAM_KEY']}
if os.environ.get('SMOKE_GATEWAY') == '1':
    headers['Authorization'] = 'Bearer ' + os.environ['LITELLM_MASTER_KEY']
url = os.environ.get('SMOKE_ENDPOINT', os.environ['ENDPOINT'])
request = urllib.request.Request(url+'/v1/chat/completions', data=json.dumps(body).encode(), headers=headers)
with urllib.request.urlopen(request, timeout=120) as response:
    data = json.load(response)
message = data['choices'][0]['message']
assert message.get('content'), 'Smoke completion has no text'
assert data['usage']['completion_tokens'] > 0, 'No generated tokens'
print('Chat completion smoke passed')
