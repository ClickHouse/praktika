# AI-assist flaky-failure detection + auto-retry

**Goal.** When a job fails for a transient/flaky reason (timeout, network blip,
transient cloud 5xx, a race, an "expected X got Y" that passes on a sibling
shard), let the orchestrator AI advisor **re-run just that job automatically**,
instead of leaving the run red for a human to click "re-run". This is the
praktika-native equivalent of what PeerDB does with the Claude Code GitHub
Action in `.github/workflows/auto-retry-flaky-tests.yml` — but it runs *inside*
the live orchestrator rather than as a second `workflow_run`-triggered workflow.

## What PeerDB does (the thing we're matching)

Separate GitHub Action, fired `on: workflow_run [completed]` after CI fails:

1. Guards: same-repo only (`head_repository.full_name == github.repository`),
   `run_attempt <= 5`.
2. Runs `anthropics/claude-code-action` with a forced JSON schema → `{is_flaky,
   confidence, summary}`; the model reads `gh run view <id> --log-failed`.
3. If `is_flaky && confidence >= 0.7` → `gh run rerun <id> --failed` + a PR
   comment ("🔄 Flaky Test Detected…").

Limitations we improve on: it needs a **second workflow + second model
invocation** after the whole run finishes; it re-runs the *entire* failed set
(not the single flaky job); it is **same-repo only** (forks get nothing); and it
hard-codes a `confidence >= 0.7` gate and a `run_attempt <= 5` backstop.

## Chosen approach — a new advisor decision, on the existing machinery

We already have every piece; this is a small, additive extension, not new
infrastructure:

- The orchestrator AI advisor (`AIAssist`) already fires **`on_job_failure`** the
  moment a job goes terminal-red, and already investigates with `fetch_log` /
  `grep_repo` / `read_file` before deciding (`orchestrator/ai/DESIGN.md`,
  "Investigation tools").
- It already **dispatches actions** from the model's `decision` list —
  `cancel_run` and `cancel_and_patch` today (`orchestrator/ai/__init__.py:653`
  `_dispatch`, cases at `:674`/`:677`). The provider's decision is authoritative;
  there is **no separate enable flag** per action (DESIGN "Action dispatch").
- The in-run **re-run machinery already exists**: `state.apply_rerun(job_names)`
  resets failed jobs (and their failed/cancelled downstream) to `PENDING` and the
  DAG driver re-drives them on the next loop iteration
  (`orchestrator/state.py:2034`). It is already used by the GitHub-initiated
  re-run path (`_drive_dag` → `state.sweep_rerun`). It enforces a hard per-job
  cap, `MAX_RERUNS_PER_JOB` (default 5, `state.py:137`).

So "flaky auto-retry" = **one new decision type, `retry_jobs`**, wired to
`apply_rerun`. No new workflow, no second model call, no `workflow_run` listener.

### The three edits

1. **Decision schema / prompt** (`orchestrator/ai/anthropic.py`, `_SYSTEM` at
   `:49`). Add a fourth action alongside `continue` / `cancel_run` /
   `cancel_and_patch`:

   > `"retry_jobs"`: the failure is transient/flaky, not a code defect — re-run
   > the failed job(s) in place. Choose this only when the evidence points to a
   > transient cause (timeout, network error, transient cloud 5xx, a race, or an
   > intermittent assertion that passed on a sibling shard / a prior commit) and
   > the current code is not at fault. If a failure might be a real bug, or the
   > fix is a clear deterministic edit, do **not** use `retry_jobs` — use
   > `continue` or `cancel_and_patch` respectively.

   The `{type, detail}` shape is unchanged; `detail` carries the flaky rationale
   (this becomes the PeerDB `summary`). No new fields required — by default the
   action retries the failed jobs named in `observation.changed` (the jobs that
   triggered this consult). Optionally accept a `jobs: [name,…]` subset for
   consolidated mode, intersected with the observed failures in code (never
   trusted to name an arbitrary job).

2. **Dispatch** (`orchestrator/ai/__init__.py` `_dispatch`, `:670`). Add a third
   case, same "first actionable decision wins" rule:

   ```python
   if dtype == "retry_jobs":
       self._retry_jobs(state, turn)   # derive names from the turn's observation
       return
   ```

   `_retry_jobs` resolves the target names to the failed jobs in this consult,
   calls `reset_ok, failed = state.apply_rerun(names)`, logs
   `[AI ] retry_jobs: re-running <names> (flaky): <detail>`, and records it on the
   session like the other actions. `apply_rerun` already:
   - skips jobs over `MAX_RERUNS_PER_JOB` (the backstop — our equivalent of
     PeerDB's `run_attempt <= 5`, but per-job, not per-run),
   - clears the stale `final.json` before resetting so the re-run actually runs,
   - bumps `rerun_count`, which is surfaced in the job check output
     (`state.py:170`, "🔁 re-run #N").

   The loop then re-kicks the PENDING jobs — identical to a human-clicked re-run,
   a proven path.

3. **Observability** — free. The decision renders automatically in the existing
   **AI Assist** GitHub check table (`provider._decision_cell` /
   `_render_check_table`, `provider.py:157`/`:358`) as a `retry_jobs` row with the
   rationale, and lands in the per-turn trace (`turns.jsonl`). This replaces
   PeerDB's separate PR comment — we already have a running surface for it.

## Data flow

```
job fails → orchestrator loop → AIAssist.on_workflow_update
   └─ provider.on_job_failure(observation)      # investigates via fetch_log/grep_repo/read_file
        └─ decision: [{type:"retry_jobs", detail:"transient GCS 500 on delete; sibling shards green"}]
   └─ AIAssist._dispatch
        └─ _retry_jobs → state.apply_rerun([failed job])   # cap: MAX_RERUNS_PER_JOB
              └─ reset job (+failed downstream) to PENDING, clear final.json, rerun_count++
   └─ next loop iteration: DAG driver re-kicks the PENDING job
   AI Assist check shows a `retry_jobs` row; trace records the turn
```

## Why this is better than the PeerDB shape here

| | PeerDB action | This proposal |
|---|---|---|
| When | after the whole run finishes (2nd workflow) | the instant the job fails, mid-run |
| Scope | re-runs the entire failed set | re-runs only the flaky job(s); green jobs untouched |
| Model calls | a second, separate Claude invocation | reuses the advisor turn already happening on failure |
| Fork PRs | excluded (`head_repository` guard) | **works** — in-run reset touches S3/state only, no branch push (unlike `cancel_and_patch`, which stays same-repo-only) |
| Backstop | `run_attempt <= 5` (per run) | `MAX_RERUNS_PER_JOB` (per job, already enforced) |
| Surface | new PR comment | existing AI Assist check + trace |

## Guardrails / design decisions

- **No confidence-threshold knob, no per-action enable gate.** The provider's
  decision is authoritative (DESIGN "Action dispatch"; this matches the project
  rule that the AI's decision stands on its own). "How confident before
  retrying" is **prompt guidance**, not a `Settings` constant or a
  `Workflow.Config` field. Steering lives in the existing
  `Workflow.AIAssist.Config.extra_prompt` / `extra_prompt_file` — e.g. a repo can
  add "only retry the `integration_*` shards; never retry `unit`".
- **Runaway-proof.** `MAX_RERUNS_PER_JOB` caps per-job retries regardless of how
  often the model asks; `apply_rerun` also refuses to reset a job that is not yet
  terminal, so a lingering request can't double-dispatch (`state.py:2054-2068`).
- **First actionable decision wins** — `retry_jobs` can't coexist with
  `cancel_and_patch` in one turn; dispatch returns after the first
  (`_dispatch`, `:670`).
- **Advisory failure is safe.** If `apply_rerun` resets nothing (cap hit, reset
  failed), the run simply finishes red as it does today — the advisor never breaks
  core orchestration (DESIGN, "The advisor is advisory and best-effort").

## Scope / non-goals

- **No new workflow or `workflow_run` listener** — the whole point is to avoid
  PeerDB's second-workflow shape.
- **In-run retry only.** We re-drive within the live orchestrator; we do not
  re-trigger a *finished* run's jobs from the AI path (that is the existing
  human/lambda re-run path).
- **No PR comment** — the AI Assist check is the surface. A PR comment could be a
  later add-on if maintainers want it in the conversation timeline.

## Rollout

1. Land the `retry_jobs` action (3 edits above) in the published runtime wheel —
   the orchestrator runs the *published* runtime, not the PR's praktika code, so
   the action must ship in a runtime before any workflow relies on it (DESIGN
   "Configuration").
2. Enable per workflow via the existing `Workflow.Config.ai_assist` (already how
   `cancel_run` / `cancel_and_patch` ship); add flaky-retry steering to
   `extra_prompt_file` if the repo wants to narrow which jobs are retry-eligible.
