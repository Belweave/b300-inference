# Health and API authentication

Inspected 2026-10-31. These are source findings, not a completed GPU deployment.
Both automated kit engine presets explicitly bind `127.0.0.1:8000`; health is on that main port.

| Engine | GET /health with --api-key | GET /v1/models |
|---|---|---|
| SGLang | Unauthenticated; auth bypass for `/health*` | API key required |
| vLLM v0.32.0 | Unauthenticated; not a guarded prefix | API key required |

SGLang source revision `e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62`:
[HTTP route and main app binding](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/entrypoints/http_server.py),
[decide_request_auth](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/utils/auth.py).
The auth decision permits health/readiness/metrics prefixes even when an API key
is configured. `/health` and `/health_generate` are routes on the main FastAPI app.
The inspected HTTP handler checks inference health using a one-token request.
The `dev-dsv41` image is mutable and its exact source SHA is not established here;
bootstrap rechecks the observed HTTP contract before publishing readiness.

vLLM v0.32.0:
[health route](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/instrumentator/health.py),
[main app route registration](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/instrumentator/__init__.py),
[authentication middleware](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/middleware/authenticate.py),
[middleware wiring](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/entrypoints/serve/middleware/register.py).
The route calls the engine health check and returns 200 or 503. Authentication
applies to `/v1`, `/v2`, `/inference`, and `/cohere`, not `/health`.

`tests/test_followup.py` tests both generated launch commands, bootstrap's actual
health request URL and lack of Authorization, and the readiness client's public
health + unauthenticated 401 + authenticated model-list sequence with mocked HTTP.
`scripts/readiness.py` runs that same contract against the real engine at startup;
`scripts/smoke.py` then requires an actual chat completion. No GPU is needed for
the local tests, and passing them is not reported as testing either real engine.

## Live launch and model identity

The [live-node recipe](live-node.md) uses `0.0.0.0:8000` inside Docker with
host port 8000 published on loopback for the tunnel. The running upstream serves
`/models/dsv41`; authenticated direct model-list/chat checks must expect that name.
The gateway advertises `deepseek-ai/DeepSeek-V4.1-Flash-1017` and maps it to
`/models/dsv41`, while accepting the upstream name unchanged (including Hermes).
The future clean restart uses the catalog served name and drops the gateway
rename; see [the naming contract](model-naming.md). Netops owns gateway changes.
The live key comes from `~/infer.key` and remains visible in container/process
inspection. Sed redaction and Docker's `--log-driver none` protect logs only;
see [the auth contract](auth-contract.md).
