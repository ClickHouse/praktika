# Cortage jobs

Chain several jobs onto **one runner, one checkout, one `ci/tmp`**, run
back-to-back with **no cleanup between them**.

## Why

To make a workflow's **critical path** as fast as possible. Each normal job pays
fixed overhead — queue wait, runner boot, repo clone/checkout, artifact
download/upload. Putting the critical-path jobs in a cortage pays that **once**:
later members start instantly on the warm runner and read the previous member's
output straight from the shared `ci/tmp` instead of round-tripping through S3.

Native engine only (`praktika`). On `GH_ACTIONS` every job gets its own runner, so
colocation is meaningless — the validator rejects it there.

## Configuration

Chain jobs with `Job.Config.attach_to` (`praktika/job.py`):

```python
build = Job.Config(name="Build", runs_on=["big"], ...)
test  = Job.Config(name="Test",  runs_on=["big"], ...).attach_to(build)
pkg   = Job.Config(name="Pkg",   runs_on=["big"], ...).attach_to(test)
```

- A cortage is a **linked list**: each job attaches to one predecessor; the
  **head** (not attached to anything) runs first. Execution order = head → tail.
- Fields: `attached_to: str` and `promote_runs_on: bool` on `Job.Config`.
- **`runs_on`**: the chain runs on the head's `runs_on`, unless a member sets
  `attach_to(prev, promote_runs_on=True)` — then the **last** promoted member's
  `runs_on` is used for the whole chain (e.g. a cheap head feeding a GPU member).
  Other members' `runs_on` is ignored (validator warns).
- **`Settings.CORTAGE_MAX_STALL_SEC`** (default 15 min): max a member waits on an
  external dependency before the chain fails (see Stalling).
- **Cross-deps are allowed**: a member may `require`/`run_after` a job outside the
  cortage, and an outside job may require a member. Only a dependency **cycle**
  with the chain order is rejected (would deadlock).

## How it works

Members stay **first-class jobs** — own GitHub check, result, digest/cache,
heartbeat and `final.json` keys (`runs/<run_id>/<job>/…`). The orchestrator
treats them almost exactly like normal jobs; only dispatch and the runner loop
differ.

**Orchestrator** (`praktika/orchestrator/`)
- `build_job_dag` adds a sequential edge predecessor→member (ordering + cancel
  cascade). `Workflow.Config` has the chain helpers (`is_cortage_head`,
  `get_cortage_chain`, `cortage_effective_runs_on`, …).
- `JobState.kick` (`state.py`) dispatches **only the head**, to the *effective*
  `runs_on` queue. Members are marked QUEUED but **not** dispatched — the head's
  runner runs them; the existing completion/liveness sweeps then monitor each
  member via its own keys.
- `get_ready` treats the sequential edge as **ordering-only**: a member waits for
  its predecessor to be terminal but is cancelled only when a **data** dep
  (`requires`/`run_after`) fails — so a member not data-blocked still runs after a
  sibling's failure (`_cortage_pred`).

**Job runner** (`praktika/orchestrator/job_runner.py`)
- The controller dispatches the entry task as usual; `run_job` detects a cortage and
  `_run_cortage_chain` walks the members **from the dispatched entry through the
  tail, in one invocation** (so the checkout and `ci/tmp` persist, nothing is
  cleaned). The entry is the head on the normal path, or a later member when the
  head was cache/filter-skipped (see `kick`). Per member it:
  **resume-skips** members already finalized OK (infra redelivery resumes at the
  first incomplete one, restoring that member's `environment`); **skips**
  Config-Workflow cache/filter-`SKIPPED` members (their outputs are in S3);
  **block-skips** members whose failed data-dep is an earlier member; **stalls** on
  external deps; runs it (`_run_single`) and writes its `final.json`; hands its env
  off to the next member.

**Controller** (`bootstrap/src/praktika_controller/`)
- Unchanged except heartbeats: `Heartbeat` follows a **pointer file** the job
  runner writes in `ci/tmp` naming the current member's heartbeat key. The
  controller stays the sole heartbeat writer, so member heartbeats keep
  `instance_id` and the SQS attempt — orchestrator liveness/retry works unchanged.
  No clean between members and SQS visibility across the whole chain are automatic
  (one `orchestrate job` invocation).

**Artifacts** (`praktika/runner.py` `_pre_run`)
- An artifact from an earlier member is reused from the shared `ci/tmp` — the S3
  download is skipped when the producing member's file is present at its **configured
  `artifact.path`** (its provenance, not a same-basename file another step may have
  left in `INPUT_DIR`); it's copied to where a download would place it so the
  consumer finds it identically. If it's absent (the producer was a cache-hit skip,
  or an infra-rerun resumed on a fresh `ci/tmp`), it falls back to the normal S3
  download. Producers still upload to S3 for external consumers.

### Stalling (external deps)

The head is dispatched on its own deps only, so a later member may reach an
external dep that isn't done yet. The member **stalls**, polling
`runs/<run_id>/state.json`: all deps `success`/`skipped` (or a `failure` flagged
`non_blocking`, which `get_ready` treats as success-equivalent) → run; any blocking
`failure`/`cancelled` → the member is skipped (the orchestrator cancels it via
cascade); otherwise wait up to `CORTAGE_MAX_STALL_SEC`, then **publish a terminal
failure** for the member (the orchestrator hasn't cancelled it, so this avoids a
long liveness-timeout hang) and fail the chain for its dependents. The wait is
surfaced via the heartbeat phase.

### Failure & resume

- A member's job failure cancels only its **dependents**; independent later
  members still run (req: critical path keeps going where possible).
- On infra loss mid-chain the entry SQS message is redelivered; the fresh runner
  resume-skips **any already-finalized member** (success *or* failure — never
  re-running one the orchestrator already finalized), restores its environment, and
  resumes at the first incomplete one via the normal attempt-bump retry path. An
  already-finalized **blocking** failure is re-recorded so its dependents stay
  block-skipped, matching the orchestrator's cascade.
- If the **head is cache/filter-skipped**, the orchestrator dispatches the first
  member that must run as the chain entry (`kick` dispatches a member only when no
  earlier member was dispatched).
- A partial **re-run** resets the member (and failed downstream) to PENDING and
  deletes its `final.json` (`_reset_job`); the original driver is gone. `kick`
  dispatches the **earliest reset member** as a fresh entry (its chain predecessor
  wasn't part of the re-run), and the runner resumes the chain from it —
  resume-skipping the still-green members and re-running the reset ones.

## Validation (`praktika/validator.py` `validate_cortages`)

Errors: `attach_to` on `GH_ACTIONS`; attach to a missing job or self; two jobs
attaching to the same predecessor (fork); a cycle in the attach links; a
dependency cycle once sequential edges are added (deadlock). Warnings: a member
`runs_on` differing from the effective one; `promote_runs_on` on a non-cortage job.
