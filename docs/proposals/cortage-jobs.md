# Cortage jobs (chain several jobs onto one runner)

**Goal.** Let a workflow pin a sequence of jobs to **one runner, one checkout,
one `ci/tmp`**, run back-to-back with **no cleanup between them**. A "cortage" is
a linked chain of jobs: the head runs first and defines `runs_on`; each attached
job runs after its predecessor on the same machine, reusing its working tree.

**Why.** Today the model is strictly *1 job ⇄ 1 SQS message ⇄ 1 fresh checkout*
(`state.py:_dispatch`, `controller.py:handle_task` → `clean_work_root` before
every task). Re-clone + runner churn is the expensive part. A cortage amortizes
checkout/boot across a group and lets a downstream member read the previous
member's build output straight out of `ci/tmp` instead of a mandatory S3
round-trip.

**Dispatch shape — head-only.** The orchestrator sends **one ordinary SQS
message, for the head job only**. The task already carries `ci_config`
(`state.py:2634`), so the runner enumerates the rest of the chain itself (follow
`attached_to`) and runs members in order **without any further SQS messages**.
Heartbeat, per-member finalization, and infra-failure reruns keep working as
today (details under *Orchestrator* / *Controller*).

**Native engine only.** On `GH_ACTIONS` each job maps to an independent
GitHub-hosted runner — colocation is meaningless. Support is **dropped in the
GH_ACTIONS engine**: the validator rejects `attach_to` on a `GH_ACTIONS`
workflow, and `yaml_generator.py` never sees it.

## API — `Job.Config.attach_to(job)`

A new builder on `Job.Config` (deep-copy + return self, like `set_requires`
etc., `job.py:211`):

```python
job_b.attach_to(job_a)                         # or attach_to("job_a_name")
job_c.attach_to(job_b, promote_runs_on=True)    # chain A<-B<-C, runs on C's runner
```

- New field `attached_to: str = ""` on `Job.Config` (appended after `post_hooks`;
  `Job.Config` has no `ext` field). Accepts a `Job.Config` or a name; stores the
  predecessor's name.
- Optional `promote_runs_on: bool = False` arg → new field
  `promote_runs_on: bool = False`. When set, this member promotes **its own
  `runs_on`** as the cortage's runner.
- **One attachment per job** (single `str`, structurally enforced) and **at most
  one job may attach to a given predecessor** — the chain stays a linked list,
  never a tree (validator asserts the second).
- **Cortage** = the maximal chain. *Head* = the job reached by following
  `attached_to` to `""`; all jobs sharing a head are one cortage. Execution order
  = head → tail.
- **Which `runs_on` the cortage uses:** the `runs_on` of the **last member in
  chain order with `promote_runs_on=True`**; if no member promotes, the **head's**
  `runs_on`. This lets a lightweight head run a chain on a heavier runner a later
  member needs. The effective `runs_on` is what routes the SQS message and picks
  the pool (`_queue_for_runs_on`); all other members' `runs_on` are informational.
- Members keep their own `requires` / `provides` / `command` / `timeout`; they
  remain first-class jobs in the DAG and in reporting.

Naming chosen: `attach_to` reads as "attach this job onto <predecessor>", mirrors
the existing `set_*`/`*_after` builder vocabulary, and the stored field
`attached_to` names the relationship, not the feature.

## Validator (`validator.py`)

New non-fatal **warning** path (today everything is `evaluate_check`/`assert` →
`sys.exit(1)`; add a `print("WARNING: ...")` that lets validation pass):

- **WARNING** — a member's `runs_on` differs from the cortage's effective
  `runs_on` (the last-promoted member's, else the head's). Ignored; the effective
  runner wins. (req #2)

New **errors** (`evaluate_check` / assert):

- `attach_to` used on a `GH_ACTIONS` workflow. (req #1)
- `attached_to` names a job not in the same workflow.
- Two jobs attach to the same predecessor (would fork the chain).
- A cycle in `attached_to` links.
- **Only a genuine deadlock cycle is an error** (revised req #4 — cross-deps to
  jobs *outside* the cortage are now **supported**, see below). Build the full job
  DAG and add each cortage's **sequential edges** (`head → m2 → … → mN`, since the
  runner executes members strictly in order). Run normal cycle detection. A cycle
  means a member waits — directly or transitively — on a job that can only run
  *after* it on the same runner → **error**. Everything else is allowed.
  - Why sequential edges, not super-node collapse: with stall-and-wait semantics
    (below), member `m_j` may be required by external `X` which is required by a
    *later* member `m_k` (`j < k`) — that is **not** a deadlock (`m_j` finalizes,
    `X` runs, `m_k` stalls then runs), and the old collapse check wrongly rejected
    it. The deadlock is only when the back-edge lands on the same-or-later member
    (`m_j → X → m_k` with `k ≤ j`), which the sequential edges turn into a cycle.

A plain job (or an *external* cortage member) requiring an **intermediate**
cortage member is allowed — that is req #3's early-unblock case, handled below.

## Orchestrator (`orchestrator/state.py`, `orchestrator/__init__.py`)

- **Readiness on the head's own deps.** `build_job_dag`
  (`orchestrator/__init__.py:129`) groups members by head and adds the sequential
  chain edges. The cortage dispatches as soon as the **head's own** `requires` are
  satisfied — it does **not** wait for later members' external deps. Those are
  enforced at runtime by the runner stalling (below). This reduces config friction:
  the user needn't arrange deps so the whole chain is ready up front.
- **Head-only dispatch.** `kick`/`_dispatch` (`state.py:670`, `:2602`) send a
  single ordinary task for the **head** to the **effective cortage runs_on** queue
  (the last-promoted member's `runs_on`, else the head's — see API). No batched
  payload, no intra-cortage dep map in the message — the runner reads the chain
  from `ci_config`. (No SQS 256 KB concern.)
- **Mark the tail as head-handled, keep watching.** When the head is kicked, the
  scheduler marks members 2..N as dispatched-via-head (a QUEUED-equivalent) so it
  does **not** also dispatch them, while **still watching each member's
  final-state key**. Members remain independent `JobState`s in `WorkflowState`.
- **Deterministic per-member keys (linchpin).** The runner writes each member's
  final-state (and heartbeat) to the *same* S3 key the orchestrator watches, so
  those keys must be a pure function of (run context + `job_name`) computed
  identically on both sides — or the head task carries the key prefix/template
  the runner fills per member. Get this wrong and finalizations land where nobody
  is looking.
- **Per-member final-state, early unblock.** As each member finishes, the runner
  uploads that member's final-state mid-cortage; `get_ready()` (`state.py:2768`)
  marks it COMPLETE and promotes its **external** dependents immediately — no
  waiting for the chain tail. (req #3)
- **Per-member heartbeat (as today).** While a member runs it heartbeats to its
  own key, exactly like a standalone job; each member transition gets the normal
  QUEUED→RUNNING grace for the next member's first heartbeat. This is how the
  orchestrator knows *which* member was the live frontier when a runner died.
- **Rerun on infra failure resumes at the failed member.** A stale heartbeat on
  member K — with members `<K` already finalized — reruns **member K**,
  re-dispatched as the head of the remaining chain `K..N` and routed to the
  **cortage's effective pool** (the last-promoted member's `runs_on`, else the
  original head's — not member K's own, unless K is itself the promoted one). Already
  finalized members are **not** re-run: their `provides` are in S3, so the fresh
  runner restores the snapshot and member K's `_pre_run` pulls them from S3 as
  normal, then continues `K+1..N` locally. Restart from the original head happens
  only when the head itself never finalized.
  - **Resumability constraint:** this works only for data that finalized members
    pushed to S3 as **declared `provides`**. Incidental `ci/tmp` side-effects from
    earlier members die with the runner — a chain that relies on undeclared local
    state cannot resume at member K and must restart from the head. Anything that
    must cross members *and* survive an infra rerun has to be a declared artifact.
- **Failure (req #6, "same rules apply").** A member's *real job* failure is not
  an infra rerun: the runner handles cascade/skip locally, and the failed
  member's final-state cancels its dependents (intra- and cross-cortage) via the
  normal CANCELLED cascade; later members that don't depend on it still run.
- **External-dep stall (new, revised req #4).** When the runner reaches a member
  whose external `requires` aren't done yet, the member **stalls** instead of
  failing. This holds the runner idle — accepted, in exchange for zero config
  friction.
  - **Signal: poll `runs/<run_id>/state.json`.** The orchestrator already writes
    this authoritative all-jobs snapshot every ~10 s (`save_snapshot`,
    `state.py:1878`), and the runner already receives its key as
    `task["state_s3_key"]` (the controller already reads it in `_run_is_finalized`,
    `controller.py:336`). The stalling member polls it and reads
    `jobs[<dep>]["status"]`: `SUCCESS` → proceed; `FAILURE`/`CANCELLED`/`SKIPPED`/
    `ERROR` → fail the cortage; `PENDING`/`QUEUED`/`RUNNING` → keep waiting.
    - *Not* each dep's `final.json`: cancelled/dropped/liveness-killed deps never
      write one (`job_runner.py` writes `final.json` only for jobs that ran to
      completion), so they'd look pending forever. `state.json` covers every
      terminal reason. The runner resolves a member's external dep job names from
      `ci_config` (same artifact→provider logic as `_pre_run`); the snapshot is
      keyed by plain job name, no normalization needed.
  - **Warn in the workflow report.** While stalled, surface a non-fatal warning
    (`ext.warnings`) naming **what it is stalling on** (the blocking dep job name)
    and **for how long** (seconds stalled). The runner emits it via heartbeat
    phase/`ext`; the orchestrator renders it. User-visible signal a cortage is
    parked.
  - **Fail the cortage if a stalled-on dep fails.** When the snapshot shows the dep
    in a terminal non-success state, the cortage **fails**: the stalling member and
    the rest of the chain are marked failed, the runner aborts and is released.
    Already-finalized earlier members keep their results.
  - **Bounded by `Settings.CORTAGE_MAX_STALL_SEC` (15 min default).** If a member
    stalls longer than this on a single dep, the member (and chain) is failed and
    the runner released — a safety net so a wedged dep can't hold a runner forever.
    The stall also ends early on dep success/failure or whole-run cancel/finalize.

## Controller (`controller.py`, `common.py`)

- Receive an ordinary head task. If the head job has attached members (read from
  `ci_config`), enter cortage mode. Run `_prepare_runner_for_task` →
  `clean_work_root` (`controller.py:1147`, `common.py:548`) **once** before the
  head, restore/clone into one `clone_dir`. Each member heartbeats under its own
  key while it runs (as a standalone job does).
- Enumerate the chain (head → tail via `attached_to`) and loop members in order:
  synthesize each member's task locally (shared `snapshot_sha`/`repo_snapshot_key`
  + per-member `job_name` and the deterministic final-state key), write
  `ci/tmp/task.json`, run `praktika orchestrate job <task> --ci`, upload that
  member's final-state — **no `clean_work_root`, no re-clone, no `ci/tmp` wipe
  between members** (req #4/#5: no clean at all within a cortage). Normal cleanup
  resumes when the next unrelated task arrives.
- Skip a member whose intra-cortage predecessor dep failed; report it CANCELLED
  so the orchestrator cascades correctly. (req #6)

## Artifacts (`runner.py`)

- Members share `ci/tmp` + checkout, so a producer's `provides` are already on
  local disk for a later member — `_pre_run` (`runner.py:349`) can short-circuit
  the S3 download when the provider is an earlier member of the same cortage and
  the path exists. S3 upload still happens for **external** consumers; S3 remains
  the fallback. (req #5, "former": local-passing is the default for cortages,
  since nothing is cleaned.)
- **It is the job's responsibility to work with a local artifact instead of
  downloading it.** When `_pre_run` short-circuits the S3 download, the framework
  does **not** re-materialize the artifact into the path a standalone run would
  download it to — the file is wherever the earlier member left it in `ci/tmp`. A
  later member that `requires` it must be written to **find it locally** and not
  assume a fresh download happened. And the inverse case must be handled too: if
  the artifact is **not** in `ci/tmp` (e.g. the producer was a cache-hit skip, or
  this is an infra-rerun resume on a fresh runner), the requiring member must know
  to fall back to the normal S3 download. In short, a cortage member cannot assume
  "my inputs are always freshly downloaded" nor "my inputs are always on local
  disk" — it must cope with both.

## Implementation checklist
- [x] `job.py`: `attached_to` + `promote_runs_on` fields (after `post_hooks`) +
      `attach_to(job, promote_runs_on=False)` builder.
- [x] `settings.py`: `CORTAGE_MAX_STALL_SEC` (15 min) + export entry.
- [x] `validator.py`: engine reject, missing/self/fork/attach-cycle asserts,
      sequential-edge deadlock-cycle assert, runs_on-mismatch + stray-promote
      WARNINGs (`validate_cortages`).
- [ ] `orchestrator/__init__.py`: cortage grouping + sequential chain edges in
      `build_job_dag` / execution-plan print.
- [ ] `orchestrator/state.py`: cortage-aware readiness, head-only `_dispatch`,
      mark-tail-head-handled + keep watching keys, per-member final-state unblock,
      resume-at-failed-member rerun, failure cascade.
- [ ] `controller.py` / `common.py`: head task → walk `attached_to`, clean once
      before head, no clean between members, per-member heartbeat/finalize,
      CANCELLED-on-dep-fail skip.
- [ ] `runner.py`: local-provides short-circuit in `_pre_run`.
- [ ] `yaml_generator.py`: unaffected (native-only); confirm no leakage.

## Hard constraints
1. **The effective `runs_on` is law.** The message routes by the last-promoted
   member's `runs_on` (else the head's) via `_queue_for_runs_on`
   (`state.py:268`); every other member's `runs_on` is informational and only
   warned on. The effective runner must be resolved **once, statically**, before
   dispatch — it determines the pool for the whole chain and every rerun of it.
2. **Collapsed-DAG acyclicity is the safety property.** Get the super-node
   collapse right or a cortage can deadlock waiting on a dep that can only run
   after it. The validator cycle check is the guard — do not ship without it.
3. **No clean inside a cortage, full clean at its boundaries.** The runner must
   still `clean_work_root` before the head and before the next unrelated task;
   only the *inter-member* cleanup is skipped. A leaked `ci/tmp` across cortages
   is a correctness bug.
4. **Members stay first-class.** Each keeps its own result, GitHub check, digest,
   and final-state key — that is what makes early external unblock (req #3) and
   per-member failure (req #6) work.

## Open questions / notes
- **Timeouts — per member.** Each member keeps its own `timeout` for its
  `orchestrate job` run; the runner lease is bounded by their sum. No
  cortage-wide budget.
- **Digest / cache — per member, skip on hit.** Members cache individually as
  normal; a cache **hit on any member (including a middle one) is skipped**, not
  re-run. Consequence for local artifact passing: a skipped producer's output is
  **not** in the shared `ci/tmp`, so a later member that `requires` it must fall
  back to the S3 download (the cached result's artifacts). The `_pre_run`
  local-provides short-circuit therefore applies only when the earlier member
  **actually ran** this cortage; otherwise it pulls from S3 as today. Keep that
  condition explicit — a skipped-member assumption that files are on local disk
  is a correctness bug.
- **Testing: controller + orchestrator change** — validate the group-task loop
  and early-unblock on real AWS (silk / Box / eu-north-1) before commit; leave
  uncommitted until confirmed.
