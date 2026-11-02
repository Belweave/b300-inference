# Boat staging and CPU guard qualification

Verified on 2026-11-02 UTC: **52 model files, 510,310,550,839 bytes**, all SHA256
and sizes matched through the authenticated private Worker. The manifest is
published; existing cloudflared 2026.10.7 amd64 also passed the pinned checksum
through the Worker. See [final receipt](results/r2-final.json),
[object ledger](results/r2-objects.csv), and [final status](results/session-status.json).
All five task sandboxes were stopped without snapshots, temporary credential
copies were removed, and Kai was told Netops can revoke the parent staging token.
No incomplete multipart uploads remained at the final cleanup check.

Staging and testing only. No public ports, deployments, inference keys, or
evidence-bucket access. Branch: `boat-staging-validation`, based on
`origin/safety-classifier` at `88595f4`. The parent R2 token stays on the Mac;
only four-hour derived S3 session credentials and the approved read-only mirror
bearer go into the no-snapshot VMs. The mirror header is read as data from
`~/.config/b300/mirror.env`; never source that unquoted, space-containing value.

## CPU correctness

`cpu_guard_server.py` loads pinned, unquantized BF16 weights on CPU and implements
the OpenAI-compatible requests made by the production `scripts/safety.py` scorer.
Shieldstral uses its model card's `MistralCommonBackend` chat template and a
single next-token forward pass with top-20 log probabilities; Qwen uses its own
chat template and at most 64 generated moderation tokens. Neither generates
sample offending material.

Pinned revisions:

* Shieldstral: `003ec7e2b0bab5f0e6307edbaf186fa5822b76f5`.
* Qwen3Guard-Gen-0.6B: `fada3b2f655b89601929198343c94cd2f64d93cc`.

Run in the sandbox with `torch` (CPU build), `transformers`, `mistral-common`,
`aiohttp`, and `psutil` installed:

```sh
python bench/boat/cpu_guard_server.py shieldstral --threads 4
# In another shell:
python bench/boat/cpu_guard_server.py qwen --threads 4
# In another shell, from the repository:
python bench/boat/correctness.py --output /tmp/regression.json
python bench/boat/correctness.py --suite heldout --endpoint --output /tmp/heldout.json
```

Each suite has 72 cases: 12 positive category-label proxies and 24 benign
controls in each of the input/output phases. Regression variants are plain,
instruction override, and JSON. Held-out variants are XML, uppercase, and role
spoofing. No real CSAM, sexual descriptions, illegal payloads, or requests to
generate such content exist in the fixture. Benign controls cover parenting,
medical education, reporting, survivor support, adult relationship labels,
coding, and ordinary tasks. The held-out run uses the real HTTP relay on an
ephemeral loopback port. Dataset hashes bind the exact generated fixtures.
The held-out suite holds out wrapper forms; it shares the same base category
records and benign topics, so it is not an independent semantic dataset.

Canaries are excluded from learned-model precision/recall. Backend errors fail
the gate and are counted separately; a fail-closed outage is never counted as a
correct model prediction. Production thresholds stay at 0.5; no keyword-based
benchmark shortcut is added. These finite, synthetic proxy results do **not**
establish real-world detection accuracy.

Baseline input: TP12/FN0/FP0/TN24. Baseline output: TP9/FN3/FP0/TN24. The Qwen
sexual-category prefilter skipped Shieldstral on three proxy positives. The fix
always applies Shieldstral's minors policy to output, including when Qwen says
Safe or omits the sexual category. Both backends remain required and backend
failures still return 503. This adds a Shieldstral call on previously skipped
outputs and requires GPU capacity/latency qualification.

After the fix, regression and held-out HTTP suites each passed all 72 cases.
Combined: **TP48, FN0, FP0, TN96; precision 100%, recall 100%, FNR 0%, FPR 0%**,
with zero backend errors. See `results/correctness-baseline.json`,
`results/correctness-fixed.json`, and `results/correctness-heldout.json`.
Linux process high-water RSS was 8,101,988 kB for Shieldstral and 2,292,676 kB
for Qwen (`results/cpu-memory.json`). Earlier adapter health snapshots sampled
RSS after requests; `/proc` high-water values are the authoritative memory
measurement for this run. The adapter now reports the OS high-water value too.

CPU timing and process RSS in result JSON are diagnostics only. **B300 p99,
throughput, VRAM, and concurrent-stream capacity remain pending a GPU node.**
The exact production SGLang container launch is not qualified by this adapter.
The [Shieldstral model card](https://huggingface.co/mistralai/Shieldstral-1.0-3B)
requires SGLang to include loader fix
[sglang#33671](https://github.com/sgl-project/sglang/pull/33671); check the pinned
node image before another expensive model warm. Run the guard correctness gate
before loading the 475 GiB serving model.

## R2 staging

`stage_r2.py` must run in Boat, with `boto3` and `requests` installed. It accepts
only temporary session credentials in a mode-600 JSON file, with keys
`accessKeyId`, `secretAccessKey`, `sessionToken`, and `endpoint`. Never put these
values on command lines or in reports. Derive them on the Mac using the approved
Netops parent access-key ID with:

```text
cf r2 temporary-credentials create --bucket model-mirror \
  --parent-access-key-id <read locally from approved credentials file> \
  --permission object-read-write --ttl-seconds 14400 \
  --prefixes models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/ tools/cloudflared/2026.10.7/
```

Capture the response privately; do not print it. No parent secret is needed in
Boat. The fixed destination is `model-mirror`, under:

```text
models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/c19ca5ca4eaeb79d29fdf08aea8327da95b1116a/
```

The canonical 52 files total **510,310,550,839 bytes**. The uploader streams
64 MiB HTTP ranges directly into R2 multipart uploads, with a bounded number of
buffers and no on-disk weight copy. Source locations are resolved once per object
and cached privately for five minutes; source connection resets retry per range.
A full authenticated Worker readback, in byte order, must match the committed
SHA256 and size of every file, alongside an R2 HEAD size check. This checks the
actual node pull route as well as the bucket bytes. Multipart ETags and user metadata
are not accepted as SHA256 proof. Interrupted multipart transfers can resume;
reused parts are qualified by the final full-object readback. Existing complete
objects also require full readback before reuse. Source range and length
validation prevents offset corruption. The manifest is published only after all
52 model objects verify. Existing cloudflared binaries are read and checked
against both their sidecars and the committed 2026.10.7 release pins; never uploaded.

```sh
python bench/boat/stage_r2.py \
  --credentials /home/user/r2-temporary.json \
  --manifest config/manifests/uncensored-c19ca5c.json \
  --report /home/user/r2-report.json --workers 2 --range-workers 16 \
  --mirror-header-file /home/user/mirror-header.txt \
  --partition-count 4 --partition-index 0
```

Use partition indices 0, 1, 2, and 3 on four VMs. The exact sorted manifest is
partitioned by index modulo four, so no two workers upload the same object.
Each partition records 13 objects and sets `partition_complete` without publishing
the manifest. Gather the four reports on one VM, then publish:

```sh
python bench/boat/stage_r2.py \
  --credentials /home/user/r2-temporary.json \
  --manifest config/manifests/uncensored-c19ca5c.json \
  --mirror-header-file /home/user/mirror-header.txt \
  --report /home/user/r2-final.json \
  --publish-reports /home/user/part-0.json /home/user/part-1.json \
                    /home/user/part-2.json /home/user/part-3.json
```

Publication requires exactly the canonical 52 unique keys, complete Worker
readback proof in every report, matching hashes/sizes, and unchanged R2 ETags
on a fresh HEAD check. The manifest itself and cloudflared are also retrieved
through the authenticated Worker and hashed before the final complete flag.

Use 16 ranges per object after stopping the CPU guard processes: two objects
retain at most 32 range buffers per VM (2 GiB of payload, plus HTTP/SDK working
buffers). While guards are resident, use four ranges per object. Credential
expiry requires newly derived temporary credentials and a resumed run. At the
end, remove temporary credential copies, stop the no-snapshot sandbox, and tell
Kai that Netops can revoke the staging parent token **only after full verification**.

## Exact node pull

The private Worker origin is the operator's `MIRROR_ORIGIN` (kept out of this repo). It preserves R2 key
paths, including `/models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8/<revision>/<file>`.
The existing downloader's `MIRROR_URL` setting expects the **model base**, whereas
Netops calls the Worker origin `MIRROR_URL`; append the model prefix once in the
node environment below. The downloader then appends `/<revision>/manifest.json`
and `/<revision>/<filename>`. The existing operator-provided
`MIRROR_AUTH_HEADER` authenticates every mirror GET. Do not enable R2 public access.

Include this checkout's `MIRROR_ONLY` support and `pull_mirror.py` in the next node
kit. With those operator-exported values and node dependencies already installed:

```sh
set +x
# Read the complete, space-containing header as data; do not source mirror.env.
export MIRROR_AUTH_HEADER="$(cut -d= -f2- "$HOME/.config/b300/mirror.env")"
export MIRROR_URL="$MIRROR_ORIGIN"/models/DeepSeek-V4.1-Flash-1017-UNCENSORED-FP8
export MIRROR_ONLY=1
export SHARD_WORKERS=16 HASH_WORKERS=4
: "${WEIGHTS_DIR:?set the existing node weights directory}"
python3 scripts/pull_mirror.py --target "$WEIGHTS_DIR" --timeout-seconds 3600
# Stage the already-mirrored amd64 helper before warm (Linux node):
(
  task_cfd=$(mktemp /tmp/cloudflared.XXXXXX)
  trap 'rm -f "$task_cfd"' EXIT
  printf '%s\n' "$MIRROR_AUTH_HEADER" | curl -fsS --proto '=https' --header @- \
    --output "$task_cfd" \
    "$MIRROR_ORIGIN"/tools/cloudflared/2026.10.7/cloudflared-linux-amd64
  printf '%s  %s\n' 1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872 "$task_cfd" | sha256sum -c -
  chmod 755 "$task_cfd"
  mv -f "$task_cfd" /tmp/cloudflared
)
# Or inherit the same exports in the existing warm/node-run invocation:
./scripts/warm.sh --layout tp8
# node-run also inherits MIRROR_ONLY=1, MIRROR_URL, and MIRROR_AUTH_HEADER.
```

The already-staged node amd64 tool URL is
`$MIRROR_ORIGIN/tools/cloudflared/2026.10.7/cloudflared-linux-amd64`,
with sidecar at the same URL plus `.sha256`. Its verified size is 40,274,445
bytes and SHA256 is
`1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872`.

`MIRROR_ONLY=1` verifies the complete mirror manifest first and never attempts HF
downloads. A missing/bad object aborts. Verified local weights/base hardlinks can
still be reused. Default behavior without `MIRROR_ONLY=1` remains HF-first for
existing callers. Auth headers remain in the environment, are redacted in
diagnostics, and are never sent to HF. `pull_mirror.py` stages weights without
starting the expensive serving model.
