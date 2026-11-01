# Child-safety hard-block: design

**Status:** draft for Preetham + counsel. Nothing here is live. P0, above the noon A/B and any listing.
**Scope of this doc:** the whole flow. Node-side classifier is Chief Engineer's; gateway enforcement, key-kill, global kill flag and the write-once evidence store are Netops'. We coordinate through this doc only.

## 1. Policy (exact)

Block **only** sexual content involving a minor: content that sexualizes a minor (a real or fictional person under 18), depicts or describes sexual activity involving a minor, or requests/solicits such content. One category, nothing else.

Explicitly **not** blocked, and must pass:
- adult sexual content;
- non-sexual content about or for children (education, parenting, medical, abuse-prevention, mandatory-reporting guidance, survivor support);
- every software, security, cyber, and other topic. The model is abliterated on purpose; this block does not re-add general refusals.

On a hit: block the request, kill the key (Netops), seal encrypted evidence, alert Preetham to file with NCMEC (filing is his manual step). Fail **closed**: if the classifier is down or unreachable, block.

## 2. Classifier choice

Requirement: self-hostable (open weights, run on our own node), a usable minors+sexual category, a **custom single-category policy** so nothing else is blocked, low latency, and a license that allows this commercial use. No hosted moderation API — the ZDR promise on /policy says prompts never leave, so shipping prompts to a third party is off the table.

Survey as of Nov 2026:

| Model | Size | License | CSE/minor category | Custom single-category policy | Latency notes |
|---|---|---|---|---|---|
| **Shieldstral 1.0 (Mistral)** | 3B (4B params w/ vision) | Apache-2.0 | Via free-form policy query (not a fixed label) | **Yes** — policy is a natural-language yes/no query at inference, one policy per call | Single forward pass, 1 generated token; fits 16 GB BF16; strong F1 (e.g. WildGuardTest prompt 88.1, response 80.4; UnsafeBench 81.8) |
| gpt-oss-safeguard-20b | 21B (3.6B active) | Apache-2.0 | Via custom policy prompt | Yes (bring-your-own policy, reasoning CoT) | Reasoning model; medium/high effort adds latency and output tokens; ~16 GB VRAM (20b) |
| Qwen3Guard-Stream | 0.6B/4B/8B | Apache-2.0 | Fixed category "Sexual Content" incl. illegal acts; **token-level streaming head** | Partial — fixed taxonomy, not a single-category custom policy; would need output filtering to one category | Purpose-built for real-time token-stream moderation; lowest latency of the set |
| Llama Guard 4 | 12B | Llama 4 Community (not OSI; 700M MAU clause) | **S4 Child Sexual Exploitation** (fixed) | Partial — can select categories, but a fixed taxonomy | ~1 s P50 hosted; 12B is heavier to self-host |
| Granite Guardian 3.3 | 8B | Apache-2.0 | `custom_criteria` natural-language definition | Yes | yes/no scoring; 8B |
| ShieldGemma 2 | 4B | Gemma (use-policy) | "Sexually explicit" (**image** classifier) | Policies are image-oriented; not a text single-category fit | Image moderation focus |
| Nemotron Safety Guard 8B v3 | 8B | NVIDIA open | Has a distinct **"Sexual (minor)"** category | Category selection | 8B text guard |
| SafeCircle Vigil | 322M | SafeCircle Research (no commercial use) | 7 child-risk categories | n/a | Research checkpoint, 12–19% FPR on unseen text; **license blocks commercial use** — rejected |

**Recommendation: Shieldstral 1.0 3B (Apache-2.0), primary.** Reasons:
1. True single-category custom policy — we write one yes/no query for exactly "sexual content involving a minor", so nothing else can be blocked. That matches the policy better than any fixed-taxonomy guard.
2. Apache-2.0: clean commercial self-hosting, no MAU clause, no copyleft.
3. One forward pass, one token + logprobs → a continuous score we can threshold. Lowest-overhead decision shape, ~16 GB BF16 on one GPU.
4. Serves on the same SGLang stack we already run, OpenAI-compatible, so the node client is a plain /v1/chat/completions call.
5. Published eval F1 is at or above the larger guards on the benchmarks that matter here (prompt/response harm, UnsafeBench).

**Secondary / defense-in-depth option for Preetham to weigh:** add **Qwen3Guard-Stream (0.6B)** on the output path for true token-level streaming detection (it was built to judge each generated token in real time), with Shieldstral as the primary input+output policy judge. This is optional; the baseline below uses Shieldstral for both input and rolling-output checks to keep one model and one policy. gpt-oss-safeguard-20b is the fallback if Shieldstral's single-category recall proves weak in the eval.

> The classifier is itself an abliteration-free, safety-tuned model; it is never the abliterated serving model.

## 3. Placement and memory cost

- Separate engine, **not** in the serving model's process. Run Shieldstral as its own SGLang server bound to **127.0.0.1:8600** (loopback only; never a public port).
- GPU: the 8×B300 node has large HBM per GPU; the serving model is TP8 across all eight. Put the classifier on a **small slice of one GPU** (MPS or a MIG-style partition) rather than a ninth device. BF16 weights ≈ 8 GB (3B) / ~16 GB with activations + a small KV cache for short classification sequences. Budget **~16–20 GB on one GPU**, taken from that GPU's share of `MEM_FRACTION` headroom — measure the real fit in the noon window before trusting it.
- CPU-only fallback exists but is too slow for inline per-request + per-chunk checks at 64–128 concurrent streams; GPU slice is the plan.
- The classifier adds VRAM pressure that competes with the serving model's KV pool. This must be accounted for in the 128-vs-64 fit check (see `warm_logic.concurrency_fit`): the classifier's reservation reduces pool headroom, so enabling SAFETY may shift the memory-fit fallback. Flagged as an open question to measure.

## 4. Check flow

Front door is the node SSE relay (`scripts/sse_relay.py`) on :8000, which already sits in front of the engine on :8001. The block is wired there, gated by `SAFETY=1` (off = today's behavior, byte-for-byte).

**Input check (before inference).** Extract text from the request:
- `/v1/chat/completions`: every message's text content (string or structured parts). Non-text parts (images) are marked present; image moderation is a separate open item (Shieldstral and ShieldGemma 2 can do images later).
- `/v1/completions`: the prompt.
- **Tool / reasoning content in the request** is user-supplied and is checked like any other text.
- Long inputs are split into overlapping windows (`SAFETY_WINDOW_CHARS` 16000 / overlap 1000) so a 262K-context prompt is fully covered; all windows scored concurrently under a process-wide inflight semaphore.
- Any window scoring ≥ threshold ⇒ block before the engine is ever contacted.

**Streaming output check (rolling chunks).** As the engine streams, assistant deltas are accumulated and scored every `SAFETY_CHUNK_TOKENS` (default 64, approximated by chars/4) over a rolling window = last overlap already-checked + new text, prefixed with the last 2000 chars of the user prompt for context (only the assistant span is judged). A final partial chunk is scored at end-of-stream.
- **Assistant reasoning / `reasoning_content` deltas and tool-call argument deltas are checked the same way** — they can carry generated text.
- On an output hit mid-stream: cancel the engine request (close upstream so it aborts), seal the incident, and end the client stream with one `data: {"error": ..., "incident_id": ...}` event + `[DONE]`. Tokens already sent cannot be recalled — a reason to keep the chunk cadence small.

**Non-streaming responses** are buffered and scored before returning.

## 5. Thresholds and fail-closed

- Score is Shieldstral's renormalised softmax over the yes/no answer token. Default decision threshold **0.5**; tune on the eval (section 10) and pin per-stage. Likely a lower (stricter) output threshold than input once we see FP/FN rates.
- **Fail closed.** With `SAFETY=1`, if the classifier returns an error, times out, returns neither yes nor no, or `/health` is unhealthy, the request is **blocked** with reason `classifier_unavailable` (distinct from `content_blocked` in telemetry; both block). The relay never "fails open" to the engine while SAFETY is on.
- If `SAFETY=1` cannot even construct (bad config, no recipients file) the node should refuse to advertise readiness — a safety-on node that can't block must not serve. (Readiness wiring is a follow-up; noted as open.)

## 6. API contract: node classifier → gateway Worker

The classifier verdict is produced and enforced **on the node** (block happens at the relay). The gateway needs to be told an incident happened so it can kill the key, set the global kill flag, and move evidence to WORM. Two mechanisms:

1. **Response signal (always).** A blocked request returns an OpenAI-shaped error the Worker can recognize:
   - Non-stream: HTTP **403**, body `{"error":{"message":"blocked by safety policy","type":"safety_block","code":"content_blocked"|"safety_unavailable","incident_id":"inc_<ts>_<rand>"}}`.
   - Stream: 200 SSE, one `data: {"error":{...,"incident_id":"..."}}` event then `[DONE]`.
   - Header on both: `X-Safety-Block: 1` and `X-Safety-Incident: inc_...` so the Worker can act without parsing the body.
2. **Incident webhook (optional, `SAFETY_INCIDENT_WEBHOOK`).** On a `content_blocked` hit the relay POSTs a minimal JSON to a loopback/gateway endpoint Netops owns: `{incident_id, created_at, stage:"input"|"output", policy_version, model, request_id, key_fingerprint}`. **No content, no raw key.** `key_fingerprint` is a truncated hash the Worker passes down or that the relay derives from the forwarded Authorization; the Worker maps it back to the key to revoke.

**Verdict JSON (internal, also the shape sealed):**
```json
{
  "incident_id": "inc_20261101T143000Z_9f3a1c20",
  "created_at": "2026-11-01T14:30:00Z",
  "stage": "input",
  "hit": true,
  "reason": "content_blocked",
  "score": 0.97,
  "threshold": 0.5,
  "policy_version": "sha256:1a2b3c4d5e6f7a8b",
  "model": "mistralai/Shieldstral-1.0-3B",
  "request_path": "/v1/chat/completions",
  "request_id": "...",
  "key_fingerprint": "sha256:...(truncated)"
}
```
Incident id: `inc_` + UTC compact timestamp + `_` + 8 random hex.

**Who does what on a hit:** relay blocks the request + seals the node-local evidence copy + fires the webhook/headers. **Gateway (Netops):** on the 403/`X-Safety-Block`, revoke that key, set the global kill flag (every request checks it; defaults to blocked if the flag store is unreachable), copy evidence to R2 object-lock, and trigger the NCMEC alert. The node also records a kill marker so it stops accepting that key locally even before KV propagation.

## 7. Evidence format

- Record = the verdict JSON above **plus** the full offending text(s) and any generated output captured.
- Encrypted with **age** to Preetham's **public** recipients only: `age -R $SAFETY_AGE_RECIPIENTS_FILE` (placeholder `/etc/b300/safety/recipients.txt`). **This code never generates, reads, or touches any private key.** Decryption is Preetham's, offline.
- JSON piped to `age` on **stdin** → `$SAFETY_SPOOL_DIR/<incident_id>.age`, mode 0600, write-once (never overwrite an existing id). Plaintext is never written to disk.
- If the recipients file is missing/empty: still **block**, write nothing in plaintext, record a seal-failure so the gateway alert still fires and a human is paged. A safety block must never depend on sealing succeeding.
- Node spool is a **staging** copy. The durable **write-once** store is **R2 Object Lock (compliance mode)** on the gateway side (Netops), which is also where the retention exemption lives.
- **Retention:** this evidence is **exempt from the 90-day ZDR purge**. Preserved per **18 U.S.C. § 2258A(h)** as amended by the **REPORT Act (2024)**: a CyberTipline report triggers a **one-year** preservation obligation for the report contents and reasonably accessible related data; secure storage with minimized access, following the current **NIST Cybersecurity Framework**. Longer voluntary retention is allowed. **Counsel must confirm** the retention period, access controls, and whether any NCMEC-designated-vendor handling rules (NIST, minimized access, encryption, annual independent audit — § 2258B) apply to us.

## 8. Notify alert payload (no content in the push)

On a `content_blocked` hit, Notify push to Preetham's active iPhone (`devices_list` → active device). **No content, no key, no victim data.**
- title: `Safety block — action needed`
- summary/banner: `A request was blocked and the key was killed. Incident <incident_id>. File with NCMEC.`
- body (markdown): incident id, UTC + ET timestamp, stage (input/output), policy_version, model, and a line "Decrypt the sealed evidence locally with your age key; do not paste content into chat." Plus the NCMEC reporting URL.
- url: deep-links to the incident record in the gateway admin surface (Netops), not to any content.
- Idempotency-Key = incident_id (one push per incident).
- Routed as urgent/approval (page), not an FYI. Filing with NCMEC stays Preetham's manual legal step.

## 9. Config surface (node)

`SAFETY` (0/1), `SAFETY_URL` (loopback), `SAFETY_MODEL`, `SAFETY_THRESHOLD`, `SAFETY_CHUNK_TOKENS`, `SAFETY_WINDOW_CHARS`, `SAFETY_OVERLAP_CHARS`, `SAFETY_TIMEOUT_SECONDS`, `SAFETY_MAX_INFLIGHT`, `SAFETY_SPOOL_DIR`, `SAFETY_AGE_RECIPIENTS_FILE`, `SAFETY_AGE_BIN`, `SAFETY_INCIDENT_WEBHOOK`. `age` is an OS binary prerequisite in the node image.

## 10. Test plan

**Unit (in this PR, fully offline, mocked classifier, inert placeholders only).**
- Config parse/validate, incl. rejecting a non-loopback `SAFETY_URL` and a bad threshold.
- Text extraction for string + structured chat parts, completions, non-text parts, bad JSON.
- Softmax score parsing from synthetic logprob payloads.
- Input gate: hit on the inert marker `TEST_TRIGGER_CSAM_PLACEHOLDER`, pass on benign text; long-input window splitting.
- Output scanner: flags a chunk containing the marker, passes benign; reasoning/tool deltas checked.
- Fail-closed: classifier raising / unhealthy ⇒ block with `classifier_unavailable`.
- Seal: fake `age` binary writes a 0600 file, write-once, seal-failure still blocks and writes no plaintext; incident-id format.
- `SAFETY=0` leaves the relay path untouched (reuse existing relay fixtures).

**Red-team / real-model eval (plan only; needs Preetham + counsel approval; download nothing now).**
- Measure recall (missed blocks) and false-block rate on **vetted, access-controlled benchmark sets**, used strictly under their terms — e.g. the classifier vendor's published safety eval sets, standard academic guard benchmarks (WildGuard, Aegis v2, UnsafeBench), and any NCMEC/industry-sanctioned evaluation corpus. **We never create, fetch, or store sexual content involving minors.** Non-explicit, abstract trigger phrasing and benign "false-positive bait" (adult, medical, parenting, abuse-prevention, coding) drive our own in-repo set.
- Threshold tuning per stage from the FP/FN curve.
- **End-to-end drill before any non-Preetham key exists:** a mocked/abstract trigger must demonstrably (1) block the request, (2) kill the key, (3) set the global kill flag, (4) seal encrypted evidence, (5) fire the Notify alert — proven once, start to finish.
- Latency/throughput of the classifier at 64–128 concurrent streams, and the VRAM hit to the serving KV pool.

## 11. Honest timing (per component)

- Node classifier service + relay hook + unit tests (this PR): **drafted today**, ~1 day to review/harden.
- Gateway enforcement (key-kill on 403, global kill flag with fail-closed default, incident endpoint, R2 object-lock WORM, NCMEC alert plumbing) — **Netops**, ~1–2 days.
- Classifier model served on the node + fit/VRAM measurement: part of a bring-up window; single-GPU-slice serving not yet proven on B300.
- Threshold tuning + red-team eval + the end-to-end drill: ~1 day **after** approvals and eval-data access.
- Counsel review of evidence handling / retention: separate, **no date we control**.

**None of this is enforced today.** Total engineering ≈ 2–3 days once approvals land; the drill gates any non-Preetham access.

## 12. Open questions

**For Preetham:**
1. Enforce-before-serve (option a) vs. take the /policy/safety claim down until the block is live and drilled (option b)? Recommendation: do (b) now (≈15-min site edit), keep only your key, restore the page after the drill passes (a).
2. Which model: Shieldstral 3B only, or add Qwen3Guard-Stream 0.6B on the output path for token-level streaming (defense in depth, more VRAM)?
3. Stage thresholds and how aggressive the output cadence is (latency vs. leakage of partial tokens before a mid-stream block).
4. Scope confirm: image inputs — block now (route images through a multimodal guard) or text-only first and reject image parts while SAFETY=1?
5. Path to your age public recipients file, and where the decrypt key lives (never on the node).

**For counsel:**
1. Confirm REPORT Act / § 2258A(h) handling: preservation period (≥1 year), secure-storage and minimized-access requirements, NIST CSF alignment.
2. Whether § 2258B NCMEC-designated-vendor rules (encryption, annual independent audit, access minimization) apply to us as a provider sealing evidence.
3. What we may retain and how long vs. what must be surfaced to NCMEC, and who files (Preetham) vs. any mandatory-provider-report timing.
4. Whether sealing the offending *text* (not imagery) changes the obligations.
