---
title: Orchestrator-owned workflow report (native path)
description: Make the single orchestrator the sole writer of the workflow report summary in the native (non-GitHub-Actions) path, replacing the concurrent per-job merge + destructive version=0 reset that corrupts the report on restarts.
doc_type: design
---

# Orchestrator-owned workflow report {#orchestrator-owned-report}

Status: **implemented.** Fixes the report-corruption class where a
killed-and-restarted orchestrator run left succeeded jobs wrongly marked
`NOT_FINALIZED`.

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

### Job side — every summary writer is now gated off on the native path

The end state: on the native path (`ORCHESTRATOR_OWNS_REPORT`) **no job-side code
writes the summary**. The orchestrator is the sole writer (increments 1–5). Each
former writer:

- `hook_html.push_pending_ci_report` → **no-op** on the native path. The
  orchestrator, not the Config job, creates the summary. GitHub Actions keeps the
  `version=0` create here (no orchestrator to do it).
- `state.create_initial_report` (called once by `_orchestrate_single` at
  fresh-run start, inside the startup-retry block) is the **sole** `version=0`
  create on the native path. A resume (`_orchestrate_resume`) never calls it, so
  a re-run keeps the finished run's rows. Because the single owner does the
  create at its own run boundary, there is **no create-once guard and no run_id
  in the report**: a fresh run resets with a new `start_time`, a resume doesn't
  touch it — which also closes the same-sha report-reuse hazard (a new
  orchestrator at the same PR/sha no longer inherits the previous run's stale
  `start_time`/`duration`).
- `hook_html.configure` → **no-op on the native path**. The orchestrator authors
  the cached/filtered SKIPPED rows itself (increment 4). On GitHub Actions (no
  orchestrator) it is unchanged.
- `hook_html.pre_run` → **no-op on the native path** (increment 5). It used to
  write the job's RUNNING row + clear the job's stale workflow-level messages on a
  re-run; the orchestrator now emits the RUNNING row for every in-flight job and
  clears a re-run job's stale messages in `publish_report`.
- `hook_html.post_run` → **does not write the summary on the native path**
  (increment 5). It still writes the per-job `result_<job>.json` (which, via
  `final.json`, carries the job's row + report messages the orchestrator reads).
  Its dependee-DROPPED cascade is now redundant with the orchestrator's own
  upstream-failure cascade, and its usage is owned by the orchestrator.
- `native_jobs._finish_workflow` → **does not write the summary on the native
  path** (increment 5): its `version=0`-less `update_final_report` upload is
  gated off. Its `NOT_FINALIZED`/DROPPED/OK stamping is a GitHub-Actions fallback
  for still-PENDING rows; on the native path the orchestrator already drives every
  job terminal, so there is nothing to stamp. Its merge-ready GH status and
  open-issues check still run (they are GH API calls, not summary writes), reading
  the summary the orchestrator maintains.

Combined with the orchestrator re-assert (increment 1), the wipe is structurally
impossible: no reset can erase a finished row, and even if one somehow did, the
orchestrator restores it next loop.

### Orchestrator side — re-assert each job's row (increment 1, shipped)

`WorkflowState.publish_report()`:

1. Lazily construct + dump an `_Environment` in the orchestrator process
   (`_ensure_report_env`, from the event: `WORKFLOW_NAME`, `PR_NUMBER`,
   `BRANCH=head_ref`, `SHA=head_sha`, `REPOSITORY`, …) so `_ResultS3` /
   `get_s3_prefix()` resolve. Done on first publish (after `_get_workflows`
   matching) so it can't change the env matching read.
2. For every job with a terminal `js.result` (parsed from `final.json`), build a
   row with the same `update_sub_result(..., drop_nested_results=True)` merge the
   runner uses, so rows render identically. Jobs the orchestrator knows finished
   are re-asserted from their `final.json`; **cached/filtered SKIPPED jobs (which
   never run and have no `final.json`) get their rows authored here too**
   (increment 4), in-flight jobs get a RUNNING row (increment 5), and workflow-
   level report messages are aggregated from each job's ext (increment 5).
   Originally this went through the version-CAS `update_workflow_results`; on the
   native path it now uses `update_workflow_results_inmem` (increment 5).
3. Called each `_drive_dag` loop (after `save_snapshot`), so the summary is
   corrected continuously — including while Finish Workflow is running.

Both steps are best-effort — report upkeep never crashes the run.

### Increment 2 (shipped) — remove the destructive reset

See "Job side" above: `push_pending_ci_report` no longer resets an existing
summary on the native path. This + the re-assert makes the wipe impossible.

### Increment 3 (shipped) — usage aggregation moves to the orchestrator

`update_workflow_results` gained `replace_usage` (SET instead of accumulate). On
the native path `publish_report` recomputes the **full** storage/compute/pipeline
aggregate from every finished job's `Result` (`js.result.ext` carries
`storage_usage`/`metrics`; compute is derived from runner + duration) and SETs it
each loop — idempotent, so re-publishing can't multiply the totals. The runner's
`post_run` stops contributing usage (`ORCHESTRATOR_OWNS_REPORT`) but still writes
rows / report messages and still sets `result.ext` for the orchestrator to read.
CIDB usage insertion stays on the runner and reads the totals off the summary.

### Increment 4 (shipped) — orchestrator authors the SKIPPED rows

`hook_html.configure` (run inside the Config job) used to write the cached/
filtered SKIPPED rows into the summary, making the runner a concurrent writer
racing the orchestrator's per-loop re-assert. Worse, its
`assert update_workflow_results(...) is None` guard tripped against the
orchestrator-seeded summary: the orchestrator seeds the summary `RUNNING` with
every job (incl. Config) `PENDING` (`config_job_running=False`), so the first
`update_sub_result` recomputed the status `RUNNING → PENDING` and the guard
raised. `configure` now **no-ops on the native path**; instead:

- `WorkflowState.apply_workflow_config` already marks `filtered_jobs` +
  `cache_success` jobs SKIPPED in the DAG. It now also stashes the cache-hit
  report link on the `JobState` (`skip_details_url`, persisted in the snapshot
  so it survives a resume).
- `publish_report` builds a SKIPPED `Result` row for each such job (cache hit →
  its reused-report link + "reused from cache"; filtered → its reason as info)
  and writes them in the same `update_workflow_results` call as the terminal
  rows. SKIPPED rows carry **no usage** (the job never ran) so they are built
  separately and kept out of the usage aggregation.

One fewer runner-side summary writer. See
`../docs/proposals/orchestrator-backlog.md` "sole summary writer".

### Increment 5 (shipped) — sole writer: retire every job-side write, drop the download + versioning

With increments 1–4 the orchestrator re-asserts the full summary each loop, but
the runner's `pre_run`/`post_run` and `_finish_workflow` were still *also* writing
it, so the orchestrator had to keep re-downloading the object every loop (a body
GET per `publish_report`) and use the version-CAS to merge safely. Increment 5
makes the orchestrator the **true sole writer** on the native path and removes
both costs:

- **`post_run`** no longer writes the summary (`ORCHESTRATOR_OWNS_REPORT`). Rows +
  report messages reach the orchestrator via each job's `final.json`
  (`js.result`); the dependee-DROPPED cascade is covered by the orchestrator's
  own upstream-failure cascade; usage was already orchestrator-owned (increment 3).
- **`pre_run`** no longer writes the summary. `publish_report` emits a RUNNING row
  for each in-flight (QUEUED/RUNNING) job, and folds in-flight job names into
  `clear_report_sources` so a re-run's stale messages are cleared while it re-runs.
- **`_finish_workflow`** no longer writes the summary (its `update_final_report`
  upload is gated off). Post Hooks ride in Finish Workflow's own returned result
  (nested) out via `final.json`; the `NOT_FINALIZED` stamping is a GHA fallback,
  moot on the native path. Merge-ready status / open-issues / CIDB still run on
  the Finish Workflow runner, reading the summary the orchestrator maintains.
- **Runner-side summary consumers** (`Runner.run`: the failed-job GH summary
  comment, final-job CIDB usage insert, automerge, Slack) now run **final-job-only**
  on the native path — the one point the summary is materialized locally (by
  `_finish_workflow`'s own download). Mid-run status lives on the orchestrator's
  check.
- **No download, no versioning.** `publish_report` holds the authoritative summary
  in memory (`_summary_result`, seeded by `create_initial_report` or loaded once on
  resume) and calls `update_workflow_results_inmem`, which applies the update
  in-memory (shared `_apply_workflow_update` helper) and — only when the content
  changed — **plain-overwrites** the summary object (`overwrite_result_to_s3`: same
  key, plain text, no gzip, no version metadata, no CAS). An unchanged loop does
  **zero S3 I/O** (this is what ended the per-loop GET flood). The version/CAS is
  kept only on the GitHub-Actions path, where concurrent per-job writers still race
  the summary (`update_workflow_results`, unchanged).

### Deferred (follow-up, not correctness)

- **Finish Workflow's own usage.** The orchestrator counts a job's usage only
  after its `final.json` lands; Finish Workflow reads the summary for the CIDB
  insert *before* it finishes, so its own (cheap, native) usage isn't counted.
  Minor accuracy nit.
- **Reset reset-jobs' rows on resume.** On a re-run, a reset job's row shows its
  previous terminal result until it completes again. Cosmetic/transient.

## Rollout / risk

All increments are additive on the native path and gated by
`ORCHESTRATOR_OWNS_REPORT` — flipping its default to False restores the old
per-job path (every job-side writer re-activates, the orchestrator falls back to
the versioned `update_workflow_results`). GitHub Actions and local runs are
unchanged (they keep the versioned per-job path). Validate on the live pipeline
after deploy: confirm a normal run's report renders identically, a failed run
shows failures + its summary comment, a re-run clears stale messages, and the
incident case (a killed+restarted run) no longer shows `NOT_FINALIZED`.

Increment 5 note: the single-writer / no-version model is only safe because the
native path has exactly one writer per run — the resume boot-lease
(`runs/<run_id>/resume.lock`) guarantees one orchestrator per run. If that
invariant ever broke, `overwrite_result_to_s3` has no CAS to catch a racing
writer; the last writer wins.

## Supersedes

- The Finish-Workflow "re-read `result_<job>.json`" fallback (Fix B) — dropped;
  the orchestrator writing truth makes it unnecessary.

## Touch points (shipped)

- `praktika/_environment.py` — `ORCHESTRATOR_OWNS_REPORT` field.
- `praktika/orchestrator/job_runner.py` — set it True in `_build_ci_environment`
  (False for local runs).
- `praktika/orchestrator/state.py` — `publish_report()` + `_ensure_report_env()`;
  `apply_workflow_config`/`JobState.skip` stash `skip_details_url`; `publish_report`
  authors the SKIPPED rows (increment 4), emits RUNNING rows for in-flight jobs and
  aggregates workflow-level report messages (increment 5); holds the authoritative
  summary in memory (`_summary_result`) and writes via `update_workflow_results_inmem`.
- `praktika/orchestrator/__init__.py` — call `publish_report()` each `_drive_dag`
  loop.
- `praktika/hook_html.py` — `push_pending_ci_report`, `configure`, `pre_run`, and
  the summary-write half of `post_run` all no-op on the native path; the shared
  skeleton builder (`_build_pending_summary` + `_write_summary_to_s3`) is driven by
  `create_initial_report` (orchestrator) and the GHA Config path.
- `praktika/native_jobs.py` — `_finish_workflow`'s `update_final_report` upload is
  gated off on the native path (increment 5).
- `praktika/runner.py` — `Runner.run`'s summary-consuming steps (GH summary comment,
  CIDB, automerge, Slack) run final-job-only on the native path (increment 5).
- `praktika/result.py` — `_apply_workflow_update` (shared apply/diff helper),
  `update_workflow_results_inmem` + `overwrite_result_to_s3` (single-writer, no
  download, no versioning); `update_workflow_results` (versioned CAS) kept for the
  GHA path.
- `praktika/orchestrator/state.py` — `create_initial_report()` is the sole
  native-path `version=0` writer (called from `_orchestrate_single`).
