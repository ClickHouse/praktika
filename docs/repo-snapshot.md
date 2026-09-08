# Repo snapshot & PR merge-commit (native engine)

On the native engine (`praktika-controller` + orchestrator), the **Config
Workflow** can snapshot the repository once to S3 so every downstream job restores
that snapshot instead of cloning from GitHub. For `pull_request` runs the snapshot
can be the **ephemeral merge** of the PR head into the target-branch tip (GitHub
Actions style), so CI reflects the post-merge state rather than the PR in isolation.

Native engine only — GitHub Actions workflows are unaffected (they use
`actions/checkout`, whose merge-commit behavior is configured separately in
`praktika/yaml_generator.py`).

## Enabling

Two project settings (`ci/settings/settings.py`), both default `False`:

- `ENABLE_S3_REPO_SNAPSHOT` — build the snapshot in the Config Workflow and have
  downstream jobs restore it. Applies to `pull_request` and `push`.
- `ENABLE_PR_EPHEMERAL_MERGE_COMMIT` — for `pull_request`, snapshot the ephemeral
  merge of head into the target tip instead of the plain head. `push` is always a
  plain head snapshot (no merge). Requires `ENABLE_S3_REPO_SNAPSHOT` (the snapshot
  is the only way the merge reaches jobs); `Validator` enforces this.

## How it works

**Config Workflow** (`_prepare_repo_snapshot` in `praktika/native_jobs.py`), the
single setup job with a full clone:

1. Determine the commit to snapshot:
   - `pull_request` + merge enabled → `git checkout <base tip>; git merge --no-ff
     <head>` (base is the first parent, matching `refs/pull/N/merge`). A merge
     conflict is a first-class early `FAIL` (with conflicting paths) and no
     downstream jobs run. The merge is done in place, so the Config Workflow's own
     later steps (docker digests, changed-file filtering, cache lookup) see the
     merged tree.
   - otherwise → the head as-is.
2. Package a **minimal, history-free** snapshot: a depth-1 repo at the target
   commit (`.git` present for tooling, no ancestry), tar + zstd. Small even for
   large repositories.
3. Content-hash the archive and upload it **write-once** (`if_none_matched`).
4. Record `snapshot_sha` and `repo_snapshot_key` in `RunConfig`.

The orchestrator pins `snapshot_sha` / `repo_snapshot_key` once, when the Config
Workflow completes, and stamps them into every downstream `job_task` (not read from
the mutable per-job environment, so a later job cannot redirect them).

**Downstream jobs** (`restore_repo_snapshot` in
`bootstrap/src/praktika_controller/common.py`): the controller downloads the
archive by key, verifies its sha256 matches the key, unpacks the shallow repo, and
checks `HEAD == snapshot_sha` before running the job. No GitHub interaction — this
replaces N clones with N cheap S3 downloads.

## S3 layout and trust tiers

Snapshots are content-addressed and split by trust tier:

```
repo-snapshots/v1/PRs/<sha256>.tar.zst     # pull_request (fork-reachable)
repo-snapshots/v1/REFs/<sha256>.tar.zst    # push / trusted events
```

`pull_request` runs route to the `pr-*` runner pools (via the PR workflow's
`runs_on_label_prefix="pr-"`), `push` runs to the non-`pr-*` pools. See
`docs/s3-layout.md` for the full bucket map. These objects have a short S3
retention (a few days) — they are large and consumed within minutes.

## Security

- **Tamper-evident.** The S3 key *is* the archive's sha256, recorded by the
  (in-run-trusted) Config Workflow; every consumer re-hashes the download and
  rejects a mismatch. Uploads are write-once, so an existing object cannot be
  overwritten.
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
- Merging trusted base code with untrusted fork code and running it is the standard
  `pull_request` merge-commit risk (the same GitHub Actions carries). Secret
  exposure is unchanged.

## Components

| Concern | Location |
|---|---|
| Settings | `praktika/settings.py`; validation in `praktika/validator.py` |
| Config Workflow injection | `praktika/workflow.py` (`_enabled_workflow_config`) |
| Snapshot + optional PR merge | `praktika/native_jobs.py` (`_prepare_repo_snapshot`) |
| Run-level state | `praktika/runtime.py` (`RunConfig.snapshot_sha`, `repo_snapshot_key`) |
| Pin + relay to jobs | `praktika/orchestrator/state.py` |
| Per-job restore | `bootstrap/src/praktika_controller/{common,controller}.py` |
| IAM trust denies | `ci/infrastructure/projects.py` |
| S3 retention | `praktika/infrastructure/storage.py` |
