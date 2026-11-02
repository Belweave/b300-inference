# Output guard and pre-deploy evaluation

`POST /v1/safety/csam` still accepts `{text, phase}` and returns
`{flag, score, model}`. Input uses the Shieldstral minors policy. Output defaults
to Qwen3Guard-Gen's **Sexual Content or Sexual Acts** category as a first pass.
Its severity is 1.0 for Unsafe, 0.5 for Controversial, and 0.0 for Safe; absence
of that category always scores 0.0. Other Qwen categories never trigger a block.

Shieldstral confirms **every output**, using the same minors policy as input.
The Boat CPU proxy benchmark found three output false negatives caused by Qwen
skipping that confirmation. Qwen's Safe/low score can no longer bypass the
authoritative minors policy. Only confirmation can set `flag=true` (apart from
the inert test canary). The returned score is the authoritative minors score,
and `model` contains both IDs: `<qwen-id>+confirm:<shieldstral-id>`.
Qwen never independently blocks adult
sexual content; the policy excludes adult content and all other categories.
Model predictions can still be wrong, so this policy scope is not an accuracy
guarantee. A required backend failure returns 503, including a failed confirmation.
The queue, Qwen call, and confirmation share one slot and one overall deadline.

The endpoint is stateless text-in/decision-out, so it uses the OpenAI-compatible
**Qwen/Qwen3Guard-Gen-0.6B**, served by SGLang/vLLM, with an assistant response to
a neutral user prompt. Qwen3Guard-Stream's token-level head needs a custom
per-token harness. Stream is a future optimization for gateway rolling output
calls, rather than a stateless scorer replacement.

| Setting | Default | Meaning |
| --- | --- | --- |
| `SAFETY_OUTPUT_GUARD` | `qwen3guard` | Use `shieldstral` for direct minors scoring on output. |
| `SAFETY_QWEN_URL` | `http://127.0.0.1:8601` | Loopback engine URL, separate from Shieldstral. |
| `SAFETY_QWEN_MODEL` | `Qwen/Qwen3Guard-Gen-0.6B` | Served model name. |
| `SAFETY_QWEN_REVISION` | `unpinned` | Revision label in results; configure the actual deployed revision. |
| `SAFETY_QWEN_THRESHOLD` | `0.5` | Retained for configuration compatibility; cannot skip minors confirmation. |

With safety enabled and the default output guard, both loopback engines must be
provisioned separately. The relay does not launch engines. The revision setting
labels results; it does not itself pin engine weights. Choose and pin the actual
engine revision during provisioning.

## Offline harness

`python3 scripts/safety_eval.py` defaults to mock backends and an embedded,
synthetic fixture. It contains only inert placeholders, a canary, benign strings,
and adult/medical/parenting/coding topic labels. A passing mock run verifies wiring
and metric calculations only. The placeholder marker has no live-model meaning.
No real evaluation data belongs in this repository.

For model evaluation before deployment, obtain a Preetham/counsel-approved JSONL
file **outside the repository**, then run in an environment with the configured
loopback engines available:

```sh
python3 scripts/safety_eval.py --live --dataset /approved/external/evaluation.jsonl --json
```

Each JSONL line has this schema:

```text
{"text": str, "phase": "input"|"output", "expected": bool, "note": optional str}
```

`expected` means a violation of the **minors policy**, not a violation of any
broader Qwen category. Include positives and negatives for each phase. The
harness rejects paths within the repository, including symlinks resolving there;
the only internal fixture is the embedded synthetic set. It never writes dataset
contents or prints text or notes. Example diagnostics contain only index, phase,
expected/predicted flags, and match status. The data-source notice goes to stderr
so `--json` stdout remains machine-readable.

Metrics and confusion matrices (`tp`, `fp`, `tn`, `fn`) are reported separately
for input and output. Recall and FNR divide by actual positives; FPR divides by
actual negatives; precision divides by predicted positives (0 if none).
Missing actual positives or negatives makes the relevant rate undefined and
fails the gates. Each phase must reach `--min-recall` (default 0.99) and stay at
or below `--max-fpr` (default 0.02). Exit codes: 0 for passing gates, 1 for failed
gates, 2 for dataset/configuration/classifier failure. Backend failures cannot
produce a passing evaluation.
