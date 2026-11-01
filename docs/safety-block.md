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

## 4. Division of labor (gateway contract)

Netops published the gateway↔node contract (`infer-gateway-safety`, `docs/safety-contract.md`, branch `safety-csam-block`). It moves all request/response handling to the gateway and leaves the node a single stateless scorer. This doc follows that contract; the earlier inline-relay design is superseded.

**Gateway (Netops) owns:** extracting every client string from a request (`messages`, `prompt`, `input`, `suffix`, `system`, `instructions`, `tools`, `functions`, `response_format`), extracting generated text on the output side (visible content, `reasoning`/`reasoning_content`, tool-call names/arguments), 16,000-char windowing with 128-char overlap (≤4 pieces in parallel, any flagged piece blocks), the input check before anything goes upstream, the rolling streaming-output check (a call once 512 new chars arrive, or after 1 s of pending text, and at end of stream), refusing non-text parts (`image_url`, `input_audio`, `file`, `image`) with 400 before classification, fail-closed enforcement, key-kill, the global kill flag, sealing age-encrypted evidence to R2 Object Lock, and the NCMEC alert.

**Node (Chief Engineer) owns:** one endpoint, `POST /v1/safety/csam`, that scores a single piece of text and returns a decision. Stateless, no evidence, no enforcement. The node is reached over the existing tunnel; the SSE relay routes `/v1/safety/*` to this scorer in-process (it adapts to the Shieldstral engine, which speaks `/v1/chat/completions` — it does not blind-proxy).

## 5. The node endpoint

```
POST {CLASSIFIER_ORIGIN}/v1/safety/csam
Authorization: Bearer {CLASSIFIER_KEY}      # required when the node has a key set; separate from the engine key; never logged
Content-Type: application/json
{"text": "<utf-8 text>", "phase": "input" | "output"}
```
Success is **HTTP 200 only**, body `{"flag": true|false, "score": <finite number, higher = riskier>, "model": "<id@revision>"}`:
- `flag` is the decision — the **node** applies the threshold (default 0.5 on Shieldstral's renormalised yes/no score). The gateway does not threshold; it records the score in sealed evidence and keeps the highest.
- `model` identifies the classifier + version, e.g. `mistralai/Shieldstral-1.0-3B@<policy_version>`, so evidence is auditable to an exact classifier+policy.
- The gateway **fails closed** on anything that is not a clean 200 of that shape. So the node returns a non-200 rather than guessing: **503** on classifier error/timeout/unavailability, **401** on bad/missing auth, **400** on a malformed body or bad `phase`, **405** on non-POST, **404** on other `/v1/safety/*`. The text is never echoed back.
- **Canary:** the synthetic token `CANARY-CSAM-TEST-7f3a` makes the endpoint return `200 {flag:true}` **without calling the model**, so an end-to-end drill works even if the model is down. This is the only trigger; no real content exists anywhere in the code or tests.

Thresholds and fail-closed: the decision threshold lives in the node (tune per the eval in §9). "Fail closed" is realised jointly — the node surfaces unavailability as 503, and the gateway turns any non-200 into a refused request. When the node (hence the classifier) is down, inference is refused, which is intended.

## 6. Performance budget

Per the contract: 3,000 ms timeout per call (`CLASSIFIER_TIMEOUT_MS`, node `SAFETY_TIMEOUT_SECONDS` defaults to 3 s to match). Streaming holds output until it is classified, so classifier latency adds directly to token delivery; at ~100 tok/s a stream makes ~1 output call/s. At 128 concurrent streams plan for **~130 output calls/s plus input calls**; output calls are ≤512+128 chars, input calls up to 16,000 chars (a 262K-token prompt ≈ 70 input calls, 4 in parallel, before the request starts). **Target p99 < 100 ms.** The node scorer is fully async, bounded by a process-wide inflight semaphore (`SAFETY_MAX_INFLIGHT`, default 128), reuses one aiohttp session to the engine, and never blocks the loop. A mocked-latency load test (`scripts/safety_loadtest.py`) drives 130 rps at concurrency 128 and reports p50/p95/p99 and non-200s against the 100 ms budget; it uses a mock classifier, not the real model, so the number it reports is the relay/endpoint overhead plus the injected latency, **not** real Shieldstral latency (that needs the model on B300 — untested).

## 7. Evidence, retention, alert (gateway-owned; node does none of it)

All evidence handling moved to the gateway per the contract. For the record, the agreed shape (Netops implements, counsel confirms): evidence encrypted with **age** to Preetham's **public** key only, written to **R2 Object Lock (compliance mode, write-once)**, **exempt from the 90-day ZDR purge**, preserved per **18 U.S.C. § 2258A(h)** as amended by the **REPORT Act (2024)** — a CyberTipline report triggers a **one-year** preservation duty for the report and reasonably accessible related data, secured with minimized access per the current **NIST Cybersecurity Framework**; longer voluntary retention allowed. Counsel must confirm the period, access controls, and whether § 2258B designated-vendor rules (encryption, annual independent audit, access minimization) apply. The score the node returns is what the gateway records; the node keeps nothing. The **Notify alert** (no content, no key, incident id only, routed urgent, NCMEC filing is Preetham's manual step) is also gateway-fired.

## 8. Config surface (node)

`SAFETY` (0/1), `SAFETY_URL` (classifier engine, loopback-only), `SAFETY_MODEL`, `SAFETY_MODEL_REVISION` (folds into the `model` id the endpoint returns; defaults to the policy version), `SAFETY_THRESHOLD`, `SAFETY_TIMEOUT_SECONDS` (default 3 s, matching the gateway), `SAFETY_MAX_INFLIGHT` (default 128), `SAFETY_CLASSIFIER_KEY` (optional; when set the `/v1/safety/csam` endpoint requires `Authorization: Bearer` it; separate from the inference key; never logged). The node runs the Shieldstral classifier engine as a prerequisite; `age`/evidence are gateway-side now, not a node dependency.

## 9. Test plan

**Unit (in this PR, fully offline, mocked classifier, inert canary only).**
- Config parse/validate, incl. rejecting a non-loopback `SAFETY_URL`, a bad threshold, and the optional key; `model` id format.
- `parse_score` yes/no softmax from synthetic logprob payloads.
- `score_text`: the canary `CANARY-CSAM-TEST-7f3a` short-circuits to `flag:true` **without touching the model**; benign text gives `flag = score ≥ threshold`; a classifier failure propagates (→ 503).
- Endpoint: 200 happy-path shape `{flag,score,model}`; 401 on missing/wrong key and 200 with the right key (and the key never appears in logs); 400 on a bad body/`phase`; 405 on GET; 503 when the classifier raises; 404 on other `/v1/safety/*`.
- `SAFETY=0` leaves the relay path byte-for-byte untouched (reuses the existing relay fixtures).
- A short in-process **load test** (`scripts/safety_loadtest.py`, also exercised as a unit test) drives the endpoint with a **mocked-latency** classifier at ~130 rps / 128 concurrency and asserts zero non-200s and p99 under the 100 ms budget. This measures endpoint/relay overhead + injected latency, **not** real model latency.

**Red-team / real-model eval (plan only; needs Preetham + counsel approval; download nothing now).**
- Measure recall (missed blocks) and false-block rate on **vetted, access-controlled benchmark sets**, used strictly under their terms — the classifier vendor's published safety eval sets, standard academic guard benchmarks (WildGuard, Aegis v2, UnsafeBench), and any NCMEC/industry-sanctioned evaluation corpus. **We never create, fetch, or store sexual content involving minors.** Benign "false-positive bait" (adult, medical, parenting, abuse-prevention, coding) plus the abstract canary drive our own in-repo set.
- Threshold tuning from the FP/FN curve.
- **End-to-end drill before any non-Preetham key exists:** firing the canary through the gateway must demonstrably (1) block the request, (2) kill the key, (3) set the global kill flag, (4) seal encrypted evidence, (5) fire the Notify alert — proven once, start to finish, across gateway + node.
- Real Shieldstral latency/throughput at 64–128 concurrent streams against the p99 < 100 ms budget, and the VRAM hit to the serving KV pool.

## 10. Honest timing (per component)

- Node `/v1/safety/csam` endpoint + relay route + unit tests + mocked load test (this PR): **drafted today**, ~0.5–1 day to review/harden.
- Gateway enforcement — key-kill, global kill flag (fail-closed default), text extraction/windowing, R2 object-lock WORM evidence, NCMEC alert — **Netops**, draft on branch `safety-csam-block`; ~1–2 days to finish + verify.
- Shieldstral classifier actually served on the node + real latency/VRAM fit: part of a bring-up window; single-GPU-slice serving and the p99 target are **not proven on B300**.
- Threshold tuning + red-team eval + the end-to-end drill: ~1 day **after** approvals and eval-data access.
- Counsel review of evidence handling / retention: separate, **no date we control**.

**None of this is enforced today.** Total engineering ≈ 2–3 days once approvals land; the drill gates any non-Preetham access.

## 11. Open questions

**For Preetham:**
1. Enforce-before-serve (option a) vs. take the /policy/safety claim down until the block is live and drilled (option b)? Recommendation: do (b) now (≈15-min site edit), keep only your key, restore the page after the drill passes (a).
2. Which model: Shieldstral 3B only, or add Qwen3Guard-Stream 0.6B on the output path for token-level streaming (defense in depth, more VRAM)?
3. Stage thresholds and how aggressive the output cadence is (latency vs. leakage of partial tokens before a mid-stream block).
4. Scope confirm: image inputs — block now (route images through a multimodal guard) or text-only first and reject image parts while SAFETY=1?
5. The `CLASSIFIER_KEY` secret value (node + gateway share it; set via a masked secret card, never in chat) and the age public recipients file path for the gateway evidence store.

**For counsel:**
1. Confirm REPORT Act / § 2258A(h) handling: preservation period (≥1 year), secure-storage and minimized-access requirements, NIST CSF alignment.
2. Whether § 2258B NCMEC-designated-vendor rules (encryption, annual independent audit, access minimization) apply to us as a provider sealing evidence.
3. What we may retain and how long vs. what must be surfaced to NCMEC, and who files (Preetham) vs. any mandatory-provider-report timing.
4. Whether sealing the offending *text* (not imagery) changes the obligations.
