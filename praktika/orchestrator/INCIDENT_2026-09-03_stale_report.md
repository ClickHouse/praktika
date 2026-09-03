---
title: Incident 2026-09-03 — stale report, "3 jobs failed to produce Result"
description: Autoscaler scaled a default orchestrator in mid-run; the redelivered attempt-2 restart left the shared PR/sha report tree inconsistent, so Finish Workflow marked three succeeded jobs NOT_FINALIZED. Includes the mid-run-kill log-loss fix.
doc_type: incident
---

# Incident 2026-09-03 — stale report, three jobs "failed to produce Result" {#incident-2026-09-03}

## Summary

PR#144, sha `fb61310`. The workflow report showed **Style Check** and both
**Parametrized** jobs as `ERROR — "Job failed to produce Result due to a script
error or CI runner issue"` even though all three had **succeeded** (their
per-job `result_<job>.json`, `runs/<run_id>/<job>/final.json`, and `state.json`
all record `success`). It was a **false negative in the aggregate report**, not a
real job failure — and it predated the manual Code Review re-run (which worked).

Root trigger: **the pool autoscaler scaled the default orchestrator ASG in
(`desired 1→0`) while attempt 1 was mid-run**, killing it; the redelivered
attempt 2 restart then left the shared, PR/sha-keyed report tree inconsistent,
and Finish Workflow trusts that tree.

## Timeline (UTC)

| time | event |
|---|---|
| 14:01:43 | autoscaler `desired 0→1` (default orchestrator ASG) |
| 14:01:51 | i-08bf0876d1d804cc3 launches (default pool, attempt 1) |
| 14:02:23 | i-08bf starts orchestrating PR#144 "Praktika CI Advanced"; kicks Config + level-2 jobs |
| 14:02:29 | attempt-1 Config Workflow completes → resets the shared PR/sha report tree to the plan (`push_pending_ci_report`, version=0) |
| 14:02:49 | **autoscaler `desired 1→0`** — reads visible queue depth = 0 (the message is in-flight/invisible) → concludes idle |
| 14:02:55 | i-08bf terminated (`Client.UserInitiatedShutdown`) **mid-run**, without deleting its SQS message; buffered orchestrator stdout lost |
| 14:02:56 | attempt-1 Praktika Pytests completes on its runner (kicked before the kill) |
| 14:03:43 | autoscaler `desired 0→1` → i-043ed4b5fce591033 launches |
| ~14:12 | attempt-1 SQS message redelivers (600s visibility) → i-043e runs as **attempt 2/3** |
| 14:12–14:17 | attempt 2 runs to completion; every job writes a `success` result |
| 14:17:32 | attempt-2 **Finish Workflow** (runner i-0f7e90cf29bc12272) reads the shared report tree, finds Style Check + both Parametrized *not-completed*, stamps them `NOT_FINALIZED` |
| ~14:33+ | manual Code Review re-run (resume) — unrelated, working correctly |

(The base pool ran "Praktika CI" concurrently on i-0fb2 → `praktika_ci/*`, a
**separate** report tree. That fan-out is normal and did not collide.)

## Root causes

1. **Autoscaler scale-in-while-processing race** (primary trigger; previously
   deferred). The lambda autoscaler set the default orchestrator ASG `desired
   1→0` at 14:02:49 based on **visible** queue depth, but the message was
   in-flight (invisible) and being processed by i-08bf. It terminated the working
   orchestrator, forcing a 600s-delayed redelivery + restart. ASG scaling
   activity confirms it (`shrinking capacity 1→0 … i-08bf selected for
   termination`).

2. **Finish Workflow is not restart-tolerant.** It marks a job `NOT_FINALIZED`
   from the **aggregate** workflow-result tree alone
   (`native_jobs.py:969-996`), checking only the GitHub job status as a fallback
   — never the job's own persisted `result_<job>.json` / `final.json`, which said
   `success`. The attempt-1 kill + attempt-2 restart left the shared,
   PR/sha-keyed tree inconsistent for the level-2 jobs (Config's version=0 reset
   across two attempts + per-job version-CAS pushes, plus attempt-1's kicked
   `job_task`s that can redeliver late), so a job that really succeeded was
   reported as failed.

3. **Mid-run kill loses orchestrator logs.** The controller ran the orchestrator
   via `subprocess.run` with stdout inherited but Python block-buffered (not a
   TTY), so progress lines only flushed at exit — and were lost when the process
   was killed. This is why i-08bf's orchestrator output was unavailable for
   diagnosis. **Fixed** (see below).

## Fixes

- **[done] Stream subprocess logs live.** `_praktika_env` now sets
  `PYTHONUNBUFFERED=1`, so orchestrator/runner stdout flushes per-line to
  CloudWatch and survives a mid-run kill. (`bootstrap/src/praktika_controller/controller.py`)
- **[proposed] Restart-tolerant Finish Workflow.** Before declaring a job
  `NOT_FINALIZED`, re-read that job's own `result_<job>.json` (or
  `runs/<run_id>/<job>/final.json`) and trust a terminal result there. This alone
  would have rendered this run all-green despite the dirty tree.
- **[deferred] Autoscaler scale-in race.** Don't scale a pool in while any of its
  messages are in-flight (account for in-flight/invisible messages, or gate
  scale-in on the controller's own idle signal rather than visible queue depth).
  This is the harder, known-deferred fix; it removes the trigger.

## Notes

This is the same concurrency class as the re-run hardening work
([RERUN_HARDENING.md](./RERUN_HARDENING.md)), but on the **initial-run** path:
an orchestrator killed mid-run + a shared PR/sha-keyed report artifact. The
re-run `resume.lock` does not cover it because the collision is between two
attempts of one trigger, not two triggers.
