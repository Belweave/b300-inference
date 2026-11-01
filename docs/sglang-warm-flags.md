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
- `SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1`: same card, line 284. Set only if
  `MemAvailable >= table + headroom` (203 decimal GB + 32 GB by default),
  refreshed after downloads. Otherwise explicitly set `0` and log why. The
  inspected newer upstream environment registry does not confirm this variable;
  it is retained for the card's preview build, and its effect needs on-node
  qualification. We do not silently substitute `--engram-config`.
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
