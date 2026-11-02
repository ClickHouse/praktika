# Re-run of a push / schedule / dispatch check

**Status:** resolved for **partial** (per-job) re-run — implemented for every event
type (`pull_request` / `push` / `schedule` / `dispatch`).
**Full** ("re-run all") re-run was **removed entirely, for all event types**
(see [Full re-run](#full-re-run-removed) below).
**Area:** `infrastructure/native/lambda_gh_trigger.py` (`_handle_rerun`,
`_handle_partial_rerun`, `_build_rerun_workflow`), `orchestrator/__init__.py`
(`_orchestrate_resume`), `orchestrator/state.py` (`save_snapshot`).

## Problem (original)

Clicking "Re-run" on a check that belongs to a **push / schedule / dispatch run**
(e.g. a `master` push) did nothing. The webhook reached the Lambda and was
silently skipped; GitHub showed no error because the Lambda returns HTTP 200 and
only prints to CloudWatch.

Both re-run paths assumed every re-run targets a pull request:

```python
# _handle_partial_rerun
prs = check_obj.get("pull_requests", [])
pr_number = prs[0].get("number") if prs else None
if not pr_number or not rerun_sha:
    print(f"SKIP: partial rerun run={run_id} - missing PR number or head sha")
    return
```

A push / schedule / dispatch check never carries a `pull_requests` entry, so
`pr_number` was always `None` and every such re-run was dropped before any work
was scheduled. The stale-guard that followed was also PR-only (`_fetch_pr`), and
`_orchestrate_resume` compounded this by forcing `event["type"] = "pull_request"`
on every resume.

### Observed incident

Re-run requested on `Main / Test AMD (tsan)`, a failed job of a `master` push run:

- check `external_id`: `run_id=103695035456`, `job="Test AMD (tsan)"`
- `head_branch=master`, `head_sha=b50d34d...`, `pull_requests=[]`
- Lambda logged `SKIP: partial rerun ... - missing PR number or head sha`;
  nothing was enqueued.

## Why the code was PR-only

The re-run feature was designed and tested exclusively for pull requests: the PR
is the authoritative source of fork status (internal vs external routing) and of
the labels / title / draft the DAG is filtered on, and the stale-guard existed to
stop an old check from running current (possibly unapproved fork) code. Push /
schedule / dispatch runs have none of these concerns but fit none of the PR-shaped
plumbing, so they fell through the `pull_requests` guard.

## Implemented: partial (per-job) re-run for any event type

A per-job re-run (the per-job check's native "Re-run" button, or the "Rerun w/
fresh base" action) now works for `push` / `schedule` / `dispatch` runs, re-running
the job (+ its failed downstream) **in place** on the existing run, exactly as it
does for a PR.

Key design decision — **a non-PR re-run reuses the run's pinned snapshot, not the
current branch head.** A resume restores the exact commit the original run was
built from (`snapshot_sha` / `repo_snapshot_key` in the run's `state.json`), so:

- There is **no stale-head concern** and no need for a branch-head fetch (the
  proposal's `GET /repos/{repo}/branches/{branch}` guard is unnecessary): the
  re-run reproduces the original commit regardless of where the branch tip has
  moved.
- There is **no fork** to gate. The `ALLOWED_SENDERS` check (GitHub only shows the
  re-run button to write-access users) is the whole authorization. This is the
  agreed trust model for non-PR re-runs.

Changes:

- **`_handle_partial_rerun`** — the PR stale-head + fork-maintainer guard now runs
  **only when the check carries a `pull_requests` entry**. With no PR, that guard
  is skipped and the re-run proceeds (the running-vs-finished S3 handoff via
  `rerun-request/` + `finalized` is unchanged and already event-type-agnostic).
- **`save_snapshot`** (`state.py`) — persists `event_type` and `head_ref` in
  `runs/<run_id>/state.json`, so a resume can reconstruct the run's real context
  for any event type instead of guessing.
- **`_build_rerun_workflow`** (new helper) — one definition of the `type="rerun"`
  message. Carries `event_type` + `head_ref` off the snapshot; for a PR run it also
  carries the PR number and the authoritative `_fetch_pr` metadata. `fresh_base`
  is forced off for a non-PR run (there is no PR head to re-merge against a base).
- **`_orchestrate_resume`** — no longer forces `event["type"] = "pull_request"`.
  It re-shapes the event back to the run's original type (`event_type` from the
  message, falling back to the snapshot), so a push/schedule/dispatch resume is
  classified as itself (correct `FORK_NAME`, push-only steps, CIDB records) while
  a PR resume still reconstructs full PR context.

## Full re-run (removed) {#full-re-run-removed}

"Re-run all" / "Re-run failed checks" (a `check_suite.rerequested`, or a re-run of
the **top-level** workflow check — anything whose check carries no per-job
`external_id`) is **no longer supported, for any event type**. `_handle_rerun`
drops it with a `SKIP` log and `_handle_full_rerun` / `_handle_external_rerun` were
deleted.

Why it was removed rather than extended:

- **No UI entry point.** GitHub only surfaces the "Re-run all jobs" / "Re-run
  failed jobs" buttons for GitHub **Actions** workflow runs. praktika posts its
  checks through the checks API (no GHA workflow backs them), so the GitHub UI
  shows no "re-run all" button at all — the `check_suite.rerequested` path was
  effectively unreachable.
- **It only ever worked for PRs anyway.** The old full-rerun path minted a *fresh*
  run (re-clone, re-merge against the current base, new `run_id`). That shape
  cannot be reconstructed for a `schedule` / `dispatch` trigger from a check
  payload (no workflow name or trigger inputs), and a `check_suite` names no single
  run.
- **Partial re-run covers the real need.** Re-running the individual failed job(s)
  (per-job check button) works for every event type and re-drives in place.

If full re-run is ever wanted again, the natural implementation is a *resume that
resets every job* keyed off the top-level check id (`= run_id`), not a fresh run —
but it is deliberately out of scope here.
