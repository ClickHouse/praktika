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
job_b.attach_to(job_a)          # or attach_to("job_a_name")
job_c.attach_to(job_b)          # chains: A <- B <- C, all on A's runner
```

- New field `attached_to: str = ""` (goes **before `ext`** — keep `ext` the last
  init field). Accepts a `Job.Config` or a name; stores the predecessor's name.
- **One attachment per job** (single `str`, structurally enforced) and **at most
  one job may attach to a given predecessor** — the chain stays a linked list,
  never a tree (validator asserts the second).
- **Cortage** = the maximal chain. *Head* = the job reached by following
  `attached_to` to `""`; all jobs sharing a head are one cortage. Execution order
  = head → tail.
- `runs_on` is taken from the **head**. Members keep their own `requires` /
  `provides` / `command` / `timeout`; they remain first-class jobs in the DAG and
  in reporting.

Naming chosen: `attach_to` reads as "attach this job onto <predecessor>", mirrors
the existing `set_*`/`*_after` builder vocabulary, and the stored field
`attached_to` names the relationship, not the feature.

## Validator (`validator.py`)

New non-fatal **warning** path (today everything is `evaluate_check`/`assert` →
`sys.exit(1)`; add a `print("WARNING: ...")` that lets validation pass):

- **WARNING** — a member's `runs_on` differs from the head's. Ignored; head wins.
  (req #2)

New **errors** (`evaluate_check` / assert):

- `attach_to` used on a `GH_ACTIONS` workflow. (req #1)
- `attached_to` names a job not in the same workflow.
- Two jobs attach to the same predecessor (would fork the chain).
- A cycle in `attached_to` links.
- **Cross-cortage dependency between overlapping cortages** (req #3/#4): collapse
  each cortage chain to a single super-node (merge every member's *external*
  `requires`/`run_after` onto it). **If the collapsed DAG has a cycle → error.**
  This is exactly "a job from one cortage requires a job from another cortage
  that runs in parallel": two cortages overlap unless one fully precedes the
  other, and a cross edge that breaks that ordering shows up as a cycle in the
  collapsed graph. It also catches the single-job interleave
  (`cortageA-member → X → cortageA-member`) that would deadlock a one-runner
  block.

A plain job (or an *external* cortage member) requiring an **intermediate**
cortage member is allowed — that is req #3's early-unblock case, handled below.

## Orchestrator (`orchestrator/state.py`, `orchestrator/__init__.py`)

- **Readiness via super-node collapse.** `build_job_dag`
  (`orchestrator/__init__.py:129`) groups members by head. A cortage dispatches
  only when the **union of all members' external deps** is satisfied — because
  the whole chain ships as one message and inner members can't wait on anything
  that isn't already done. (This is why cross-cortage cycles are illegal.)
- **Head-only dispatch.** `kick`/`_dispatch` (`state.py:670`, `:2602`) send a
  single ordinary task for the **head** to the head's runs_on queue. No batched
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
  **cortage's pool** (the *original head's* `runs_on`, not member K's). Already
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
- [ ] `job.py`: `attached_to` field (before `ext`) + `attach_to()` builder.
- [ ] `validator.py`: engine reject, name/chain/fork/cycle asserts,
      runs_on-mismatch WARNING, collapsed-DAG cross-cortage cycle check.
- [ ] `orchestrator/__init__.py`: cortage grouping + super-node collapse in
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
1. **Head's `runs_on` is law.** The batched message routes by the head only
   (`_queue_for_runs_on`, `state.py:268`); member `runs_on` is informational and
   only warned on.
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
