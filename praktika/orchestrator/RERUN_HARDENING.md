---
title: Partial Re-run — Hardening (design notes)
description: Unified running/finished re-run design (S3 request-log + finalized signal + per-run boot lock), the concurrency problems it closes, and the one remaining orthogonal problem (stale-attempt overwrite).
sidebar_label: Partial Re-run Hardening
doc_type: design
---

# Partial re-run — hardening {#rerun-hardening}

Status: **unified re-run implemented; P3 + one narrow residual remain.** The
running/finished unification below (a single S3 request log, the `finalized`
liveness signal, a per-run `resume.lock` boot-lease, and the finalize-recheck
handshake) is now the shipped behaviour and closed **P1/P2/P4**. What's left is
**P3** (stale-attempt overwrite) and the **finalize-window redundant-resume**
residual — both documented below. See
[PROTOCOL.md → Partial re-run](./PROTOCOL.md#partial-rerun) for the shipped flow.

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

A coarse **per-PR throttle** (`RERUN_MIN_INTERVAL_S`, default 120s) currently
limits re-runs to one per PR per window as a first line of defence.

The two paths above (separate S3-request vs SQS handling, plus a per-PR
rate-limit) are what the **Unified re-run** design below replaces.

## Unified re-run (implemented)

The running and finished paths collapse into **one** rule with a single shared
request log in S3. Three signals carry all the coordination — no generation
counter, no TTL, no user-facing rate-limit:

- **`runs/<run_id>/rerun-request/<delivery>.json`** — the request log. *Every*
  re-run writes here, always, after validation. Delivery-id-named so concurrent
  clicks never collide; consumed (deleted) by whichever orchestrator drains them.
- **`finalized` in `state.json`** — the "is a live orchestrator present?" signal.
  `false` while an orchestrator is driving (original run *or* an in-flight
  resume); `true` only when no orchestrator is running.
- **`runs/<run_id>/resume.lock`** — a per-run **boot lock** that serializes
  *spawning* a resume orchestrator for a finished run. Created by the lambda
  (conditional), deleted by the orchestrator once it's up. It only has to cover
  the SQS→orchestrator-ready boot window; after that, `finalized=false` is the
  live signal. No timer — see "Why no TTL".

### Lambda — every re-run (after SHA / maintainer validation)

```
1. write runs/<run_id>/rerun-request/<delivery>.json   (the job list)
2. read finalized from state.json
3. finalized == false  → done. A live orchestrator will sweep it.
4. finalized == true    → create runs/<run_id>/resume.lock  (IfNoneMatch='*')
     win  → send SQS {type: rerun, run_id}
     lose → done. Another resume is already being spawned; it will batch-drain
            this request too (it's already in S3).
```

There is **no throttle**. A user clicking five jobs on a running workflow just
writes five request keys; the live orchestrator batches them, and the existing
`MAX_RERUNS_PER_JOB` cap + terminal-only guard in `apply_rerun` stop any
runaway. The `resume.lock` is not a rate-limit — it's a spawn-lease that stops
*two orchestrators* starting for the same finished run, nothing more.

### Orchestrator — resume (`_orchestrate_resume`)

```
1. load snapshot
2. seed_from_snapshot + apply_rerun + sweep_rerun   (batch-drain ALL pending requests)
3. save_snapshot(finalized=false, required=True)    ← live signal now ON
4. delete runs/<run_id>/resume.lock                 ← only AFTER step 3 (see below)
5. drive DAG …
   finalize: write finalized=true → one more sweep_rerun()
       found → set finalized=false, keep driving
       none  → delete resume.lock + exit
```

**Ordering matters.** The lock must be deleted *after* the `finalized=false`
write, not before. If it were deleted first there'd be a gap where the lock is
gone **and** `finalized` is still `true` — a concurrent click would create a new
lock and spawn a *second* orchestrator on the same `run_id`. Deleting after
`finalized=false` hands the "live orchestrator present" signal over atomically:
by the time the lock is gone, new clicks already read `finalized=false` and take
the running path. Deleting the lock again on later sweeps / in the exit path is
harmless idempotent insurance — only the *first* delete's ordering matters.

The resume orchestrator **batch-drains all** pending `rerun-request` keys under
its lock, not just its own message's jobs. This is what lets multiple
finished-run clicks (and the losers of the lock race) converge into one resume.

### Orchestrator finalize handshake (closes the running-path finish race)

Finalize is not "write `finalized=true` and exit" — it's write `finalized=true`,
then run **one more** `sweep_rerun()`; if that finds a request, set
`finalized=false` and keep driving. This is what makes the lambda's
"write-request-then-read-finalized" safe on the *running* path:

- lambda read sees `false` → the orchestrator hasn't finalized yet, so its
  post-finalize recheck runs *after* the request write (happens-before via S3
  strong consistency) and drains it;
- lambda read sees `true` → the orchestrator has finalized, so the lambda takes
  the finished path and spawns a resume.

Either way the request is never stranded.

### Why no TTL on the lock

**SQS redelivery is the crash-recovery, so the lock needs no timer.** The lock
only covers the boot window (lambda sends SQS → orchestrator writes
`finalized=false` → deletes lock). The only way it's left set is an orchestrator
that crashed *inside* that window — but that same crash means the `type:rerun`
SQS message was never acked, so it redelivers on visibility-timeout and a fresh
orchestrator picks up the same `run_id`, deletes the (now stale) lock
unconditionally on boot, and proceeds. The lock being present never blocks the
*orchestrator* — only the *lambda* from spawning duplicates — so a redelivered
orchestrator sails past it.

The one case redelivery doesn't cover is the message exhausting
`maxReceiveCount` → DLQ (the orchestrator crashed on every attempt). Then the run
stays wedged with the lock set — but that's **visible** (a message parked in the
DLQ), identical to any other terminally-failing run, and strictly better than a
silent timer-based takeover. Accept it.

### Concurrency walkthrough

- **Multi-select on a finished run** — user re-runs `Test` + `Style Check`; two
  `rerequested` webhooks fire together. Both lambdas write their request to S3,
  both read `finalized=true`, both try to create `resume.lock`; the conditional
  create lets exactly one win → one SQS → one orchestrator. It batch-drains
  *both* requests. The loser sent no SQS. **One resume, both jobs.** (closes P1)
- **Finish race on a running run** — lambda writes request, reads
  `finalized=false`, relies on the live orchestrator; the orchestrator is
  finalizing at that instant. Its post-finalize recheck sweep runs after the
  request write → drains it and keeps driving. (closes P2)
- **Stale throttle window** — there is no throttle window to go stale; the lock
  is create-only and explicitly deleted. (closes P4)
- **Resume orchestrator dies during boot** — SQS message redelivers → fresh
  orchestrator removes the stale lock and resumes.

## Still remaining — finalize-window redundant resume

The finalize-recheck handshake orders the write as `finalized=true` **then**
recheck-sweep, which guarantees no *lost* request (P2). The cost is a tiny window
between those two steps where a click sees `finalized=true` (so the lambda claims
`resume.lock` and spawns a resume) **and** the still-alive orchestrator's recheck
sweep also drains the same request and keeps driving — so the job runs twice: once
on the live orchestrator, once on the spawned resume, racing on the same run_id.
It is **redundant work, never a lost or wrong result** (both attempts delete
stale `final.json` on reset, and `MAX_RERUNS_PER_JOB` caps the churn), and the
window is sub-second, once per finalize. Chosen deliberately over the reverse
order (sweep-then-finalize), which trades this for a *silently lost* request —
the worse failure. The lease-generation design closes it fully; deferred.

## Still remaining — P3: a stale redelivered runner can overwrite a re-run result

This one is **orthogonal** to the running/finished unification above and is *not*
closed by it. `heartbeat.json` and `final.json` are keyed per **job**, not per
**attempt**.

**Already mostly covered:** the runner's pre-clone `_run_is_finalized` guard
(`controller.py`) makes a redelivered stale runner skip whenever the run is
finalized — which is the normal state a re-runnable job is in. The scenario below
only bites in the narrow overlap where the stale runner redelivers *during* an
active resume (which deliberately sets `finalized=false`), and even then
`_reset_job` has already deleted the old `final.json`. So this is a low-
probability residual, not a routine failure.

**Scenario**
1. `Test` (attempt 1) is dispatched; its runner is declared dead (heartbeat
   timeout) but its `job_task` is still within `maxReceiveCount` and will be
   redelivered.
2. User re-runs `Test`. `_reset_job` deletes the old
   `final.json`/`heartbeat.json` and dispatches attempt 2 to a fresh runner
   (writing the **same** keys).
3. The redelivered attempt-1 runner wakes up, finishes, and writes
   `runs/<run_id>/Test/final.json` — the same key attempt 2 uses.
4. The orchestrator's `sweep_completions` reads attempt 1's stale `final.json`
   and completes the check with attempt 1's (old) result — before attempt 2
   finishes, or overwriting attempt 2's.

**Fix — attempt-scoped completion keys.** `_reset_job` already bumps
`rerun_count`; carry it on the `job_task` and key the per-attempt signals by it:

```
runs/<run_id>/<job>/a<rerun_count>/heartbeat.json
runs/<run_id>/<job>/a<rerun_count>/final.json
```

The runner writes/reads `a<n>` keys; the orchestrator sweeps only the current
attempt's key. A stale attempt-1 runner writes `a0` keys, which the `a1`
orchestrator never reads → no overwrite, no premature completion. (`state.json`,
`cancel-request`, and `rerun-request` stay run-scoped — they're
attempt-agnostic control signals.)

## Mapping

| Problem | Status |
|---|---|
| P1 — concurrent finished-run resumes | **closed** — conditional `resume.lock` create (single spawner) + batch-drain |
| P2 — finish race / lost request | **closed** — write-request-then-read-`finalized` + finalize-recheck handshake |
| P4 — throttle window-refresh race | **closed** — partial-path throttle removed; lock is create-only (full-rerun path keeps the coarse throttle) |
| Finalize-window redundant resume | **open** (narrow, benign) — closed only by the lease-generation design |
| P3 — stale-attempt overwrite | **open** (narrow) — attempt-scoped `a<n>` completion keys |

## Increments

1. **Unify the request path** — *done.* The lambda always writes `rerun-request`
   then reads `finalized`; the finished path is guarded by the conditional
   `resume.lock`; the resume orchestrator batch-drains all requests, writes
   `finalized=false`, then deletes the lock (in that order). The partial-path
   throttle is removed (the full-rerun path keeps its coarse throttle). Closed
   P1/P4.
2. **Finalize handshake** — *done.* `_drive_dag` finalize writes `finalized=true`,
   does one more `sweep_rerun()`, and un-finalizes + continues if it finds
   anything. Closed P2 (at the cost of the narrow finalize-window residual above).
3. **Attempt-scoped keys (P3)** — *not done.* Carry `rerun_count` on the
   `job_task`, key `heartbeat`/`final` by `a<n>`, sweep the current attempt.
   Self-contained, low blast radius; can land independently.
4. **Lease-generation** — *not done.* The full lease + generation counter from
   the earlier proposal closes both the finalize-window residual and P3 together;
   only worth it if the narrow residuals prove to bite in practice.

## Touch points (implemented for 1–2; remaining for 3–4)

- `praktika/infrastructure/native/lambda_gh_trigger.py` — `_handle_partial_rerun`
  writes the `rerun-request` key first, then reads `finalized`; on `finalized`,
  `_claim_resume_lock` conditional-creates `runs/<run_id>/resume.lock` and only
  sends SQS if it won. The partial path no longer throttles.
- `praktika/orchestrator/__init__.py` — `_orchestrate_resume` batch-drains,
  `save_snapshot(finalized=false, required=True)`, then `delete_resume_lock()`;
  `_drive_dag` finalize writes `finalized=true` then re-sweeps + un-finalizes.
- `praktika/orchestrator/state.py` — `delete_resume_lock()`; `sweep_rerun()`
  batches; `save_snapshot` carries the `finalized` flag.
- `praktika/orchestrator/job_runner.py` — (P3, remaining) read `rerun_count` from
  the task, write `a<n>` keys.
- IAM — `runs/*/resume.lock` added to the webhook role's `artifact_resources`
  (create); the orchestrator EC2 role deletes it under its bucket-wide
  `s3:DeleteObject`.
