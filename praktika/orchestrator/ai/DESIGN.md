# AI orchestration — design

This package is the plumbing skeleton for letting an LLM observe a running
workflow and (eventually) decide what to do — skip/cancel/reorder jobs,
auto-fix failures, triage review findings (see ClickHouse/ClickHouse#101924).

**Current scope is intentionally minimal:** the orchestrator consults an
*advisor* on every workflow update, the advisor calls a pluggable model
provider, and the whole turn is recorded. The only provider is a **mock that
does nothing**. The goal is to lock in the interfaces and the flow so real
model SDKs, action dispatch, and cost accounting slot in later without
rewriting the orchestrator loop.

## Flow

```
orchestrator loop (_orchestrate_single)
  └─ while state.not_finished():
       kick ready jobs
       state.wait()                     # blocks; sweeps S3 → a job result lands
       advisor.on_workflow_update(state, event)
            ├─ _delta(state)            # jobs newly terminal since last turn
            │     └─ (none) → return    # idle ticks fire no turn
            ├─ build_observation(...)   # serializable snapshot of the run
            ├─ provider.decide(obs) → Turn(reasoning, decision, usage)
            ├─ ledger.add(turn.usage)   # cost/token accounting
            └─ trace.record(obs, turn)  # stdout one-liner + JSONL
       patch top-level check
  └─ finally: advisor.finalize()        # prints run cost/usage total
```

The hook fires **only when a job newly reaches a terminal state** (success /
failure / skipped / cancelled), i.e. on an actual "result received", not on
every poll tick.

## Module layout

| File | Responsibility |
|---|---|
| `provider.py` | The stable contract: `AIProvider` ABC + `Observation` / `Turn` / `Usage` dataclasses + a name→class registry (`register`, `resolve`). |
| `mock.py` | `MockProvider` — returns a no-op `Turn`, zero usage, no network. |
| `trace.py` | `TraceLogger` (stdout + `TEMP_DIR/ai/turns.jsonl`) and `UsageLedger` (run totals — the cost seam). |
| `__init__.py` | `Advisor` (delta detection, calls the provider, records turns) + `build_observation` + `Advisor.maybe_create` factory. |

## The provider contract

`Observation` (in) and `Turn` (out) are plain, JSON-serializable dataclasses —
the boundary the orchestrator depends on. A real provider:

1. formats the `Observation` into a prompt + tool specs,
2. runs the SDK's tool-use loop,
3. fills `Usage` from the SDK's token report and computes `cost_usd` from a
   pricing table,
4. returns a `Turn` with `reasoning` and (later) a populated `decision`.

None of that touches `praktika/orchestrator/__init__.py`. `Advisor._safe_decide`
wraps `provider.decide` so a provider bug becomes an error `Turn`, never a
crashed run.

### Adding a provider

```python
# praktika/orchestrator/ai/anthropic.py
from .provider import AIProvider, Turn, Usage

class AnthropicProvider(AIProvider):
    name = "anthropic"
    def decide(self, observation) -> Turn:
        ...  # call SDK, fill Usage, return Turn
```

Register it in `provider.py` (next to the mock) and set
`AI_PROVIDER = "anthropic"` in `ci/settings/settings.py`.

## Configuration

`praktika/settings.py` (library defaults — AI off):

| Setting | Default | Meaning |
|---|---|---|
| `AI_ORCHESTRATION_ENABLED` | `False` | Master switch. When off, `maybe_create` returns `None` and the loop is unchanged. |
| `AI_PROVIDER` | `"mock"` | Registered provider name. |
| `AI_MODEL` | `""` | Provider-specific model id; empty = provider default. |

This repo (`ci/settings/settings.py`) sets `AI_ORCHESTRATION_ENABLED = True`
with the mock so the advisory flow runs end-to-end at zero cost.

## Tracing & cost

- **Per turn:** a one-liner to stdout (live in `journalctl -fu
  praktika-controller`) and a full JSON record appended to
  `TEMP_DIR/ai/turns.jsonl` — `{turn, ts, changed, observation, reasoning,
  decision, usage, error, ledger}`. Enough to replay the AI's exact view and
  decision after the fact.
- **Per run:** `UsageLedger` totals turns / input+output tokens / `cost_usd`,
  printed at `finalize()`.

## Non-goals (next phases)

- **Action dispatch.** `Turn.decision` is recorded but never applied. A later
  phase adds a dispatcher that maps decision items (`skip_job`, `cancel_job`,
  `cancel_run`, …) onto `WorkflowState` mutations, with an allowlist and
  opt-in-per-PR gating as the RFC requires.
- **Real SDK providers**, prompt engineering, tool specs.
- **S3 persistence** of traces (currently local + stdout only).
- **Pricing tables** beyond the `cost_usd` field plumbed through `Usage`.
