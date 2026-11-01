# SGLang warm-up flag evidence

Inspected 2026-11-01. Public Docker Hub tag metadata reports `dev-dsv41` index
`sha256:4a5d132a06a77c8331e15845f2e925adc788b00105097ad55409afa3f4fa4860`.
It does **not** identify a Git source commit. No image was pulled or run for this PR.
We cannot claim the moving tag contains the inspected GitHub commit.

The source references below pin `e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62`.
Warm-up records the actual local image ID, installed SGLang version and SHA256 of
its server_args module. Before creating the engine, it requires every selected
flag in that image's `--help`, then parses the exact argv with its own
`ServerArgs.add_cli_args` and `argparse.parse_args`. Missing flags or invalid
choices stop warm-up without a ready signal. This validates syntax, not numerical
correctness or B300 performance.
Retain the produced image digest after runtime qualification. Per the pinned
model card, `--trust-remote-code` is enabled. Only canonical manifest files are
fetched; no model Python files or Jinja chat template are added.

| Used option | Pinned source evidence |
|---|---|
| `--mem-fraction-static .85`, `--max-running-requests 64`, `--chunked-prefill-size 8192`, `--schedule-policy lpm` | [schedule fields, lines 27–100](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/schedule.py#L27) |
| `--cuda-graph-max-bs-decode`, `--cuda-graph-bs-decode 1 2 4 8 16 32 64` | [execution fields, line 636](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/exec_.py#L636) |
| `--enable-cache-report` | [serving fields, line 196](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/serving.py#L196) |
| Optional `--enable-hierarchical-cache` | [memory fields, line 100](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/memory.py#L100) |
| `--tp-size 8 --ep-size 8`; optional `--attn-dp-size 8` | [parallel fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/parallel.py#L49) |
| Existing model/context, loopback/port, native parsers, metrics and API-key flags | [model fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/model.py), [serving fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/serving.py), [observability fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/observability.py) |

The field-to-CLI registration is in [server_args.py, line 354](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/server_args.py#L354).

Model-card requirements, retained per Kai's correction:

- `--trust-remote-code`, `--reasoning-parser deepseek-v41` and
  `--tool-call-parser deepseekv41`: [pinned model card, lines 279–287](https://huggingface.co/dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/blob/c19ca5ca4eaeb79d29fdf08aea8327da95b1116a/README.md#L279).
  Exact parser choices are checked by the installed image, without substituting `auto`.
- Engram now uses `--engram-config` with explicit GPU/CPU placement, replacing
  the model card's deprecated environment variable. Host mode fails closed
  rather than changing the selected variant. See the November 1 evidence below.
- Eight B300s use TP8/EP8, with DP attention as an alternative. The starting
  context is 262144 (warm range 250000–400000), memory fraction .85, running
  requests 64 and graphs through 64. Running requests remain a knob: noon
  measurement prints the conservative KV-pool recommendation. These are starting
  settings, not a measured concurrency or memory-fit promise.
- No Jinja template is installed; the card describes DeepSeek's `encoding/`
  prompt format. The kit relies on the image's model support and parsers. A
  failure with the canonical 52-file set must be fixed in the qualified image,
  not by fetching unmanifested code during warm-up.

Dropped:

- `SPEC_DECODE=mtp`: rejected, not translated to EAGLE/NEXTN/DSpark. The
  [V4.1 cookbook](https://docs.sglang.io/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.md)
  prescribes DSpark and excludes generic MTP/EAGLE. The presence of a generic
  speculative flag is not model-specific support. Noon reports mark MTP unavailable.
- Generic `--cuda-graph-bs`: use the confirmed decode-specific spelling above.
- Card defaults for four H200s (TP4/EP4, .80, running=20, 1M context) are not
  used as SGLang defaults for this eight-B300 kit.

`HF_HUB_ENABLE_HF_TRANSFER=1` is set for compatibility, but does not accelerate
this path: Hub 1.x removed hf_transfer. Warm-up deliberately uses streaming HTTP
with 16 parallel shards (configurable 1–48), to observe speed, verify while writing,
and switch a failed/slow shard to the mirror. No misleading hf_transfer claim is
made. The legacy bootstrap/prestage path continues using HF Xet.

## November 1 A/B additions

The new options were checked against upstream commit
`c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0`, retrieved November 1, 2026.
This is source evidence, not proof that a moving `dev-dsv41` tag contains it.
The A/B renderer lives in `scripts/serve_plan.py`; engine options are centralized
in `scripts/engine.py`. Warm checks the installed engine and router help and
argument parsers before creating any GPU process. Engram's JSON is additionally
validated with the installed `EngramConfig.from_dict`. Runtime compatibility
and memory fit are checked by load/readiness, not inferred from argument parsing.

| Option / behavior | Pinned primary source |
|---|---|
| `--tp-size`, `--ep-size`, `--attn-dp-size`, `--enable-dp-lm-head`, `--dist-init-addr` | [parallel fields](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/python/sglang/srt/arg_groups/fields/parallel.py) |
| Native replicas versus attention DP; old DP-attention flags deprecated | [DP/DPA guide](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/docs/docs/advanced_features/dp_dpa_smg_guide.mdx) |
| `--engram-config`: `placement` is exactly `gpu` or `cpu`, default replication `none`, lookup `auto` | [EngramConfig schema](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/python/sglang/srt/arg_groups/engram_config.py), [JSON CLI parsing](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/python/sglang/srt/arg_groups/fields/exec_.py#L1134), [Engram guide](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/docs/docs/advanced_features/engram.mdx) |
| DSpark `--speculative-algorithm DSPARK --speculative-dspark-block-size 5` | [V4.1 cookbook, section 3.3](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/docs/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.mdx) |
| Router module; worker URLs, host/port, policy `cache_aware`, `--dp-aware`, `--api-key`, `--prometheus-host` | [launch_router](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/sgl-model-gateway/bindings/python/src/sglang_router/launch_router.py), [RouterArgs](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/sgl-model-gateway/bindings/python/src/sglang_router/router_args.py) |
| Router API-key middleware on inference routes, public health; `/v1/models` GET forwards client auth to worker | [server routes](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/sgl-model-gateway/src/server.rs), [HTTP proxy](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/sgl-model-gateway/src/routers/http/router.rs#L92) |
| Why cache-aware rather than shared-key hashing: consistent hashing falls back to Authorization when no session routing key is supplied | [consistent hashing policy](https://github.com/sgl-project/sglang/blob/c757c4bb7c55d1eb69fdb2bdc6e6fb8507acb7a0/sgl-model-gateway/src/policies/consistent_hashing.rs#L136) |

The host RAM floor is multiplied by independent replica count; no cross-process
table sharing is assumed. TP8 remains one engine. DEP layouts use a single
loopback router on the existing tunnel target port, without changing tunnel,
mirror authentication or ready-signal contracts. See the [noon runbook](ab-serve-plans.md)
for the exact commands and remaining on-image checks.
