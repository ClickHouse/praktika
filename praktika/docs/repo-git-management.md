# Repo & git management in a CI run (native engine)

This document describes **which git tree every layer of a native-engine run
executes on**, the three checkout modes, the settings that select them, and how
re-runs (native and the custom "fresh base" action) interact with each.

Native engine only (`praktika-controller` + orchestrator). GitHub Actions
workflows are unaffected — they check out via `actions/checkout`, configured
separately in `praktika/yaml_generator.py`.

Related: [`s3-layout.md`](s3-layout.md) (bucket map). **How** the praktika runtime
is obtained from that tree (baked venv, `runtime_source` reinstall, etc.) is a
separate concern — see [`installing-praktika.md`](installing-praktika.md); this doc
covers only *which tree* each layer runs on.

---

## The layers, and why they must agree

A single run touches the repo at several layers, each on a possibly-different
commit unless something forces agreement:

| Layer | Established by | Notes |
|---|---|---|
| `praktika-controller` | baked AMI package | immutable; never from the checkout |
| repo checkout (clone or snapshot restore) | controller, per task | the tree jobs and the orchestrator see on disk |
| `praktika` runtime (`orchestrator/`, `job_runner.py`, `native_jobs.py`, `runner.py`) | derived from the checkout (see [`installing-praktika.md`](installing-praktika.md)) | when a pool runs praktika from the checkout, the driver code is whatever tree the controller laid down |
| orchestrator DAG (`workflow.jobs`, `requires`, `run_after`, `runs_on`) | orchestrator `_get_workflows()` | read from the checkout the orchestrator runs in |
| each job's working tree | job runner | the tree the job's commands run against |

The **key invariant**: for a run to be coherent, the checkout, the praktika
runtime, the DAG, and every job tree should all derive from the **same commit**.
The controller is the trust anchor and the sequencing point that guarantees this:
it establishes the commit *before* the praktika runtime is taken from the checkout
and *before* any PR-influenced code runs, so the driver code and the executed code
are the same tree. (This is why the ephemeral merge lives in the baked controller
and not in a job — a job's praktika is the checkout-derived, PR-tamperable copy.)

---

## The three modes

Selected by two project settings in `ci/settings/settings.py`, both default
`False`:

| Setting | Effect |
|---|---|
| `ENABLE_S3_REPO_SNAPSHOT` | The controller builds the repo state once, publishes a snapshot to S3, and every job restores it instead of cloning GitHub. Applies to `pull_request` and `push`. |
| `ENABLE_PR_EPHEMERAL_MERGE_COMMIT` | For `pull_request`, snapshot the **ephemeral merge** of the PR head into the target-branch tip (GitHub-Actions style) instead of the plain head. No-op for `push`. Requires `ENABLE_S3_REPO_SNAPSHOT` (`Validator` enforces this). |

### Mode 1 — Plain head clone (snapshots off)

`ENABLE_S3_REPO_SNAPSHOT = False` (the default).

Every layer clones the **head** from GitHub:

- The orchestrator's controller clones `head_sha` (`clone_repo`) and launches the
  orchestrator from that clone — DAG at head.
- Each job's controller clones `head_sha` again (`clone_repo`) and runs the job.

No S3 snapshot; `N` jobs → `N` independent shallow clones. Simple, but every layer
hits GitHub and a `pull_request` run tests the PR **in isolation** (not merged with
its base).

### Mode 2 — Head S3 snapshot

`ENABLE_S3_REPO_SNAPSHOT = True`, and either a `push`, or a `pull_request` with
`ENABLE_PR_EPHEMERAL_MERGE_COMMIT = False`.

- The orchestrator's controller clones `head_sha`, packs a **history-free** depth-1
  snapshot of the head (`.git` kept, no ancestry), and uploads it write-once,
  content-addressed. The orchestrator runs from the head clone; DAG at head.
- Every job (including the Config Workflow) **restores** that snapshot from S3
  instead of cloning — no GitHub interaction, `N` cheap S3 downloads.

All layers still run at **head**; the only change from Mode 1 is *how* jobs get the
tree (S3 restore vs GitHub clone).

### Mode 3 — Ephemeral merge-commit S3 snapshot

`ENABLE_S3_REPO_SNAPSHOT = True` **and** `ENABLE_PR_EPHEMERAL_MERGE_COMMIT = True`,
on a `pull_request`.

The orchestrator's controller, after cloning `head_sha`:

1. Fetches the base branch and resolves the base tip (see **Base pinning** below).
2. Deterministically merges: `git checkout <base tip>; git merge --no-ff <head>`
   with fixed author/committer identity + dates, so the merge SHA depends only on
   `(base, head, resulting tree)` — reproducible by any actor from the same inputs.
   Base is the first parent, matching `refs/pull/N/merge`.
3. Publishes the history-free snapshot of the **merge commit** to S3.
4. Launches the orchestrator **from the merged checkout** → DAG built from the
   merged tree.
5. Every job (Config Workflow included) restores the **merge** snapshot.

Now **all** layers — checkout, runtime, DAG, and every job tree — derive from the
one ephemeral merge commit. CI reflects the post-merge state, and base-side changes
since the PR branched (a new job, a changed `requires`/`runs_on`, edited driver
code in `ci/praktika/`) are honored consistently instead of being silently dropped
or mis-ordered.

Implementation: the merge + publish live in
`bootstrap/src/praktika_controller/merge.py` (`prepare_repo_snapshot`), invoked
from `controller.py`. The in-repo `native_jobs._prepare_repo_snapshot` no longer
merges — it is a **fail-closed verify** that the restored `HEAD == snapshot_sha`.

#### Summary: what runs where

| Layer | Mode 1 (plain head) | Mode 2 (head snapshot) | Mode 3 (merge snapshot) |
|---|---|---|---|
| checkout | clone head | clone head (orch) / restore head (jobs) | merge (orch) / restore merge (jobs) |
| praktika runtime | head | head | **merge** |
| orchestrator DAG | head | head | **merge** |
| job trees | head (clone) | head (restore) | **merge** (restore) |
| S3 snapshot | none | `REFs/` or `PRs/` | `PRs/` |

---

## Settings variations

- **`ENABLE_S3_REPO_SNAPSHOT`** — off ⇒ Mode 1; on ⇒ Mode 2 (or Mode 3 with the
  merge flag). Push runs are always a plain-head snapshot.
- **`ENABLE_PR_EPHEMERAL_MERGE_COMMIT`** — turns a `pull_request` from Mode 2 into
  Mode 3. Requires `ENABLE_S3_REPO_SNAPSHOT`; the snapshot is the only channel the
  merge reaches jobs through.
- **`STICKY_MERGE_BASE_HOURS`** (default `0`) — Mode 3 only. When `> 0`, a new run
  for a PR reuses the **same** target-branch commit its previous run merged against,
  as long as the new run starts within this many hours of the previous one. This
  keeps the digest/cache warm across rapid PR iterations instead of re-merging a
  moving base every push. `0` always merges against the live tip. The pin is
  per-PR, stored in S3, and untrusted (validated as a commit id and required to be
  an ancestor of the live tip before reuse). When the pinned base differs from the
  live tip, the controller additionally runs a non-destructive `git merge-tree`
  check against the **current** tip, so a green never hides a real conflict with
  live `main`.

### Base pinning & determinism (Mode 3)

- Live base tip = `git rev-parse origin/<base_ref>`.
- Optional sticky pin per PR (`STICKY_MERGE_BASE_HOURS`).
- Fixed identity/dates make the merge commit reproducible: the controller's merge
  SHA equals what any other actor would produce from the same base + head, which is
  what lets the merge move between layers (and across re-runs) without drift.

### Conflict handling (Mode 3)

If the PR head does not merge cleanly into the (pinned or live) base tip:

- The controller **fails fast, before the orchestrator exists**: it finalizes the
  pre-clone "CI" check as `failure` with title *Merge conflict* and the conflicting
  file list, and returns — no snapshot is published, no runtime is installed, no
  jobs run.
- The SQS message is **deleted** (a conflict is deterministic; retrying would just
  re-conflict). By contrast, an *infra* error during the merge (e.g. a failed
  `git fetch`) raises and is retried on a fresh instance.
- Because this is a pre-orchestrator failure, there is no full praktika HTML report
  — the conflict is surfaced on the check itself.

---

## Commit metadata (Mode 3)

The merge snapshot every job restores is history-free, so the PR head commit object
is unreachable downstream and `git log HEAD` on a job would return the synthetic
`Merge … into …` commit (authored by `praktika@localhost`). To keep reports, CIDB,
and Slack notifications anchored to the **PR branch head**, the controller captures
the head commit subject + author *before* the merge rewrites `HEAD`
(`common.head_commit_info`) and threads them through `event → job_task →
_Environment` (`COMMIT_MESSAGE` / `COMMIT_AUTHORS`), where the job prefers them over
`git log HEAD`. Modes 1 and 2 already run at head, so nothing special is needed.

---

## Re-runs

Three re-run paths, each with different repo semantics. The first two are GitHub's
native buttons; the third is a custom praktika action.

### Re-run all jobs — fresh run, re-merged against current base

GitHub `check_suite.rerequested` (the "Re-run all jobs" button) has no per-job
marker → `_handle_full_rerun` mints a **brand-new run** (new `run_id`, new S3
prefix) as an ordinary `pull_request` trigger. It therefore goes through the normal
fresh-run path and, in Mode 3, **re-merges against the current base tip** at re-run
time. Use this when the base moved and you want a full re-plan (new base jobs,
recomputed digests/filters, current `main`).

### Re-run failed jobs — resume, reusing the original snapshot

GitHub `check_run.rerequested` on a per-job check carries `{run_id, job}` in its
`external_id` → `_handle_partial_rerun` **resumes the existing run in place**:

- **Finished run** → a fresh orchestrator boots, reloads the persisted DAG from
  `state.json`, resets the target job(s) + failed downstream, and re-drives. The
  controller **restores the original published snapshot** (same commit, same base),
  so the re-run executes the exact tree the run was built on.
- **In-progress run** → the request is dropped for the live orchestrator's
  `sweep_rerun` to pick up; it re-runs on the already-pinned snapshot.

Everything (DAG shape, docker digests, changed-file filters, cache) is reused from
the original run — only the requested jobs re-execute.

### Re-run with fresh base — resume, re-merged against current base (custom action)

A per-job check-run **action button, "Rerun w/ fresh base"** (identifier
`rerun_fresh_base`), added to every completed job check. It routes through the same
`_handle_partial_rerun` (so it inherits the same sender/maintainer and stale-head
guards) with `fresh_base=True`, but behaves differently by run state:

- **Finished run** → resume with `fresh_base=True`. The controller re-clones the
  head and **re-runs the ephemeral merge against the CURRENT base tip**, publishes a
  new snapshot, and the resumed orchestrator overrides the snapshot pinned from
  `state.json` (`override_repo_snapshot`) so re-dispatched jobs restore the fresh
  tree.
- **In-progress run** → `fresh_base` is **ignored**; the live orchestrator keeps the
  existing snapshot (an in-progress run never re-merges).
- **Conflict** → if the head no longer merges into the moved base, the run **fails**
  (its top-level check is marked *Merge conflict*). There is **no fallback** to the
  old snapshot — there is no point re-running against a base the PR can't merge into.

**Caveat.** A fresh-base *partial* re-run refreshes only the **executed tree**. It
resumes the persisted DAG and the original Config Workflow's decisions (job set,
ordering, docker digests, changed-file filters), so a base change to *job structure*
or *Dockerfiles* is **not** picked up — use **"Re-run all jobs"** for a full
re-plan. Fresh-base partial re-run is for "a base fix / flaky-infra fix landed on
`main`, re-run just my expensive failed job against it."

#### Re-run summary

| Trigger | Scope | Base | Re-plans DAG/digests? |
|---|---|---|---|
| Re-run all jobs | whole workflow (new run) | current tip (re-merged) | yes |
| Re-run failed jobs | failed job(s) + downstream (resume) | original (snapshot reused) | no |
| Rerun w/ fresh base | selected job(s) + downstream (resume, finished only) | current tip (re-merged) | no (tree only) |

---

## Snapshot packaging (Modes 2 & 3)

The snapshot is a **minimal, history-free** archive: a depth-1 repo at the target
commit (`.git` kept for tooling, no ancestry), `tar` + `zstd`. Small even for large
repositories. The controller content-hashes the archive and uploads it
**write-once** (`if_none_matched`); the S3 key *is* the sha256. On restore, the job
controller downloads by key, re-hashes and rejects a mismatch, unpacks, and checks
`HEAD == snapshot_sha` before running (`restore_repo_snapshot` in
`bootstrap/src/praktika_controller/common.py`). No GitHub interaction — this
replaces `N` clones with `N` cheap S3 downloads.

## S3 layout and trust tiers

Snapshots are content-addressed and split by trust tier:

```
repo-snapshots/v1/PRs/<sha256>.tar.zst     # pull_request (fork-reachable)
repo-snapshots/v1/REFs/<sha256>.tar.zst    # push / trusted events
```

`pull_request` runs route to the `pr-*` runner pools (via the PR workflow's
`runs_on_label_prefix="pr-"`), `push` runs to the non-`pr-*` pools. These objects
have a short S3 retention (a few days — large, consumed within minutes). See
[`s3-layout.md`](s3-layout.md) for the full bucket map.

## Security

- **Tamper-evident.** The key is the archive sha256, recorded by the (in-run-
  trusted) controller; every consumer re-hashes the download and rejects a
  mismatch. Uploads are write-once, so an existing object cannot be overwritten.
- **Authorized-commit guard.** Jobs never resolve a live ref or fetch from GitHub
  for checkout; the restore requires `HEAD == snapshot_sha` (the snapshot-mode
  equivalent of `clone_repo`'s stale-head guard).
- **Trust segregation, IAM-enforced.** The runner role is bucket-wide, so
  `ci/infrastructure/projects.py` carves the tiers back out per pool with explicit
  Deny statements (`repo-snapshots/v1/{PRs,REFs}/*`):

  | Pool | `REFs/*` (trusted) | `PRs/*` (untrusted) |
  |---|---|---|
  | `pr-*` (untrusted) | read allowed, **write Deny** | read+write (own tier) |
  | non-`pr-*` (trusted) | read+write (own tier) | **read+write Deny** |

  Rule: reads may flow down-trust, never up; writes never up. A fork job cannot
  plant a `REFs/` snapshot, and a trusted run cannot ingest a `PRs/` one. A private
  project with no untrusted actor can omit these Denys (whole-bucket is fine).
- **Merge safety.** Git merge is data-only (no PR code runs during the merge), so
  merging untrusted fork content in the baked controller is safe. Merging trusted
  base code with untrusted fork code and running it is the standard `pull_request`
  merge-commit risk (the same GitHub Actions carries); the head is covered by the
  external-PR approval gate and the base is trusted. Secret exposure is unchanged.

---

## Component map

| Concern | Location |
|---|---|
| Settings + validation | `praktika/settings.py`, `praktika/validator.py` |
| Controller merge + publish (Mode 3) | `bootstrap/src/praktika_controller/merge.py` |
| Controller clone / restore / resume wiring | `bootstrap/src/praktika_controller/controller.py`, `common.py` |
| In-job fail-closed verify | `praktika/native_jobs.py` (`_prepare_repo_snapshot`) |
| Snapshot pin + relay to jobs | `praktika/orchestrator/state.py` (`seed_repo_snapshot`, `override_repo_snapshot`, `_dispatch`) |
| Resume driver | `praktika/orchestrator/__init__.py` (`_orchestrate_resume`) |
| Re-run routing + fresh-base action | `praktika/infrastructure/native/lambda_gh_trigger.py` |
| Fresh-base action button | `praktika/orchestrator/state.py` (`RERUN_FRESH_BASE_ACTION`) |
| Commit metadata threading | `common.head_commit_info` → `state._dispatch` → `job_runner._build_ci_environment` |
| Run-level state | `praktika/runtime.py` (`RunConfig.snapshot_sha`, `repo_snapshot_key`, `base_sha`) |
| IAM trust denies | `ci/infrastructure/projects.py` |
