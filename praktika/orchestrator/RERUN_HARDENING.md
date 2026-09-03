---
title: Partial Re-run — Hardening (design notes)
description: Open concurrency/correctness problems in finished-run partial re-run, with scenarios, and a proposed generation + lease design.
sidebar_label: Partial Re-run Hardening
doc_type: design
---

# Partial re-run — hardening {#rerun-hardening}

Status: **design / not yet implemented.** Captures the remaining hardening work
for partial re-run (see [PROTOCOL.md → Partial re-run](./PROTOCOL.md#partial-rerun)
for how the shipped feature works). Written up so it can be picked up later.

## Background (what's already shipped)

A re-run of a single failed check re-runs only that job + its failed downstream,
in place on the existing run, whether the run is still running or finished:

- **Running run** → the lambda drops `runs/<run_id>/rerun-request/<delivery>.json`;
  the live orchestrator's `sweep_rerun()` batches all such keys each loop and
  resets the jobs. Safe: one live orchestrator, and it batches.
- **Finished run** → the lambda enqueues a `type:"rerun"` SQS message; a fresh
  orchestrator (`_orchestrate_resume`) reloads `runs/<run_id>/state.json`, resets
  the requested jobs, and re-drives the DAG **reusing the same `run_id`** (so the
  S3 state prefix and per-job `external_id`s stay stable).

Two review findings are already fixed:

- **SHA / auth guard** — a partial re-run refetches the PR and rejects it if the
  live head advanced past the check's sha (runners clone `refs/pull/N/head`, the
  live head), and requires a maintainer for fork PRs.
- **Cancelled-run resume** — resume now clears the previous generation's
  `cancel-request` / `cancel` markers so a re-run of a job from a *cancelled*
  workflow isn't immediately re-cancelled.

A coarse **per-PR throttle** (`RERUN_MIN_INTERVAL_S`, default 120s) also limits
re-runs to one per PR per window as a first line of defence.

The problems below are the **finished-run** path specifically — they don't apply
to the running-run path (single live orchestrator + batching).

## The remaining problems

### P1 — Concurrent finished-run resumes are not serialized

The finished-run path enqueues **one SQS message per click**. The orchestrator
pool is `size=0`/Auto, so N messages can be picked up by **N orchestrator
instances at once**, all resuming the **same `run_id`**.

**Scenario**
1. Run 500 for PR#144 finishes; `Test` and `Style Check` are both red.
2. User multi-selects both and clicks "Re-run" → two `check_run.rerequested`
   webhooks fire ~simultaneously.
3. Lambda reads `finalized=true` for both → enqueues **two** `type:"rerun"`
   messages.
4. Autoscaler starts **two** orchestrator instances. Both:
   - clone into the same per-PR dir (`/opt/praktika/work/pr-144`),
   - reopen the same check ids,
   - load the same `state.json`, reset their own job, and both
     `save_snapshot()` — last writer wins, so one reset is lost,
   - dispatch to the same `runs/<run_id>/…` completion keys.
5. Result: clobbered snapshots, one job's reset silently dropped, possibly a
   checkout yanked out from under the other instance.

The throttle mitigates this (a resume takes ~15s ≪ 120s window) but it's a
rate-limit, not a guarantee — if the throttle fails open (e.g. an S3 error) or
the window is tuned down, the race is back.

### P2 — Finish race: a re-run request can be silently lost

The lambda decides running-vs-finished by reading the `finalized` flag in
`state.json`. There's a read/write gap against the orchestrator finalizing.

**Scenario**
1. Run 500 is on its last job; the orchestrator is about to finalize.
2. Lambda handles a re-run click: reads `state.json` → `finalized=false` (still
   running) → writes `runs/<run_id>/rerun-request/<d>.json`, expecting the live
   orchestrator to pick it up. It does **not** enqueue a resume message.
3. The orchestrator had *already* run its final `sweep_rerun()` (found nothing)
   an instant earlier, and now writes `finalized=true` and exits.
4. No live orchestrator remains to consume the request, and no resume message
   was enqueued → **the user's re-run is silently lost** (the check just stays
   red, nothing happens).

The current "final `sweep_rerun()` before finalize" narrows the window but does
not close it: the request can land *after* that sweep.

### P3 — A stale redelivered runner can overwrite a re-run's result

`heartbeat.json` and `final.json` are keyed per **job**, not per **attempt**.

**Scenario**
1. `Test` (attempt 1) is dispatched; its runner is declared dead (heartbeat
   timeout) but its SQS `job_task` is still within `maxReceiveCount` and will be
   redelivered.
2. User re-runs `Test`. `_reset_job` deletes the old `final.json`/`heartbeat.json`
   and dispatches attempt 2 to a fresh runner (writing the **same** keys).
3. The redelivered attempt-1 runner now wakes up, finishes, and writes
   `runs/<run_id>/Test/final.json` — **the same key attempt 2 uses**.
4. The orchestrator's `sweep_completions` reads attempt 1's stale `final.json`
   and completes the check with attempt 1's (old) result — before attempt 2
   finishes, or overwriting attempt 2's result.

### P4 — Throttle window-refresh is not serialized

The per-PR re-run throttle (`_rerun_throttled` in `lambda_gh_trigger.py`) is
atomic only when the marker is **absent**: it claims a window with a conditional
create (`put IfNoneMatch='*'`). When the marker **exists but the window has
expired**, it refreshes with an **unconditional** `put` and returns "allowed" —
and that path is not serialized.

**Scenario**
1. A re-run happened >`RERUN_MIN_INTERVAL_S` ago, so `pr/<pr>/rerun-throttle`
   holds a stale timestamp.
2. Two re-run webhooks for the PR arrive together. Both `IfNoneMatch` creates
   fail (marker present), both read the same old `ts`, both see the window
   expired, both do the unconditional `put`, and **both return allowed**.
3. For a finished run that enqueues **two resume controllers**, racing on the
   same `run_id` / snapshot / checkout / completion keys (the exact concurrency
   the throttle exists to prevent — see P1).

Note the common multi-select case (no marker yet) *is* serialized by the initial
`IfNoneMatch` create; only the stale-window-refresh path has the hole.

**Fix options**
- Make the throttle *create-only*: encode the window in the key,
  `pr/<pr>/rerun-throttle-<floor(event_ts / interval)>`, claimed with
  `IfNoneMatch='*'`. Exactly one caller wins each window's create; no timestamp
  read, no unconditional overwrite. (Minor: a boundary-straddling pair can each
  win adjacent windows — a throttle-accuracy nit, not a concurrency race.)
- Or rely on the per-run **lease** below (P1), which serializes resumes directly
  and makes the throttle just a cheap first filter.

## Proposal — a per-run "resume generation" behind an atomic lease

All three collapse into one primitive: each resume is a new **generation** of the
run, and only one orchestrator may own a generation at a time (an atomic
**lease**).

### 1. Generation counter

Add `generation: int` to `state.json` (0 = original run, +1 per resume).

### 2. Generation-scoped S3 keys — fixes **P3**

Move the per-attempt signals under the generation:

```
runs/<run_id>/g<gen>/<job>/heartbeat.json
runs/<run_id>/g<gen>/<job>/final.json
runs/<run_id>/g<gen>/cancel
```

The `job_task` carries `generation`; the runner writes/reads `g<gen>` keys; the
orchestrator sweeps only its own generation. A stale attempt-1 runner writes
`g0` keys, which the `g1` resume never reads → no overwrite, no premature
completion. (`state.json`, `cancel-request`, and `rerun-request` stay
run-scoped — they are generation-agnostic control signals.)

### 3. Atomic lease — fixes **P1**

On resume, claim the run before touching anything, using an S3 conditional
create (S3 supports `If-None-Match: *`):

```
put runs/<run_id>/resume.lock  (If-None-Match:*, body={generation, instance_id, ts})
  win  → own this resume; generation = prev+1; drain requests + run; delete lock at end
  lose → another resume in flight → drop this delivery (its jobs are already in the
         batched rerun-request keys the winner will drain)
```

Stale-lease recovery: if the existing lock's `ts` is older than a timeout (the
owner died), allow a conditional takeover. This is the real serialization — one
claimant per run — so concurrent finished-run resumes can no longer reuse each
other's checkout / keys.

### 4. Write-then-check handshake — fixes **P2**

- **Lambda** (every re-run): (a) write the `rerun-request` key **first**;
  (b) *then* read `finalized`; (c) if `finalized == true` → enqueue a resume
  trigger.
- **Orchestrator finalize**: write `finalized=true`, then run **one more**
  `sweep_rerun()`; if it finds a request → set `finalized=false` and keep going.

Because the request write happens-before the lambda's `finalized` read:
- if that read sees `true` → the lambda enqueues a resume (a fresh orchestrator
  claims + drains the request);
- if it sees `false` → the orchestrator hasn't finalized yet, so its
  post-finalize recheck runs *after* the request write and drains it.

Either path → the request is never stranded.

Also make the finished-run path **always write a `rerun-request` key** (like the
running path), and have the resume orchestrator **batch-drain all** pending
requests under its lease, instead of acting only on its own message's
`rerun_jobs`. This unifies running and finished handling and lets multiple
finished-run clicks converge into one resume.

## Mapping

| Problem | Closed by |
|---|---|
| P1 — concurrent resumes | the lease (single claimant per run) |
| P2 — finish race / lost request | write-then-check + finalize-recheck handshake |
| P3 — stale-attempt overwrite | generation-scoped `g<gen>` keys |
| P4 — throttle window-refresh race | create-only window-keyed throttle, or the lease (P1) |

With the lease in place, the per-PR throttle becomes an optional cheap
first-line filter rather than the correctness mechanism, and could be removed.

## Suggested increments

1. **Generation-scoped keys (P3)** — self-contained: add `generation` to the
   snapshot + `job_task`, key `heartbeat`/`final`/`cancel` by generation, sweep
   the current generation. High value, low blast radius.
2. **Lease (P1)** — `resume.lock` conditional-create + stale takeover; losers
   drop.
3. **Handshake + always-write-request + batch-drain (P2)** — lambda writes
   request first then checks `finalized`; orchestrator finalize-recheck.

## Touch points

- `praktika/orchestrator/state.py` — `generation` field; `g<gen>` key builders;
  `save_snapshot`/`seed_from_snapshot`; lease helpers; finalize recheck.
- `praktika/orchestrator/__init__.py` — `_orchestrate_resume`: claim lease, bump
  generation, batch-drain, run; `_drive_dag` finalize recheck.
- `praktika/orchestrator/job_runner.py` — read `generation` from the task, write
  `g<gen>` keys.
- `praktika/infrastructure/native/lambda_gh_trigger.py` — always write
  `rerun-request`; write-then-check-`finalized` handshake; enqueue-on-finalized.
- IAM: the webhook role already has `runs/*/rerun-request/*` and
  `runs/*/state.json`; add `runs/*/resume.lock` if the lambda ever writes it
  (currently only orchestrators would).
