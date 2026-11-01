# Warm cut-over

Default model: `dealignai/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8` at
`c19ca5ca4eaeb79d29fdf08aea8327da95b1116a`. The committed
[canonical manifest](../config/manifests/uncensored-c19ca5c.json) is Kai's verified
52-file manifest. Shard metadata was rechecked against the public HF API on
2026-11-01. All 48 shards total 510.30 GB. Exactly 27 (00013–00039) differ
from the pinned base, totaling **199,573,008,344 bytes**. The other 21 can be
hardlinked after verification (310.72 GB reused). Shards 00047 and 00048 are about 101.5 GB each,
likely Engram tables: a full download is dominated by those two individual files,
so adding parallel shard workers alone cannot remove that bottleneck. Partial
warm-up hardlinks them. Across filesystems, copying them adds local I/O time.

Only the 48 shards plus `config.json`, `model.safetensors.index.json`,
`tokenizer.json` and `tokenizer_config.json` are fetched. Every file requires
canonical SHA256 plus size, including the four small files whose SHA256s were
computed from downloaded bytes. Those four files are identical to base and are
also hardlinked after verification. README, two branding PNGs and
`inference/kernel.py` differ; they are optional and are not downloaded.
The kernel change likely does not affect SGLang, which uses its own kernels,
but needs runtime qualification. Base-only encoding README/output fixtures are
not copied. The full [79-file API listing](../config/model-manifest.json) is audit
evidence only; it never overrides canonical hashes or expands downloads.
The model is MIT, DeepseekV41ForCausalLM, mixed FP8/FP4 experts.

## On the replacement node (operator action, not run by this PR)

Export an existing upstream key, `WINDOW_END` (the **old node's** expiry), and
`TUNNEL_SLOT` with exactly `a` or `b`. The operator exports the slot from their
node environment; warm-up reads only the process environment and never sources
or reads `node.env`. A missing or invalid slot fails before downloads or GPU work.
No credential files are sourced by these tools. With Python dependencies and
Docker already installed, run `scripts/warm.sh`. It does not start a tunnel, flip
a gateway, acquire GPUs or release allocations. It refuses an occupied local
port 8000. The replacement node needs eight B300s and its own allocation lifetime.
This is preparation on a separate node; do not run it on the serving node.

Key knobs: `BAKED_IMAGE`, `BASE_WEIGHTS_DIR` (default `/mnt/nvme/models/dsv41`),
`WEIGHTS_DIR` (default `/mnt/nvme/models/dsv41-uncensored`), `SHARD_WORKERS=16`,
`HASH_WORKERS=4`, `MIN_MBPS=0` (decimal MB/s; measured over at least 10 seconds),
`MEM_FRACTION=.85`, `CHUNKED_PREFILL_SIZE=8192`, `SCHEDULE_POLICY=lpm`,
`CONTEXT_LENGTH=262144` (250000–400000), `MAX_RUNNING_REQUESTS=64` (provisional;
replace from the noon pool report and sweep),
`LAYOUT=tp8|dep4x2|dep8x1|dep2x4`, `ENGRAM=gpu|host`, `DSPARK=off|on`, `WARM_GRAPH_BS=1,2,4,8,16,32,64`,
`HIERARCHICAL_CACHE=0`, `ENGRAM_TABLE_GB=203`, `ENGRAM_HEADROOM_GB=32`.
MTP is deliberately rejected; see [flag evidence and dropped options](sglang-warm-flags.md).
Baseline is TP8/EP8. The parsers are `deepseek-v41` for reasoning and `deepseekv41`
for tools, with `--trust-remote-code` and no Jinja template. Engram defaults to GPU; explicit host mode uses
`--engram-config '{"placement":"cpu"}'` only after the available-RAM gate
(203 GB per replica + 32 GB headroom), and fails closed on insufficient RAM.
No deprecated Engram environment variable is emitted. See the
[A/B noon runbook](ab-serve-plans.md) for flags, dry-runs and image qualification.

Bake `docker/Dockerfile.baked` before the rental, pin the result, and stage it
locally as `BAKED_IMAGE`. A locally present baked image skips pulls; otherwise
warm-up pulls it (or the preview image). Dependencies/native parser imports and
redactor are baked; GPU weight loading, kernel compilation and graphs are not.
The full kit directory remains mounted read-only for `engine.py` and stdin auth.
The SGLang inner argv key exposure described in [auth-contract.md](auth-contract.md)
still applies; Docker logging is disabled and captured streams are redacted.

Image prep and weights overlap. Timings with epoch start/end and status are in
`runs/warm-<timestamp>-<id>.json`; logs and ready file are in its sibling directory.
The weights stage includes parallel streaming hashes, local verification and any
copy. Initial verification still reads reused shards: pre-stage a verification
run before a paid window when possible. Markers bind expected digest, size,
mtime, ctime, inode and device; `FORCE_HASH=1` bypasses them. These markers assume
operator-controlled, immutable storage, not hostile writers. Downloads replace
files atomically and never write through existing hardlinks.

Optional mirror: `MIRROR_URL/<revision>/manifest.json` must contain
`{"revision":"<sha>","files":{"<shard>":{"sha256":"...","size":123}}}`
with exactly the committed shard inventory and hashes. It may additionally
include any of the four canonical small files. Kai's 52-entry `files` array
format is also accepted. Unknown files or any disagreement abort.
Shards are at `MIRROR_URL/<revision>/<filename>`. HF errors, bad hash/size or a
speed below the floor trigger a per-shard mirror retry; the mirror is verified
identically. Failed partial transfers restart that shard. Small files can use
mirror fallback only when included in its validated manifest; otherwise they use HF.
No credentials belong in URLs. Netops owns the mirror; this PR creates no bucket.
Optional `MIRROR_AUTH_HEADER` is one complete `Name: value` header, such as
an Authorization bearer header or a custom `X-Foo` header. Export it through the
operator's existing environment; leave it unset or empty for plain GETs. Header
names must be HTTP tokens and values must be nonempty single-line HTTP field
values; malformed input fails with an error that omits the supplied value.
The header is sent on mirror manifest, shard/small-file and R2 speed-test
requests only, including the measurement probe. It is never sent to HF or the
location probe, placed in URLs, or included in reports. Authenticated mirror
redirects are refused so credentials cannot follow a redirect to another host;
configure `MIRROR_URL` to the final HTTPS origin. Mirror request errors discard
raw HTTP exception details, and log redaction masks both the full line and value.
Network timing records only Cloudflare `colo`/`loc` and bounded 8 MiB HF/R2 probe
speeds, not IPs or URLs. Set `MIRROR_TEST_OBJECT` (default `speed-test.bin`) for R2.
Probe throughput is a sample; transfer records contain actual full-shard speeds.

## Budget (all estimates; no B300 measurements yet)

| Step | Budget / bound |
|---|---|
| Image, already baked/local | 0–1 min; missing layers depend on image size/link |
| Partial 199.57 GB at 1 / 10 / 25 Gbit/s | 26.61 / 2.66 / 1.06 min ideal; 38.01 / 3.80 / 1.52 at 70% |
| Full 510.30 GB at 1 / 10 / 25 Gbit/s | 68.04 / 6.80 / 2.72 min ideal; 97.20 / 9.72 / 3.89 at 70% |
| Hash verification | 1.7–8.5 min for full bytes at 5–1 GB/s; streaming hashes overlap transfers; unchanged markers skip reads |
| Engram host table placement/load | 1–5 min planning allowance, to be measured; RAM gate may disable it |
| Weight load / kernel compilation | 5–15 min, to be measured |
| CUDA graph capture | 1–5 min, to be measured; seven small decode sizes |
| Health, 1-token and streaming checks | 0.2–1 min after engine is serving |
| Operator-owned prefix replay | 0.1–1 min for supplied tiny placeholders |

Image and transfer wall time overlap; table/weight loading may overlap internally,
so do not sum those startup estimates as measured phases. The timing log records
the combined load/graph/health wall time; engine startup logs are needed to split
it further. **15–20 minutes is a target only** with baked images, partial reuse,
fast verified local storage, cached verification and a nearby sustained 10–25
Gbit/s source. It is impossible for partial transfer alone at 1 Gbit/s. Full cold
loads, remote 101 GB shards or cross-device copies can exceed it even at 10 Gbit/s.

## Gateway contract

Client API base: `https://api.infer.belweave.ai/v1` (host
`https://api.infer.belweave.ai`, base path `/v1`). `infer.belweave.tech` is a
deprecated alias kept working during migration until clients move over.
Netops owns DNS and Worker routing for both hosts; the origin tunnel is unchanged.

Success atomically writes `runs/warm-<id>/ready.json` and prints one
`READY <JSON>` line. Schema 1 contains `state`, `model`, `revision`, `ready_epoch`,
`deadline_epoch`, `container`, `timing_file`, and additive `tunnel_slot` (`a` or
`b`, from the validated `TUNNEL_SLOT` environment variable). Schema stays at 1;
the ready file and stdout line carry the same slot. Netops must independently probe the
new origin through its route before flipping. A PID or ready file alone is not
proof of public reachability. No ready signal is emitted after
`WINDOW_END - 13 minutes`; failure removes any new ready file and stops only the
new warm container. File hashes/copies can finish after the deadline, but late
completion cannot become ready. Arrange rental expiry cleanup independently.

Flip **by WINDOW_END−13 min**. JIT start recommendation is that flip deadline minus
measured warm duration minus five minutes (equivalently old expiry minus 13 min,
warm duration and 5 min). Once flipped, stop old-node admission and drain until
`min(flip_time + DRAIN_CAP, WINDOW_END - 3 min)`, with DRAIN_CAP default **1200
seconds** (20 min). This is an absolute stop time, not epoch seconds used as a
duration. `warm_logic.drain_until` implements the arithmetic; Netops applies it.
The existing standalone kit's T−10 min teardown is a different lifecycle: a
coordinated cut-over must configure its deadline/drain policy explicitly. These
tools do not silently override an existing node's timer.

## Noon measurement

Run `python3 scripts/measure.py --warm-report runs/warm-<id>.json --log
runs/warm-<id>/engine.log --output runs/noon-tp8` on the prepared node. It reports
local sweeps at 1/8/16/32/64 streams, TTFT, decode tok/s, aggregate input/output
rates, cached-token rate, errors, network probes, and a 16/32/48/64 by
100K/250K/400K pool-fit table with 10% headroom. Missing pool/bytes/cache fields
remain unknown; multiple DP pool lines use the minimum, never an assumed sum.
The report prints recommended `MAX_RUNNING_REQUESTS = floor(0.9 *
max_total_num_tokens / CONTEXT_LENGTH)` at the selected context, including zero
when a full context cannot fit. This conservative bound assumes full contexts;
validate it with real workload measurements. Tune `MEM_FRACTION` from that run,
then rerun measurement; the starting .85 value is not a measured optimum.

An operator separately prepares a DP/EP run, then uses `--compare` on the two
JSON reports. Comparison rejects extra changed engine knobs/model/workload. MTP
is unavailable rather than fabricated. This tool never restarts an engine.
The fixed synthetic prompt measures cache reuse; it is not a diverse workload
SLA. Prefix files contain only our own placeholders, never customer prompts.
