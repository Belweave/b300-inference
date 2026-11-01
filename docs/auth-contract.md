# Engine API keys and captured output

Inspected 2026-10-31. No real node, credential file, or engine image was accessed
for these checks. The SGLang source is main at
`e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62`; the moving `dev-dsv41` image may differ.
vLLM is tag `v0.32.0`. Each rental still runs the auth/readiness/chat checks.

## SGLang

The serving argument record defines `api_key` with a `None` default, and the CLI
registers the record's fields. No `SGLANG_API_KEY` server-auth variable or
`--api-key-file` option was found in the inspected argument, environment,
launch and auth sources. EXA_API_KEY is an unrelated third-party tool credential.

The engine logs its resolved argument dictionary through
`_server_args_for_logging`. That function invokes `redact_watermark_secrets`,
whose secret-field list covers watermark configuration, not `api_key`. Thus this
startup path **can print the API key**. Do not rely on upstream masking here.

Sources: [serving fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/serving.py#L160),
[argument serialization/CLI](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/server_args.py#L317),
[environment registry](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/environ.py),
[startup logger](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/entrypoints/engine.py#L179),
[watermark-only filter](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/sampling/watermarking/core.py#L25).

The kit's narrow fallback adds `--api-key` only inside `serve.py`, after the
secret arrives via Docker stdin. It is absent from Docker create configuration,
the host command, and `runtime.json`; **it remains visible in the inner process's
argv (`ps` or `/proc/.../cmdline`)**. No claim of complete argv secrecy is made.

## vLLM

The API middleware explicitly falls back to `envs.VLLM_API_KEY` when no CLI keys
are given. The environment registry reads that variable. The kit sets it inside
`serve.py` and omits `--api-key`, keeping it out of process argv and Docker config.
The inspected CLI has no dedicated API-key-file option. Its nondefault-argument
startup logger also masks `api_key`, `hf_token`, and `watermark_config`.

Sources: [middleware registration](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/middleware/register.py#L32),
[environment registry](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/envs.py#L819),
[CLI fields](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/launchers/cli_args.py#L325),
[startup masking](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/utils/api_utils.py#L270).

## Storage and operator handoff

The watcher copies `MAC_NODE_ENV_FILE` (default `~/.config/b300/node.env`) with
`scp -p`, then applies mode 600 remotely. It never sources or parses that file
on the Mac. Node entrypoints source `NODE_ENV_FILE` (default `~/node.env`) when
present; it must be owned by the current user and mode 600. Dry runs skip it.
`client.env` is not copied or used. Both filenames are ignored by Git.

All engine and gateway stdout/stderr flow through `scripts/redact.py` before
log files. Detached bootstrap/benchmark/reverse-SSH captures use `capture.sh`,
which loads the node environment before starting both the command and filter.
The filter masks exact exported secret values, quoted/unquoted API-key fields,
and Bearer tokens. Docker uses `--log-driver none`, so `docker logs` cannot retain
raw startup output. A normal Docker logging driver would retain SGLang's echoed
key before a host-side filter could protect it.

Teardown filters collected cloudflared output and re-passes every diagnostic
archive member through `redact.py`. Env files, keys, symlinks and incoming image
archives are excluded. Scratch storage contains filtered bytes only. Fake-key
tests exercise stdout/stderr capture and inspect every tar member.

The short [live-node block](live-node.md) uses name-only `-e INFER_UPSTREAM_KEY`
for convenience and an in-container filter before a mounted log file. Unlike
the full kit's stdin transport, that environment value **is visible in Docker
inspect** to Docker-capable users. Both approaches leave secrets in process
memory/environment; host root and Docker access remain trusted. The unchanged
vendor tunnel helper's raw `/tmp/cloudflared.log` caveat still applies.
