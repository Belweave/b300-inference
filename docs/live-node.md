# Live B300 launch

For the supervised one-shot command, use [node-run](node-run.md).

For the November 1 uncensored-model experiment, use the [A/B noon runbook](ab-serve-plans.md).
The recipe below documents the earlier live server, not the new A/B defaults.

Default: the finalized max-TPS configuration run on 8× B300 with TP8.
The commands below are operator instructions, not an automatic deployment.
The server binds `0.0.0.0:8000` inside the container; the published host port is
loopback-only for the tunnel. The sole key source is the private, mode-600
`~/node.env`: its `INFER_UPSTREAM_KEY` must be the same upstream key configured
in the gateway. Provision that value in `~/.config/b300/node.env` on the Mac
before copying it below; do not generate a separate server key.
SGLang has no env/file option for `--api-key`: Docker inspect and node process
inspection (`ps`) can expose it. The kit's Python redactor plus `--log-driver none` protects logs only.

Both parsers use **auto** on `lmsysorg/sglang:dev-dsv41`. From architecture
`DeepseekV41ForCausalLM`, the image resolved tool calls to `deepseekv41` and
reasoning to `deepseek-v41`. We use `auto`, not hardcoded parser names.

No served-name override was used on the live server: it serves `/models/dsv41`.
See [model naming and the gateway contract](model-naming.md) for current aliases
and the future restart plan. Do not rename the running server as part of this update.

The automated kit's default `PROFILE=longctx` uses the same SGLang capacity
settings in `config/sglang.env`; its existing `/weights` mount, loopback binding,
secret transport and served-name override are separate from this live recipe.
`PROFILE=throughput` retains the old 32K/256 configuration in
`config/throughput.env`; it is an optional alternative, not the default.

```bash
# ON THE MAC, from the b300-inference repo; replace <user@node> and <port>.
scp -P <port> -p ~/.config/b300/node.env scripts/redact.py <user@node>:~/
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
umask 077
set -a; . ~/node.env; set +a
: "${INFER_UPSTREAM_KEY:?missing from node.env}"
mkdir -p -m 700 ~/dsv41-logs
chmod 700 ~/dsv41-logs
touch ~/dsv41-logs/dsv41.log
chmod 600 ~/dsv41-logs/dsv41.log
# The key is visible in container configuration and process args; filtering protects logs only.
docker run -d --name dsv41 --gpus all --ipc host --shm-size 32g \
  --cap-add SYS_NICE --log-driver none -p 127.0.0.1:8000:8000 -e INFER_UPSTREAM_KEY \
  --mount type=bind,src=/mnt/nvme/models/dsv41,dst=/models/dsv41,readonly \
  --mount "type=bind,src=$HOME/dsv41-logs,dst=/logs" \
  --mount "type=bind,src=$HOME/redact.py,dst=/redact.py,readonly" \
  --entrypoint sh lmsysorg/sglang:dev-dsv41 -c \
  'umask 077; python3 -m sglang.launch_server --model-path /models/dsv41 --tp 8 --trust-remote-code --host 0.0.0.0 --port 8000 --context-length 409600 --max-running-requests 128 --cuda-graph-max-bs-decode 128 --chunked-prefill-size 8192 --mem-fraction-static 0.85 --tool-call-parser auto --reasoning-parser auto --enable-metrics --api-key "$INFER_UPSTREAM_KEY" 2>&1 | python3 -u /redact.py >> /logs/dsv41.log'
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
  -d '{"model":"/models/dsv41","messages":[{"role":"user","content":"Say hi in five words."}],"max_tokens":32}' \
  <<<"Authorization: Bearer $INFER_UPSTREAM_KEY"
```

```bash
# MAC: Netops-supplied tunnel helper transfer (default shown; confirm with Netops).
scp -P <port> -p vendor/infer-gateway/node-tunnel.sh <user@node>:~/
```

```bash
# NODE: tunnel; execute (do not source) the helper after sourcing node.env.
# It exports TUNNEL_TOKEN and unsets CF_TUNNEL_TOKEN only inside its own process.
# It appends to raw /tmp/cloudflared.log across runs; do not share that file unredacted.
set +x
set -a; . ~/node.env; set +a
~/node-tunnel.sh
```

```bash
# NODE: stop when finished; rm -f kills the shell, engine and redactor together.
docker rm -f dsv41
```
