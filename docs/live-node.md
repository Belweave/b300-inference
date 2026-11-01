```bash
# ON THE MAC, from the b300-inference repo; replace <user@node> and <port>.
scp -P <port> -p ~/.config/b300/node.env <user@node>:~/
ssh -p <port> <user@node> 'chmod 600 ~/node.env'
```

```bash
# NODE: check weights; continue to launch only after this check succeeds.
set +x
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/mnt/nvme/models/dsv41')
assert not any(p.rglob('*.incomplete')), 'Download still in progress'
indices = list(p.rglob('*.safetensors.index.json'))
assert indices, 'Missing safetensors index'
shards = {idx.parent / name for idx in indices for name in json.loads(idx.read_text())['weight_map'].values()}
assert shards and all(s.is_file() and s.stat().st_size > 0 for s in shards), 'Missing/empty shard'
assert (p / 'config.json').is_file(), 'Missing config'
print(f'All {len(shards)} indexed shards present; this is not a checksum verification.')
PY
```

```bash
# NODE: launch; failures exit only this child bash, preserving your SSH shell.
set +x
bash -e <<'BASH'
set -a; . ~/node.env; set +a
: "${INFER_UPSTREAM_KEY:?missing from node.env}"
umask 077
mkdir -p -m 700 ~/dsv41-logs
chmod 700 ~/dsv41-logs
touch ~/dsv41-logs/dsv41.log
chmod 600 ~/dsv41-logs/dsv41.log
cat > ~/dsv41-redact.py <<'PY'
import os, re, sys
secrets = [v for k, v in os.environ.items() if v and any(s in k for s in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET'))]
for line in sys.stdin:
    for value in sorted(secrets, key=len, reverse=True):
        line = line.replace(value, '[REDACTED]')
    line = re.sub(r'''(?i)(["']?(?:api[_-]?key|admin_api_key)["']?\s*[:=]\s*)(?:\[REDACTED\]|"[^"\n]*"|'[^'\n]*'|[^\s,}\])]+)''', r'\1[REDACTED]', line)
    line = re.sub(r'(?i)(Bearer\s+)[^\s"\x27,;]+', r'\1[REDACTED]', line)
    sys.stdout.write(line); sys.stdout.flush()
PY
# docker inspect shows -e values to anyone with Docker access on this node.
# SGLang has no verified native env/file key option; its inner process argv still exposes the key.
# Redaction runs inside the container before disk; host log: ~/dsv41-logs/dsv41.log.
docker run -d --name dsv41 --gpus all --network host --ipc host \
  --cap-add SYS_NICE --log-driver none -e INFER_UPSTREAM_KEY \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  --mount type=bind,src=/mnt/nvme/models/dsv41,dst=/weights,readonly \
  --mount "type=bind,src=$HOME/dsv41-logs,dst=/logs" \
  --mount "type=bind,src=$HOME/dsv41-redact.py,dst=/redact.py,readonly" \
  --entrypoint sh lmsysorg/sglang:dev-dsv41 -c \
  'umask 077; python3 -m sglang.launch_server --model-path /weights --tp 8 --host 127.0.0.1 --port 8000 --context-length 409600 --max-running-requests 64 --cuda-graph-max-bs-decode 64 --chunked-prefill-size 4096 --mem-fraction-static 0.80 --reasoning-parser auto --tool-call-parser auto --enable-metrics --served-model-name deepseek-ai/DeepSeek-V4.1-Flash-1017 --api-key "$INFER_UPSTREAM_KEY" 2>&1 | python3 -u /redact.py >> /logs/dsv41.log'
BASH
```

```bash
# NODE: optional log following; paste separately, then Ctrl-C before the next block.
tail -f ~/dsv41-logs/dsv41.log
```

```bash
# NODE: wait; Ctrl-C safely cancels this loop if startup fails.
until curl -fsS localhost:8000/health >/dev/null; do sleep 15; done; echo healthy
```

```bash
# NODE: test after healthy; source the key without printing it.
set +x
set -a; . ~/node.env; set +a
curl -fsS localhost:8000/health
curl -fsS localhost:8000/v1/chat/completions \
  -H @- -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-ai/DeepSeek-V4.1-Flash-1017","messages":[{"role":"user","content":"Say hi in five words."}],"max_tokens":32}' \
  <<<"Authorization: Bearer $INFER_UPSTREAM_KEY"
```

```bash
# MAC: Netops-supplied tunnel helper transfer (default shown; confirm with Netops).
scp -P <port> -p vendor/infer-gateway/node-tunnel.sh <user@node>:~/
```

```bash
# NODE: tunnel; the helper reads CF_TUNNEL_TOKEN.
set +x
set -a; . ~/node.env; set +a
~/node-tunnel.sh
```

```bash
# NODE: stop when finished; rm -f kills the shell, engine and redactor together.
docker rm -f dsv41
```
