# Compute the PR ephemeral merge commit in the controller, not in the Config job

**Goal.** For a `pull_request` run with
`Settings.ENABLE_PR_EPHEMERAL_MERGE_COMMIT`, make **every** layer of the run —
the praktika runtime code, the orchestrator's DAG, the Config job, and every
job's working tree — derive from the *same* ephemeral merge commit
(`base tip` + `PR head`). Today only the Config job and downstream job trees see
the merge; the controller clone, the reinstalled praktika runtime, and the
orchestrator's DAG all still run at the PR **head** (non-merge). Move the merge
up to the controller — the one place that runs before praktika is installed from
the checkout and before any PR-influenced code executes — so the whole run is
anchored to one commit.

## Background: which layer runs at HEAD vs MERGE today

For a PR on the native (`PRAKTIKA`) engine:

| Layer | Runs at | Established by |
|---|---|---|
| `praktika-controller` (bootstrap) | baked AMI package | image builder (`image_builder.py`) |
| repo clone | **HEAD** | controller |
| `praktika` runtime (`orchestrator/`, `job_runner.py`, `native_jobs.py`, `runner.py`) | **HEAD** (`runtime_source` reinstall) | controller |
| orchestrator DAG (`workflow.jobs`, `requires`, `run_after`) | **HEAD** | orchestrator `_get_workflows()` (`mangle.py:41`) |
| ephemeral merge + digests + filtering + snapshot | **MERGE** | Config job (`native_jobs._prepare_repo_snapshot`) |
| job working tree + runner-side `_get_workflows()` | **MERGE** snapshot | job runner (`job_runner.py:305`) |

Three layers — the clone, the praktika runtime, and the DAG — are on the wrong
side of the merge. Consequences:

- **Silent dropped jobs.** If the base branch added a job since the PR branched,
  it exists in the merged tree but not in the orchestrator's head DAG, so it
  never runs. `apply_workflow_config` ignores unknown job names
  (`state.py:832-834`), so this is silent.
- **Unresolvable jobs.** If the base removed a job, the orchestrator schedules it
  (head) but the runner can't resolve it from the merged snapshot
  (`job_runner.py:311-314`) → the job errors.
- **Wrong ordering / routing.** Base-side changes to `requires` / `run_after` /
  `runs_on` / docker make the orchestrator order and route by head while the
  runner executes merge.
- **Runtime-code drift (the deepest one).** The orchestrator and job-runner *code
  itself* is `ci/praktika/*` reinstalled from the head checkout via
  `runtime_source` ("the pool always runs the current checkout",
  `runner_pool.py:303-310`, `orchestrator_pool.py:81-82`). A PR (or the base) that
  touches `ci/praktika/` drives the run with head's driver code while jobs run the
  merged tree. **This is why the fix cannot live in the orchestrator** — the
  orchestrator is itself reinstalled from the checkout, so merge logic placed
  there would be the head (PR-tamperable) version, executed *after* the wrong
  runtime is already installed.

## What already exists (do not reinvent)

- **The merge is deterministic.** `_prepare_repo_snapshot` fixes identity/dates so
  the merge SHA depends only on `(base_sha, head_sha, resulting tree)`
  (`native_jobs.py:510-548`). Any actor given the same base + head produces the
  identical merge commit — this is what lets the merge move without breaking the
  Config job's own reproduction of it.
- **Base-SHA pinning.** Live base tip is `git rev-parse origin/<base>`
  (`native_jobs.py:442`); optional sticky pinning via `STICKY_MERGE_BASE_HOURS` +
  `_resolve_sticky_base` (`native_jobs.py:452-461`, def ~`native_jobs.py:330`),
  with a `git merge-tree --write-tree` conflict check against live tip
  (`native_jobs.py:471-508`).
- **Snapshot build + publish.** History-free depth-1 archive (keeps `.git`,
  `native_jobs.py:553`), `tar | zstd`, uploaded to
  `repo-snapshots/v1/{PRs,REFs}/<sha256>.tar.zst` (`native_jobs.py:557-633`).
  IAM scopes the tiers so a `pr-*` pool cannot write `REFs/` and a trusted pool
  cannot read/write `PRs/` (`native_jobs.py:415-418`).
- **Snapshot key already flows through the run.** `_prepare_repo_snapshot` sets
  `workflow_config.repo_snapshot_key` / `snapshot_sha` (`native_jobs.py:632-633`);
  the orchestrator pins them once (`state.py:842-846`) and puts them into every
  downstream `job_task` (`state.py:1874-1885`). The controller consumes the task's
  `repo_snapshot_key` to restore the tree (not in this checkout — the restore
  lives in the upstream controller; cf. "clone is already on disk",
  `job_runner.py:113`).
- **`runtime_source` reinstall.** On every task the controller reinstalls
  `<source>` (usually `.`, the checkout) into an overlay of the prebaked base venv
  (`runner_pool.py:303-310`). This is the hook we retarget at the merged tree.
- **Bootstrap check exists pre-clone.** The controller opens a bootstrap check run
  *before* the (slow) clone (`orchestrator/__init__.py:380-388`) — a place to
  surface a merge conflict that fails before the orchestrator exists.
- **The controller is a separate, baked package.** `praktika-controller`
  (`ci/praktika/bootstrap/pyproject.toml`, `version.py:33-35`), launched as
  `/usr/local/bin/praktika-controller` (`image_builder.py:162`). **It is not
  vendored in this checkout** — changes land upstream, ship as a package, and
  require an AMI rebuild + pool redeploy.

## Chosen approach: merge in the controller

The controller is the correct trust anchor and the correct sequencing point:

- It runs **before** the `runtime_source` reinstall, so it can choose *which tree*
  praktika installs from.
- It is **baked/immutable**, so the merge logic can't be tampered by the PR.
- Git merge is **data-only** (no PR code execution), so merging untrusted content
  there is safe; the approval gate (`EXTERNAL_PR_GATE_FLOW.md`) already covers the
  head, and the base is trusted.

### Controller change (upstream `praktika-controller`)

On receiving an orchestrator task (the one that launches the orchestrator, i.e.
the run's first controller invocation), after cloning `head_sha`:

```
1. If event is pull_request AND ENABLE_PR_EPHEMERAL_MERGE_COMMIT:
     a. Fetch base:            git fetch --filter=tree:0 origin <base_ref>
     b. Resolve+pin base_sha:  live tip, or sticky pin (STICKY_MERGE_BASE_HOURS);
                               write the pin into run state so the whole run shares it.
     c. Conflict check vs live tip when pinned base != live tip
                               (git merge-tree --write-tree; fail fast — see step 5).
     d. Deterministic merge:   GIT_AUTHOR/COMMITTER fixed identity+dates,
                               git checkout <base_sha> && git merge --no-ff --no-edit <head_sha>
                               -> snapshot_sha = git rev-parse HEAD
     e. Publish snapshot:      pack history-free archive, upload to
                               repo-snapshots/v1/PRs/<sha256>.tar.zst
                               (tier by event type; controller is trusted/baked).
   Else (push / non-merge): snapshot_sha = head_sha (plain snapshot), as today.
2. runtime_source reinstall praktika FROM THE MERGED CHECKOUT (not head).
3. Launch the orchestrator. Its _get_workflows() now reads the merged workflow
   defs -> DAG matches execution. No orchestrator code change required in spirit.
4. Downstream runners: their controller restores the snapshot from the task's
   repo_snapshot_key (a valid history-free checkout, .git kept), reinstalls
   praktika from it, runs the job. No re-merge on runners — only the
   orchestrator's controller merges, once per run.
5. Merge conflict: complete the pre-clone bootstrap check as failed
   (orchestrator/__init__.py:380) with the conflicting files, and exit before
   installing praktika / launching the orchestrator.
```

`base_sha` / `snapshot_sha` / `repo_snapshot_key` are pinned into the run state so
the existing task-fan-out (`state.py:1874-1885`) distributes them unchanged. The
determinism (`native_jobs.py:510-548`) guarantees the controller's merge SHA
equals what the Config job would have produced from the same base.

### In-repo trim (`ci/praktika/native_jobs.py`)

Once the controller establishes the merged checkout and publishes the snapshot:

- `_prepare_repo_snapshot` **stops merging and stops building the snapshot** — the
  tree it runs on is already the merge, and `repo_snapshot_key` is already set by
  the controller. It should assert/verify `git rev-parse HEAD == snapshot_sha`
  (fail closed if not) and otherwise no-op the merge+publish.
- The base-pin + sticky logic (`_resolve_sticky_base`, `STICKY_MERGE_BASE_HOURS`)
  and the live-tip conflict check **move to the controller**. Keep the `Setting`
  names (they are already praktika `Settings`, `settings.py:143-152`).
- Everything after the merge in `_config_workflow` is **unchanged** and stays on
  the sacrificial runner: docker digests (`native_jobs.py:985`), filter hooks
  (`1003`), changed-file filtering (`1040`), cache lookup (`1076`), submodule cache
  (`1101`). This is the deliberate isolation boundary — heavy/untrusted work stays
  off the controller and orchestrator.

## Why not "do everything on the controller/orchestrator"

Do **not** fold the whole of `_config_workflow` into the controller/orchestrator.
That job is intentionally a sacrificial, horizontally-scaled, `PRs/`-tier-scoped
worker. Moving `pre_hooks` / `workflow_filter_hooks` / imported PR Python / docker
digests / `tar+zstd` / CIDB checks onto the controller or orchestrator would (a)
execute untrusted code with their privileges, (b) turn a retryable job failure
into a driver crash, and (c) serialize heavy IO onto scarce instances. The merge
is the *only* thing that must move up, because it (and only it) decides which tree
praktika and the DAG come from. Keep the Config job for the rest.

## Constraints / open decisions

1. **Upstream + baked release path.** The controller half lands in the upstream
   `praktika-controller` package and needs an AMI rebuild + redeploy. Heavier than
   the checkout-reinstalled `ci/praktika`, but appropriate for trust-critical
   infra and generic (the flag is already a praktika `Setting`).
2. **Snapshot ownership.** If the controller publishes the snapshot, the Config
   job's build+publish becomes redundant (kept only as a fail-closed verify).
   Confirm the controller honors the `PRs/` vs `REFs/` IAM tiering by event type
   (`native_jobs.py:415-418`).
3. **base_sha coordination.** The pin must be written once (orchestrator's
   controller) and shared; downstream runners inherit the merge purely by
   restoring the snapshot, so they need no base_sha.
4. **Conflict UX.** Surface merge conflicts through the pre-clone bootstrap check
   before the orchestrator exists (report ownership normally belongs to the
   orchestrator — this is the one pre-orchestrator failure path).
5. **Fork/untrusted PRs.** Installing praktika from the merged tree means running
   merged `ci/praktika` on the orchestrator/runner — same trust as running merged
   job code (already the model); the approval gate covers head, base is trusted.

## Status

Open — design agreed (merge belongs in the controller). Not started. Blocked on an
upstream `praktika-controller` change; the in-repo `native_jobs.py` trim is a
follow-up that depends on it.
