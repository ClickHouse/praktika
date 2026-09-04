# Native merge-commit CI (design)

Status: **design + phase 1 in progress**. Owner: CI team.

## Problem

Praktika's **native engine** (the self-hosted `praktika-controller` + orchestrator)
always runs pull-request CI against the PR **head** commit. GitHub Actions-style
CI instead runs against an **ephemeral merge commit** — the PR head merged into the
current target branch — so that CI reflects the state of the code *after* the PR
lands, not the PR in isolation.

The GitHub Actions engine already does this (it defers to `actions/checkout`'s
default `refs/pull/N/merge`, toggled off by the repo var `DISABLE_CI_MERGE_COMMIT=1`
— see `praktika/yaml_generator.py`). The native engine does not: `enable_merge_commit`
is declared on `Workflow.Config` but is stubbed with `assert False, "NOT implemented"`
in the Config Workflow (`praktika/native_jobs.py`).

This document specifies merge-commit support for the **native engine only**.

## Goals

1. **Configurable** — head vs merge-commit, per workflow, defaulting to head (no
   change for existing workflows).
2. **Immutable per run** — the merge commit is computed **once** and every job in
   the run uses that exact commit, even if the target branch advances mid-run.
3. **Snapshot-based distribution** — the merge is computed by the first job and
   packaged as a **minimal, history-free snapshot** on S3; every downstream job
   restores that snapshot and performs **no GitHub interaction at all**. This is
   more reliable than N independent clones on a large pipeline, faster, and keeps
   the transferred data minimal.
4. **Secure** — trusted and untrusted (fork/OSS PR) snapshots are strictly
   segregated and never mixed; the existing "run only the authorized commit"
   invariant is preserved.

Deferred to a later phase (**not** in scope here): sticky-base pinning across runs
for the AI-fix iteration loop (see "Phase 2" below).

## Non-goals

- The GitHub Actions engine (already handled).
- Changing head-mode behavior in any way.

## Background: how the native engine checks out today

- `clone_repo(repo, head_sha, pr_number, token, work_dir, branch)` in
  `bootstrap/src/praktika_controller/common.py` is the **single** checkout
  primitive. It is called at orchestrator bootstrap
  (`controller.py`, orchestrator start) and again for **every per-job runner**.
- It pins the checkout to the **authorized `head_sha`** via a reachable-SHA fetch,
  never the live `refs/pull/N/head`. This is a security invariant: a fork commit
  pushed *after* a run is authorized must not execute under that run's
  authorization. A TOCTOU guard (`actual_sha == head_sha`) enforces it.
- The run's shared immutable state (`RunConfig`, `praktika/runtime.py`) is fixed
  once by the Config Workflow and relayed to every job unchanged via
  `env.WORKFLOW_CONFIG` and the per-job `job_task`. `RunConfig.sha` is fixed this
  way today; the merge fields ride the same relay.
- The **submodule cache** (`native_jobs.py::_prepare_submodule_cache` +
  `runner.py::_restore_submodule_cache`) is the existing precedent for a
  content-addressed, write-once S3 archive restored per-job. The merge snapshot is
  the same shape.

## Design

### Overview

```
Config Workflow (first job, has full history)
  ├─ resolve base_sha  = tip of BASE_BRANCH        (pinned for the run)
  ├─ git checkout base_sha; git merge --no-ff head_sha   (GitHub first-parent semantics)
  │     └─ conflict  → early, clean FAIL result (no downstream jobs run)
  ├─ package a MINIMAL, history-free snapshot of the merged tree
  ├─ content-hash the archive  →  snapshot_key (trust-scoped)
  ├─ upload write-once to S3 (if_none_matched)
  └─ record base_sha / merge_sha / merge_snapshot_key in RunConfig  →  relayed to all jobs

Every downstream job
  └─ restore snapshot from S3 by key, verify content hash == key
       →  working tree already at merge_sha, zero GitHub interaction
```

### 1. Configuration (Goal 1)

Reuse the existing `Workflow.Config.enable_merge_commit: bool = False`. No new
knob. `False` = head (unchanged default); `True` = merge-commit mode.

### 2. Computing the merge once (Goal 2)

Performed in the Config Workflow, the one setup job that already has full history
(it unshallows HEAD and fetches `BASE_BRANCH` for author/commit-message features):

- `base_sha` = current tip of `BASE_BRANCH`, captured once. This is the value that
  Phase 2 will optionally pin across runs.
- Replicate GitHub merge semantics: `git checkout base_sha` then
  `git merge --no-ff head_sha`, so `base_sha` is the **first** parent (matches
  `refs/pull/N/merge`).
- A **merge conflict** is a first-class, early CI failure: the Config Workflow
  returns a `FAIL` result with the conflicting paths in `info`, and no downstream
  jobs run. This surfaces un-mergeable PRs clearly instead of as a confusing
  downstream break.
- `base_sha`, `merge_sha`, and `merge_snapshot_key` are written to `RunConfig`
  (new fields) and relayed to every job via the existing mechanism. This is what
  makes the merge immutable for the run: the target branch may advance, but the
  run keeps using the commit computed here.

### 3. Snapshot distribution (Goal 3)

- **Minimal / no history.** History is only needed to compute PR authors and
  pre-merge commit messages, and that work happens *in the Config Workflow*, which
  keeps its full clone. The snapshot handed to downstream jobs is history-free: a
  depth-1 shallow repo at `merge_sha` (`.git` present so tooling like
  `git describe` / diff-vs-base works, but no ancestry), or a pure `git archive`
  tree if we confirm no downstream job needs `.git`. This keeps the payload small
  even for very large repositories.
- **Upload.** Content-addressed archive, uploaded **write-once**
  (`S3.put(if_none_matched=True)`) — the submodule-cache pattern.
- **Restore.** Every downstream job downloads the archive by key and unpacks it.
  No `git fetch`, no GitHub token use for checkout. On a large pipeline this
  replaces N clones-with-merge by N cheap S3 downloads.
- **Fallback.** If the object is unexpectedly missing, the job reconstructs the
  merge from the two pinned parents (both reachable by sha) — never from a live
  ref — so correctness and the security model do not depend on S3 availability.

### 4. Security (Goal 4)

**Trusted vs untrusted segregation.** Mirrors the existing cache trust model
(`praktika/cache.py`: a `pull_request` `CacheRecord` is untrusted and only reused
by `pull_request` workflows). Snapshots live under a **dedicated top-level prefix**
in the artifact bucket (`<bucket>/merge-snapshots/...`, a sibling of `PRs/`,
`runs/`, `ci_cache/`), keyed in a trust- and PR-scoped namespace:

```
merge-snapshots/v1/pull_request/pr-<N>/<content-hash>     # untrusted (incl. fork PRs)
merge-snapshots/v1/trusted/<branch>/<content-hash>        # trusted
```

**Not under `ci_cache/`.** `ci_cache/` holds only small, safe cache *metadata*
(success records), which is why the runner role can freely read+write it. A
snapshot is a heavy *artifact* of potentially untrusted (fork) repo content, so it
must not share that prefix. A dedicated prefix keeps artifacts separable from
metadata and lets access, retention, and trust be governed independently (e.g. a
future prefix-scoped IAM policy, or per-tier lifecycle rules).

A `pull_request`-tier snapshot is **only** ever consumed by `pull_request` runs; a
trusted run never reads a PR-tier object. Fork and non-fork PRs, and trusted runs,
cannot cross-contaminate.

**Tamper-evidence / anti-poisoning.** The key **is** the content hash of the
archive, computed and recorded in `RunConfig` by the (in-run-trusted) Config
Workflow. Every downstream job re-hashes the downloaded bytes and rejects any
mismatch. Combined with write-once upload, an attacker cannot substitute content
for a key, and cannot overwrite an existing snapshot.

**Authorized-commit invariant preserved.** `base_sha` and `head_sha` are both
pinned at Config-Workflow authorization time and recorded in `RunConfig`; the
merge is built from exactly those two commits. Downstream jobs never resolve a live
ref and never fetch from GitHub for checkout. The TOCTOU guard adapts from
"checked-out sha == head_sha" to "restored HEAD == recorded merge_sha".

**Threat model note.** Merging trusted base code with untrusted fork code and
running it is the standard `pull_request` merge-commit risk (the same one GitHub
Actions carries). Secret exposure is unchanged from today: merge-commit mode does
not grant PR jobs any new access.

## Affected components

| Concern | Location | Change |
|---|---|---|
| Config surface | `praktika/workflow.py` (`enable_merge_commit`) | none (already exists) |
| Run-level state | `praktika/runtime.py` (`RunConfig`) | add `base_sha`, `merge_sha`, `merge_snapshot_key` (+ `setdefault` back-compat) |
| Merge + snapshot | `praktika/native_jobs.py` (Config Workflow) | replace `assert False` with merge, conflict-fail, snapshot build + upload |
| Relay to jobs | `praktika/orchestrator/state.py` | stamp merge fields into each `job_task`; mark full-clone jobs |
| Per-job restore | `bootstrap/src/praktika_controller/common.py`, `controller.py` | snapshot-download branch + hash verify + adapted TOCTOU guard |
| Submodules | `praktika/native_jobs.py` | compute `submodule_cache_hash` from the merged tree in merge mode |

## Permissions

No IAM change is required for phase 1. The Config Workflow runs on the runner
pool (`CI_CONFIG_RUNS_ON`) whose role grants whole-bucket read+write on the
artifact bucket (`ci/infrastructure/projects.py`: `allowed_s3_prefixes` is the
bare bucket name, no key prefix), and the `praktika-controller` runs on the same
instance under the same role. So the Config Workflow's `PutObject`/`HeadObject`
and the controller's `GetObject` on `merge-snapshots/...` are already covered by
the bucket-wide grant.

Snapshots deliberately use a top-level `merge-snapshots/` prefix rather than
`ci_cache/` — see "Not under `ci_cache/`" above. This is what makes the two
caveats below actionable:

- **If a future hardening pass narrows `allowed_s3_prefixes`** to specific key
  prefixes, `merge-snapshots` must be added explicitly, or snapshot
  put/head/get will start failing. Because it is its own prefix (not commingled
  with `ci_cache` metadata), it can be granted with different actions per tier.
- **The trusted/untrusted tier segregation is logical, not IAM-enforced today.**
  The runner role is bucket-wide, so IAM alone would not stop a fork-PR job writing
  a `trusted/`-tier key or reading another PR's object. What prevents a poisoned
  snapshot from being *consumed* is the content-hash key + write-once upload + the
  `HEAD == authorized merge_sha` check, and the fact that the orchestrator (not the
  job) chooses the key. The dedicated prefix makes enforcing the boundary at the
  IAM layer (prefix-scoped roles, or a bucket per tier) a clean follow-up.

## Phasing

- **Phase 1 (this change): configurable merge-commit via snapshot.** Goals 1–4.
- **Phase 2 (deferred): sticky base for the AI-fix loop.** Hold `base_sha` fixed
  across successive runs of the same PR when (a) the new push is an AI fix and (b)
  the run starts within N hours of the first pin, to maximize digest-cache hits
  during rapid AI iteration. Requires: a per-PR pin record in S3, an N-hour window
  anchored to the **first** pin (hard-capped total staleness), an AI-fix signal
  reusing the existing `ai_orchestrator` marker rather than a new gate, and
  merge-ready/automerge **re-verification against the live base** before landing
  (a pinned-base green is stale by construction and must be surfaced).

## Open questions

- Snapshot payload: shallow `.git` repo vs pure `git archive` tree — decide by
  auditing which downstream jobs actually need `.git`.
- Whether `needs_submodules` jobs restore submodules from the snapshot or continue
  to use the existing submodule cache keyed off the merged tree.
