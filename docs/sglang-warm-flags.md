# SGLang warm-up flag evidence

Inspected 2026-11-01. Public Docker Hub tag metadata reports `dev-dsv41` index
`sha256:4a5d132a06a77c8331e15845f2e925adc788b00105097ad55409afa3f4fa4860`.
It does **not** identify a Git source commit. No image was pulled or run for this PR.
We cannot claim the moving tag contains the inspected GitHub commit.

The source references below pin `e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62`.
Warm-up records the actual local image ID, installed SGLang version and SHA256 of
its server_args module. Before creating the engine, it requires every selected
flag in that image's `--help`, then parses the exact argv with its own
`prepare_server_args`. Missing flags or invalid choices stop warm-up without a
ready signal. This validates syntax, not numerical correctness or B300 performance.
Retain the produced image digest after runtime qualification. No remote model
Python code is enabled via `--trust-remote-code`.

| Used option | Pinned source evidence |
|---|---|
| `--mem-fraction-static .85`, `--max-running-requests 64`, `--chunked-prefill-size 8192`, `--schedule-policy lpm` | [schedule fields, lines 27–100](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/schedule.py#L27) |
| `--cuda-graph-max-bs-decode`, `--cuda-graph-bs-decode 1 2 4 8 16 32 64` | [execution fields, line 636](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/exec_.py#L636) |
| `--enable-cache-report` | [serving fields, line 196](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/serving.py#L196) |
| Optional `--enable-hierarchical-cache` | [memory fields, line 100](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/memory.py#L100) |
| TP8; optional `--attn-dp-size 8 --ep-size 8` | [parallel fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/parallel.py#L85) |
| RAM-gated `--engram-config '{"placement":"cpu"}'` | [execution fields, line 1134](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/exec_.py#L1134) |
| Existing model/context, loopback/port, native parsers, metrics and API-key flags | [model fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/model.py), [serving fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/serving.py), [observability fields](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/arg_groups/fields/observability.py) |

The field-to-CLI registration is in [server_args.py, line 354](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/server_args.py#L354).

Dropped:

- `SPEC_DECODE=mtp`: rejected, not translated to EAGLE/NEXTN/DSpark. The
  [V4.1 cookbook](https://docs.sglang.io/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.md)
  prescribes DSpark and excludes generic MTP/EAGLE. The presence of a generic
  speculative flag is not model-specific support. Noon reports mark MTP unavailable.
- `SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE`: no supported use verified in the
  [environment registry](https://github.com/sgl-project/sglang/blob/e3ec98b15e6a82da7e58efff9f8d4448b9f0ae62/python/sglang/srt/environ.py).
  Use the current placement flag after checking `MemAvailable >= table + headroom`
  (203 decimal GB + 32 GB by default), refreshed after downloads.
- Generic `--cuda-graph-bs`: use the confirmed decode-specific spelling above.
- TP4/EP4 and max-running=20 from the model card: not copied. The warm preset is
  TP8, running=64, graphs through 64. DP attention/EP is an explicit experiment;
  neither layout nor 64 full 409,600-token contexts is a measured fit guarantee.

`HF_HUB_ENABLE_HF_TRANSFER=1` is set for compatibility, but does not accelerate
this path: Hub 1.x removed hf_transfer. Warm-up deliberately uses streaming HTTP
with 16 parallel shards (configurable 1–48), to observe speed, verify while writing,
and switch a failed/slow shard to the mirror. No misleading hf_transfer claim is
made. The legacy bootstrap/prestage path continues using HF Xet.
