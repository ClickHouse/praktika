---
title: Orchestrator-owned workflow report (native path)
description: Make the single orchestrator the sole writer of the workflow report summary in the native (non-GitHub-Actions) path, replacing the concurrent per-job merge + destructive version=0 reset that corrupts the report on restarts.
doc_type: design
---

# Orchestrator-owned workflow report {#orchestrator-owned-report}

Status: **design — to implement.** Fixes the report-corruption class in
[INCIDENT_2026-09-03_stale_report.md](./INCIDENT_2026-09-03_stale_report.md).

## Problem

The workflow report summary (`{report_prefix}/result_<workflow>.json`) is built
the **GitHub-Actions way**, where there is no central process, so every job races
to update a shared file:

- **Config Workflow** writes it with `push_pending_ci_report` at **`version=0`**
  — a *destructive, unconditional reset* (all rows PENDING).
- Each **job's runner** merges its own row via `post_run → update_workflow_results`
  (optimistic version-CAS).
- **Finish Workflow** reads the summary and stamps any still-PENDING row
  `NOT_FINALIZED` (it has no fallback in native mode — the GitHub job-status file
  doesn't exist off GitHub Actions).

On a restart (e.g. the orchestrator killed mid-run, redelivery → a 2nd attempt, a
duplicate Config), a **late `version=0` reset wipes rows for jobs that already
finished**, and nothing re-writes them (the orchestrator considers a job with a
`final.json` *done* and never re-dispatches it — completion and the summary row
are decoupled). Those jobs are then reported `NOT_FINALIZED` even though they
succeeded (their own `result_<job>.json` is OK the whole time).

## Insight

Praktika is **not** GitHub Actions: there is a **single orchestrator per run**
that already tracks every job and already parses each job's **full `Result`** from
`final.json` (`state.py` `sweep_completions` → `js.result`). So the orchestrator
can be the **sole writer** of the report summary:

- **No concurrency** → no version-CAS, no `version=0` destructive reset, no races.
- **Self-healing** → the orchestrator rewrites the whole summary each loop from
  its authoritative state, so a job it knows is terminal is *always* terminal in
  the report, regardless of restarts, resets, or whether the runner re-ran.

## Design

### Gate (native vs GitHub Actions vs local)

Add `ORCHESTRATOR_OWNS_REPORT: bool = False` to `_Environment`. The orchestrator's
`_build_ci_environment` (job_runner) sets it **True** for every dispatched job, so
it flows to jobs via `environment.json`. It is False for local runs and under
GitHub Actions — so those keep the existing per-job writers.

Not gated on `GITHUB_ACTIONS` alone, because a **local** run (`praktika run`) also
lacks `GITHUB_ACTIONS` yet has no orchestrator — it must keep writing its own
report.

### Job side — skip summary writes when `ORCHESTRATOR_OWNS_REPORT`

- `hook_html.push_pending_ci_report` → no-op (orchestrator writes the initial
  pending summary).
- `hook_html.configure` (cache-SKIPPED rows) → no-op (orchestrator includes
  cached jobs).
- `hook_html.pre_run` (clear stale messages) → no-op.
- `hook_html.post_run` → **still** `copy_result_to_s3(result)` (the per-job
  `result_<job>.json` stays authoritative) and upload files; **skip**
  `update_workflow_results`.
- `native_jobs._finish_workflow` → skip loading the summary and the per-job
  `NOT_FINALIZED` reconciliation loop and the final summary write; still run
  merge-ready status / open-issues / post-hooks (compute `failed_results` from the
  orchestrator-authored summary / per-job results instead).

### Orchestrator side — become the writer

Add `WorkflowState.publish_report(finalized=False)`:

1. Ensure a usable `_Environment` in the orchestrator process (construct from the
   event: `WORKFLOW_NAME`, `PR_NUMBER`, `BRANCH=head_ref`, `SHA=head_sha`,
   `REPOSITORY`, plus report ext fields; dump once) so `_ResultS3` /
   `get_s3_prefix()` resolve.
2. Build the summary `Result` tree (top = workflow; one sub-result per job):
   - **terminal** job → `Result.from_dict(js.result)`, merged with
     `drop_nested_results=True` (flatten failed leaves) to match today's render;
   - **cached/skipped** → `Result.create_new(name, SKIPPED, [cache_link], "reused from cache")`;
   - **pending/running** → `Result.create_new(name, PENDING|RUNNING)`.
3. Set the top-level ext (pr_title, report_url, commit_sha, branch, …) as
   `push_pending_ci_report` does today.
4. Write via `_ResultS3.copy_result_to_s3(summary)` (handles gzip + naming).
5. Call it each `_drive_dag` loop iteration (after `sweep_completions`) and once
   more at finalize — same cadence as `save_snapshot`.

Because there is one writer, no version metadata / precondition is needed; a plain
overwrite each loop is correct and idempotent.

### Deferred (follow-up, not correctness)

- **Usage KPIs** (`storage_usage` / `compute_usage` / `pipeline_utilization`)
  were accumulated inside `update_workflow_results`. Re-aggregate them in the
  orchestrator from `js.result.ext.metrics` — monitoring data, so it can land
  after the core. Note the gap in `log()` until then.
- **`report_messages`** (warning/error banners) are user-facing; fold them in
  from each `js.result` when present so they aren't lost.

## Rollout / risk

This removes the job-side writers on the native path, so if the orchestrator
writer regresses, native reports break. It is easily reverted (flip
`ORCHESTRATOR_OWNS_REPORT` default to False → the old per-job path resumes).
Validate on the live pipeline right after deploy: confirm a normal run's report
renders identically, then confirm the incident case (a killed+restarted run) no
longer shows `NOT_FINALIZED`.

## Supersedes

- The Finish-Workflow "re-read `result_<job>.json`" fallback (Fix B) — dropped;
  the orchestrator writing truth makes it unnecessary.

## Touch points

- `praktika/_environment.py` — `ORCHESTRATOR_OWNS_REPORT` field.
- `praktika/orchestrator/job_runner.py` — set it True in `_build_ci_environment`.
- `praktika/hook_html.py` — gate `push_pending_ci_report` / `configure` /
  `pre_run` / `post_run`.
- `praktika/native_jobs.py` — gate `_finish_workflow`'s summary logic.
- `praktika/orchestrator/state.py` (+ `__init__.py`) — `publish_report()` + calls
  in `_drive_dag` / finalize; construct the orchestrator `_Environment`.
