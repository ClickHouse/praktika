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
by `pull_request` workflows). The **trust tier is the outermost path segment** in
the artifact bucket, so the boundary can be prefix-scoped wholesale by IAM and
generalizes to other untrusted artifacts later:

```
untrusted/merge-snapshots/v1/<content-hash>.tar.zst    # pull_request (incl. fork PRs)
trusted/merge-snapshots/v1/<content-hash>.tar.zst      # push and other trusted events
```

An `untrusted/` snapshot is only ever consumed by `pull_request` runs; a trusted
run never reads it, even if the content would hash identically.

**No PR/branch scope in the key.** The object is content-addressed by its sha256,
which is globally unique and self-verifying, so a scope segment adds nothing for
correctness or security (a different merged tree yields a different key; an
identical tree yields identical bytes). Retention is handled by S3 lifecycle rules
on the tier prefix, not by scoping the key.

**Not under `ci_cache/`.** `ci_cache/` holds only small, safe cache *metadata*
(success records), which is why the runner role can freely read+write it. A
snapshot is a heavy *artifact* of potentially untrusted (fork) repo content, so it
must not share that prefix. Separate top-level trust prefixes keep artifacts apart
from metadata and let access, retention, and trust be governed independently.

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

The runner role grants whole-bucket read+write on the artifact bucket
(`ci/infrastructure/projects.py`: `allowed_s3_prefixes` is the bare bucket name),
which already covers snapshot `PutObject`/`HeadObject`/`GetObject`. On top of that
bucket-wide grant, this (OSS) project **enforces the trust boundary at the IAM
layer** with per-pool explicit Deny statements, because the trust tier is the
outermost path segment.

Runner pools are split by trust: fork / `pull_request` runs route to the `pr-*`
pools (via the PR workflow's `runs_on_label_prefix="pr-"`, applied to every job
including the injected Config Workflow — `praktika/mangle.py`); `push` runs use the
non-`pr-*` pools. Each pool's role (`ci/infrastructure/projects.py`, via a Deny in
`ext["iam_statements"]`) carries:

| Pool (trust) | `trusted/*` | `untrusted/*` |
|---|---|---|
| `pr-*` (untrusted) | read allowed, **write Deny** | read+write (own tier) |
| non-`pr-*` (trusted) | read+write (own tier) | **read+write Deny** |

Information-flow rule: reads may go down-trust but never up; writes never go up. So
a fork job cannot plant (or overwrite) a `trusted/` snapshot, and a trusted run
cannot ingest `untrusted/` (fork-produced) content. This is *defence in depth* on
top of the content-hash key + write-once upload + `HEAD == authorized merge_sha`
checks, which independently prevent a poisoned snapshot from being consumed.

Notes / caveats:

- **Whole bucket for private, scoped for OSS.** A private project with no untrusted
  actor can skip these Denys entirely (whole-bucket is fine). The split is applied
  per-project in `projects.py`, not via a framework flag — matching the framework's
  convention of expressing trust by routing to dedicated pools.
- The Deny resources use the bare bucket name and are namespaced to
  `praktika-artifacts-eu-north-1` by the deploy-time policy sweep (`cloud.py`).
- The tiers are empty until a workflow sets `enable_merge_commit`, so the Denys are
  inert for existing CI.
- The orchestrator role is not restricted here: it relays the snapshot *key*, never
  downloads snapshot *content*, so no untrusted bytes flow into it.

## Phasing

- **Phase 1: configurable merge-commit via snapshot.** Goals 1–4.
- **Phase 2: sticky merge base (implemented, simplified).** A new run reuses the
  PR's previously pinned target-branch commit — even if the branch has advanced —
  when it starts within `Settings.STICKY_MERGE_BASE_HOURS` of the PR's **previous
  run**, so the digest cache stays warm across rapid iterations instead of
  re-merging a moving base. `0` disables it (always the live tip). PR events only.
  - Per-PR pin record `{S3_ARTIFACT_BUCKET}/pr/<pr>/merge-base-pin.json` =
    `{base_sha, pinned_ts, base_branch}`. Per-PR, so a fork can only affect its own
    runs; the base itself is fixed for the run via the usual `RunConfig` pinning.
  - The pinned base is reused only if it is still an **ancestor of the live tip**
    (an older tip on the same branch), guarding against a stale/forged pin;
    otherwise it resets to the live tip.
  - **Deliberately dropped from the original idea:** the "AI-fix only" gate (the
    window applies to any run) and the first-pin anchor.
  - **Known trade-offs (accepted):** the window is measured from the *previous
    run*, so a steady stream of runs within the window keeps the base pinned
    indefinitely — there is no absolute staleness cap. And a green from a pinned
    (older) base does not necessarily reflect mergeability against the current
    tip; the merge-ready/automerge gate does **not** re-verify against the live
    base. Add a hard cap and/or a live-base re-verify before landing if either
    becomes a problem.

## Open questions

- Snapshot payload: shallow `.git` repo vs pure `git archive` tree — decide by
  auditing which downstream jobs actually need `.git`.
- Whether `needs_submodules` jobs restore submodules from the snapshot or continue
  to use the existing submodule cache keyed off the merged tree.
