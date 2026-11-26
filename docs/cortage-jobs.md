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
- Fields: `attached_to: str`, `promote_runs_on: bool`, and `cortage_links` on
  `Job.Config` (`cortage_links` carries the extra links a multi-level `attach_to`
  declares for jobs other than the immediate predecessor — see below).
- **`runs_on`**: the chain runs on the head's `runs_on`, unless a member sets
  `attach_to(prev, promote_runs_on=True)` — then the **last** promoted member's
  `runs_on` is used for the whole chain (e.g. a cheap head feeding a GPU member).
  Other members' `runs_on` is ignored (validator warns).
- **`Settings.CORTAGE_MAX_STALL_SEC`** (default 15 min): max a member waits on an
  external dependency before the chain fails (see Stalling).
- **Cross-deps are allowed**: a member may `require`/`run_after` a job outside the
  cortage, and an outside job may require a member. Only a dependency **cycle**
  with the chain order is rejected (would deadlock).

### Chaining native jobs (Config / Docker)

The Praktika-native jobs — **Config Workflow** and the **Docker build** jobs — are
injected during mangle and aren't in the workflow's authored `jobs` list, so they
can't call `attach_to()` themselves. A single member configures the whole
predecessor chain, native jobs included, by passing an **ordered list** to
`attach_to` (`[head, …, immediate_pred]`; consecutive entries are chained):

```python
Job.Config(name="Member", runs_on=["arm"], command="…").attach_to(
    ["Config Workflow", "Dockers Build (arm)"]   # Config -> Docker -> Member
)
```

- Those extra (non-immediate) links ride on `cortage_links` and are applied to the
  **mangled** workflow by `mangle.py` once the native jobs exist.
- A native job is only a valid target when it is actually **injected** in this
  workflow (e.g. a Docker build needs `workflow.dockers`; Config needs the config
  job enabled). Attaching to one that isn't injected is rejected during mangle (it
  would otherwise crash resolving the chain's `runs_on`).
- **Finish Workflow cannot be a cortage member** (neither attached, nor attached
  to). It is the orchestrator-owned summarizer: it reads the workflow summary the
  orchestrator writes to S3 only once every job's row is finalized, so it must run
  **after** the orchestrator has observed every job complete. Colocating it on a
  runner runs it on the chain driver right after its predecessor — before the
  orchestrator records that predecessor — so it would mis-stamp jobs as
  `NOT_FINALIZED`. `mangle.py` rejects it.

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

An **`always_run`** member is never cancelled by the orchestrator, so it mirrors
that here: a failed/cancelled dep does **not** skip it (the dep counts as settled),
and it does **not** time out — it waits until every dep is terminal, then runs
regardless of upstream outcome. It is also never block-skipped by a failed in-chain
data dep.

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

## Limitations

- **GitHub credentials are not isolated per member.** A cortage shares one process
  home and one `~/.config/gh` across its members, with no cleanup between
  `_run_single` calls. Once any member runs with `enable_gh_auth=True`,
  `GHAuth.auth()` persists its token there and **every later member in the chain can
  use `gh` with that token**, even one that didn't set `enable_gh_auth`. This is an
  accepted limitation — per-member credential sandboxing is not done. Treat a
  cortage as a **single trust domain**: only chain members you'd grant the same
  GitHub credentials, and don't mix trust levels within one chain.

## Validation

`praktika/validator.py` `validate_cortages` runs on the **un-mangled** workflow, so
it can't see native jobs or applied `cortage_links`. It errors on: `attach_to` on
`GH_ACTIONS`; attach to self; two jobs attaching to the same predecessor (fork); a
cycle in the attach links; a dependency cycle once sequential edges are added
(deadlock). A Praktika-native job name (Config/Docker/Finish) is **accepted** as an
`attach_to` target here even though it isn't in `jobs` yet. Warnings: a member
`runs_on` differing from the effective one (skipped for native-involved chains,
whose effective `runs_on` is only known after mangle); `promote_runs_on` on a
non-cortage job.

`praktika/mangle.py` re-runs the **full** structural validation on the **mangled**
graph, once native jobs are injected and `cortage_links` applied — this is the
authoritative pass. The existence / self-attach / fork / attach-cycle /
dependency-deadlock checks are shared with the validator via
`_cortage_structural_errors`, so link-introduced forks and cycles through native
jobs are caught here even though they're invisible pre-mangle. mangle also: gives
the "attached to a native job that wasn't injected" case a clearer message than the
generic "unknown job", and rejects Finish Workflow as a cortage member.
