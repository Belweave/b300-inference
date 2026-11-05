# Off-node kill/drain stub for Saturday

`scripts/kill-window.sh` is a **plan and mock rehearsal only**. It cannot reach a
gateway, node, GPU, tunnel, provider or paid inference endpoint. There is no live
adapter or `--execute` flag. It reads no credentials or env files, runs no hooks,
and does not change boot defaults, safety classifiers, weights, node registration
or `status.sh --json`. Nothing schedules it or calls it from boot/teardown.

Run from the Mac with fixture identifiers:

```sh
./scripts/kill-window.sh --window-id saturday-fixture --node-id a1b2c3d4
./scripts/kill-window.sh --window-id saturday-fixture --node-id a1b2c3d4 --rehearse drained
./scripts/kill-window.sh --window-id saturday-fixture --node-id a1b2c3d4 --rehearse busy
./scripts/kill-window.sh --window-id saturday-fixture --node-id a1b2c3d4 --rehearse unknown
./scripts/kill-window.sh --window-id saturday-fixture --node-id a1b2c3d4 --rehearse revoke-failed
make kill-window-test
```

No flag (or `--dry-run`) prints the JSON plan. Rehearsals use an in-memory action
log and virtual time, so the 120-second bound takes no real waiting. Exit codes:
`0` for a plan or simulated drain, `2` for a simulated drain timeout/forced stop
(also CLI argument errors), `1` for a simulated revocation failure. Every JSON
result says `live_execution_supported: false`; rehearsal results are not receipts
for a real revocation or shutdown. `DRY_RUN=0`, `EXECUTE=1`, `DRAIN_HOOK` and
`RELAY_SH` cannot turn this into live execution.

## Gateway handoff and ordering

The companion gateway's `keys.sh kill-window` owns event-key revocation. It takes
**no window ID argument**: it closes the sticky `__hackathon_window__` KV flag
first, then deletes all `hackathon-*` key hashes. Other customer keys remain.
The stub's `--window-id` is a local handoff/correlation label, not a way to narrow
the gateway command's scope. The node kit owns local admission closure, drain
and teardown. `kill_and_drain()` defines this adapter boundary; only the in-memory
implementation is provided here:

| Step | Required behavior of a future live adapter |
| --- | --- |
| `revoke_window(window_id)` | Run `keys.sh kill-window` on the operator host, check successful completion and the closed event flag. Construct `{"window_id":"<same label>","scope":"hackathon-*","window_closed":true,"revoked":true}` only after confirmation. This is our adapter receipt, not the CLI's stdout format. A missing, false, wrong-scope or mismatched acknowledgement stops before touching the node. |
| `close_admission(node_id)` | Verify the target matches the `node_id` from `status.sh --json`, then block new node-local work using a tested mechanism. Return exactly `true` after confirmation. |
| `metrics(node_id)` | Return numeric `running` and `queued` totals. Both must be zero for three consecutive samples, two seconds apart. A busy queue, missing/invalid gauge or metrics error resets the streak. |
| `stop_engine(node_id)` | Stop only the selected window's engine and its associated services after drain or the drain bound. No broad process-name kills. |
| `stop_tunnel(node_id)` | Withdraw only that window's connector/relay after engine stop. Attempt withdrawal even if engine shutdown fails. |

The drain bound is explicit, 120 seconds by default, and can only be shortened
with `--drain-seconds 1..120`. At the bound, the rehearsal performs the mocked
stop sequence and reports `drain_timeout` and `forced: true`; unavailable metrics
never count as success. The future adapter must bound its own calls and honor the
remaining reservation deadline; this stub's virtual-time bound does not establish
a wall-clock bound for a live adapter. Repeated revocations/stops must be
idempotent. A stop failure must surface as failure, not a successful drain.

For vLLM the metric source is `vllm:num_requests_running` plus
`vllm:num_requests_waiting`; for SGLang it is `sglang:num_running_reqs` plus
`sglang:num_queue_reqs`. Aggregate all matching series, as the existing
`scripts/drain.py` does. That script does **not** stop admission. No universal
engine drain endpoint is assumed.

The companion implementation is being prepared on `feat/hackathon-window` in
[`infer-gateway`](https://github.com/Belweave/infer-gateway). Its KV propagation
means revocation may take about 60 seconds for new requests and 90 seconds for
running streams; command success does not mean every edge has observed it.
Verified node admission closure is therefore still required before declaring a
drain. If key listing/deletion fails, the gateway flag stays closed and the
command exits nonzero: do not manufacture a successful adapter receipt.

The separate [`POST /admin/safety` global kill switch](https://github.com/Belweave/infer-gateway/blob/main/docs/safety-gateway.md#global-kill-flag)
affects all inference and is not substituted for the event command. This stub
executes neither command and invents no remote kill-window URL. It never calls
`keys.sh start-window`. Confirm the companion contract on its final reviewed
commit before implementing the live adapter.

## Saturday activation work still needed

Implement and review the gateway/event and node-local adapters before relying
on this sequence. Verify target ownership, revocation scope (including active
streams), bounded calls, admission closure, engine/guard/relay ownership, tunnel
withdrawal, and behavior on failures. Keep gateway revocation in effect through
teardown; this path never clears it. Preserve node registration for monitoring.
Provider reservation release is a separate operator step; stopping an engine is
not proof that billing stopped. Existing boot and teardown behavior is unchanged.

## Offline verification

Run the full kit/status suite on Linux (CI or a local CPU-only Linux container
with external networking disabled). It relies on Linux `/proc` and `/bin/bash`;
it is not a macOS compatibility suite. The kill-window and registration tests
also run on the Mac.

```sh
make kill-window-test PYTHON=python3
python3 -m unittest discover -s tests -p test_status.py -v
python3 -m unittest discover -s tests -p test_register_node.py -v
make test lint dry-run PYTHON=python3
```

The status tests use fake GPU output, temporary runs/weight files, and loopback
health fixtures. Registration tests use fake SSH and temporary config files
(real `ssh -G` only parses a local fixture). No node connection is required.
