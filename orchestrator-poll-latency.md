# Orchestrator poll latency on the critical path

**Finding:** every DAG handoff (a producer finishing → its dependent being
dispatched) costs up to one full `WAIT_POLL_INTERVAL_S` (currently **10s**),
because the orchestrator notices completions only by polling S3, not by an event.
On a critical path with several handoffs this is tens of seconds of pure idle.

## Evidence

peerdb PR run, orchestrator stream `i-053cf63593381f874` (times relative to the
Config Workflow KICK):

| Producer `[DONE]` | Dependent head `[KICK]` | Gap |
|---|---|---|
| Config Workflow `6.7s` | Flow images `14.0s` | **7.3s** |
| Go modules `33.3s` | Go build cache `43.0s` | **9.7s** |
| Go build cache `142.5s` | flow-test matrix `144.0s` | 1.5s (landed just before a sweep) |

The 1.5s–9.7s spread is exactly "where in the 10s window did the producer
finish" — not per-job work. `[KICK]→[PICK]` (SQS delivery + the runner's own
long-poll) is a separate ~7–8s and is not what this note is about.

## Root cause

`praktika/orchestrator/state.py`:

- `WAIT_POLL_INTERVAL_S = 10` (module constant, line ~130).
- `WorkflowState.wait()` (~line 2998): the completions queue was retired
  ("phase 2b"), so the loop is: dispatch ready jobs → `time.sleep(WAIT_POLL_INTERVAL_S)`
  → `sweep_cancel()` / `sweep_completions()` / `sweep_liveness()` → re-evaluate.

So a job that finalizes mid-interval isn't seen until the next sweep, and its
dependents aren't dispatched until then. Worst case per handoff = the full
interval; average ≈ half.

The constant's own comment already frames it as a reactivity knob
("Kept short so the orchestrator reacts quickly to … finished jobs").

## Suggested fix

1. **Make it `Settings`-configurable** (default unchanged) so a project can tune
   it without forking:
   ```python
   WAIT_POLL_INTERVAL_S = int(getattr(Settings, "WAIT_POLL_INTERVAL_S", 10) or 10)
   ```
   and add `WAIT_POLL_INTERVAL_S` to `Settings` + its `__all__` allowlist.
2. **Lower the default / project override to ~3s.** Each critical-path handoff
   then drops from avg ~5s / worst 10s to avg ~1.5s / worst 3s. For peerdb the
   handoffs that gate the matrix (Config → cortage heads; go-build-cache → the 12
   non-chained matrix perms) are ~2–3 levels → ~10–15s of wall clock.

### Trade-off
Shorter interval = proportionally more S3 sweeps. Each `wait()` does
`sweep_cancel` + `sweep_completions` + `sweep_liveness` (S3 LIST/GET over
`runs/<run_id>/…`). 10s→3s is ~3× the sweeps — a few hundred extra cheap requests
per run. Watch S3 request cost / rate only on very wide fan-outs.

### Possible follow-up (bigger)
Event-driven completion (a lightweight completions signal, or S3 event
notifications) would remove the latency entirely instead of trading it against
poll frequency — but the poll-interval knob is the cheap 90% win.

## Note for cortages
Within a cortage this latency does **not** apply: the chain driver runs members
back-to-back on one runner with no orchestrator poll between them. So moving a
critical-path producer→consumer pair into one cortage (e.g. peerdb's
`Go modules -> Go build cache`) already removes its handoff poll; the interval
knob covers the remaining non-chained handoffs (cortage *heads* waiting on their
external deps, and the matrix waiting on go-build-cache).
