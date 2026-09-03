---
title: CI Engine — Lifecycle & Message/State Flow
description: End-to-end interaction between the webhook Lambda, orchestrator, runners, the two SQS queue tiers, and every S3 sync object (who reads/writes what).
sidebar_label: CI Engine Lifecycle
doc_type: reference
---

# CI Engine — lifecycle & flow {#ci-engine-lifecycle}

How the pieces talk. **SQS carries forward dispatch only** (two hops); **everything
that flows back — completion, liveness, cancel, re-run, run state — goes through
S3.** GitHub Checks is the only thing talking to the user.

See also [PROTOCOL.md](./PROTOCOL.md) (contracts) and
[RERUN_HARDENING.md](./RERUN_HARDENING.md) (open re-run work).

## Components

| Component | Runs on | Consumes | Produces |
|---|---|---|---|
| **Lambda** (`gh-trigger`) | AWS Lambda | GitHub webhooks | → workflow-trigger SQS; S3 cancel/rerun/throttle markers; GitHub gate checks |
| **Orchestrator** | EC2 ASG `praktika-workflow-orchestrator[-base]` | workflow-trigger SQS | → per-runner-type SQS (`job_task`); S3 run state; GitHub checks |
| **Runner** | EC2 ASG `praktika-<runner-type>` (`…-base`, `pr-…`, `…-bedrock`, …) | per-runner-type SQS | S3 heartbeat/final; GitHub check transitions are owned by the orchestrator |

Both orchestrator and runner run the **same** `praktika-controller` poller; its
`praktika_role` instance tag (`workflow_orchestrator` vs `job_runner`) picks the
role.

## SQS queues (forward dispatch only)

| Queue | Producer → Consumer | Message |
|---|---|---|
| `praktika_clickhouse_workflows` (a.k.a. workflow-trigger) | Lambda → Orchestrator | `pull_request` / `push` trigger, or `rerun` (finished-run resume) |
| `praktika-<runner-type>` | Orchestrator → Runner | `job_task` |

There is **no** per-run SQS queue (retired). Everything else is S3.

## S3 objects — who writes, who reads

Bucket: `Settings.S3_ARTIFACT_BUCKET` (e.g. `praktika-artifacts-eu-north-1`).

| Key | Writer | Reader | Purpose |
|---|---|---|---|
| `runs/<run_id>/state.json` | Orchestrator (each loop + `finalized=true` at end) | Lambda (running-vs-finished routing), resume Orchestrator (seed), Runner (finalized guard) | DAG snapshot: per-job status/check_id/rc/rerun_count, env, `finalized` |
| `runs/<run_id>/<job>/final.json` | Runner (on job exit) | Orchestrator (`sweep_completions`) | `{rc, environment, result}` — job completion (replaces SQS completions) |
| `runs/<run_id>/<job>/heartbeat.json` | Runner (every `heartbeat_interval_s`) | Orchestrator (`sweep_liveness`) | `{ts, phase, instance_id, attempt}` — liveness |
| `runs/<run_id>/cancel` (kill flag) | Orchestrator (`cancel_unfinished_jobs`) | Runner (`CancelWatchdog` + pre-clone guard) | tells running runners to kill the job subprocess |
| `runs/<run_id>/cancel-request` | Lambda (UI Cancel button) | Orchestrator (`sweep_cancel`) | manual cancel of one run |
| `pr/<pr>/cancel-before-<scope>` | Lambda (`synchronize`/new push) | Orchestrator (`sweep_cancel`) | `{ts, head_sha}` — older in-scope runs self-cancel |
| `pr/<pr>/rerun-throttle` | Lambda (conditional `IfNoneMatch`) | Lambda | one re-run per PR per `RERUN_MIN_INTERVAL_S` |
| `runs/<run_id>/rerun-request/<delivery>.json` | Lambda (live partial re-run) | Orchestrator (`sweep_rerun`, consume-once) | `{jobs:[...]}` a live run should reset+re-run |
| `external-pr-approvals/<repo>/pr/<n>.json` | Lambda (gate approve/store) | Lambda | fork-PR approval state |

## Normal run (push / PR)

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant L as Lambda
    participant WQ as workflow SQS
    participant O as Orchestrator
    participant S3 as S3
    participant RQ as runner SQS
    participant R as Runner
    participant CK as GitHub Checks

    GH->>L: webhook (pull_request/push)
    L->>L: verify HMAC; refetch PR (drop if head advanced)
    opt synchronize
        L->>S3: put pr/<pr>/cancel-before-<scope> {ts,sha}
    end
    L->>WQ: enqueue trigger {type,pr,head_sha,…}
    O->>WQ: receive trigger (visibility heartbeat)
    O->>CK: open top-level check  (run_id = check id)
    O->>O: clone PR head, build DAG
    loop each ready job
        O->>CK: create per-job check (queued, external_id={run_id,job})
        O->>RQ: send job_task {run_id, state_s3_key, cancel_s3_key, heartbeat/final keys, environment, rerun_count}
    end
    O->>S3: put state.json (finalized=false) — each loop
    R->>RQ: receive job_task
    R->>S3: head cancel_s3_key? (skip if cancelled)
    R->>S3: get state.json — skip if finalized
    R->>R: clone refs/pull/<n>/head
    loop while running
        R->>S3: put <job>/heartbeat.json
    end
    O->>S3: get <job>/heartbeat.json (sweep_liveness)
    O->>CK: per-job check → in_progress
    R->>R: run job (optionally in Docker)
    R->>S3: put <job>/final.json {rc,env,result}
    R->>RQ: delete job_task
    O->>S3: get <job>/final.json (sweep_completions)
    O->>CK: per-job check → success/failure
    Note over O: advance DAG; repeat until all terminal
    O->>S3: put state.json (finalized=true)
    O->>CK: complete top-level check
    O->>WQ: delete trigger
```

## Cancel

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant L as Lambda
    participant O as Orchestrator (live)
    participant S3 as S3
    participant R as Runner

    alt UI "Cancel" button
        GH->>L: check_run.requested_action (cancel)
        L->>S3: put runs/<run_id>/cancel-request
    else new push (supersede older runs)
        GH->>L: pull_request.synchronize
        L->>S3: put pr/<pr>/cancel-before-<scope> {ts,sha}
    end
    O->>S3: sweep_cancel — get cancel-request / cancel-before
    Note over O: cancel-request present, OR cancel-before.ts > my event_ts & different sha
    O->>O: state.cancelled = True; cancel unfinished jobs
    O->>S3: put runs/<run_id>/cancel  (kill flag)
    R->>S3: CancelWatchdog polls cancel → kills job subprocess
    O->>GH: top-level check → cancelled
```

## Partial re-run — running workflow

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant L as Lambda
    participant S3 as S3
    participant O as Orchestrator (live)
    participant RQ as runner SQS
    participant R as Runner

    GH->>L: check_run.rerequested (per-job check)
    L->>L: parse external_id → (run_id, job)
    L->>L: refetch PR — reject if head advanced; fork ⇒ require maintainer
    L->>S3: get runs/<run_id>/state.json → finalized=false (running)
    L->>S3: put pr/<pr>/rerun-throttle (IfNoneMatch; 1/window)
    L->>S3: put runs/<run_id>/rerun-request/<delivery>.json {jobs}
    O->>S3: sweep_rerun — list+get rerun-request/*
    O->>S3: delete each read request (consume-once)
    O->>O: apply_rerun: reset job + failed downstream → PENDING (cap MAX_RERUNS_PER_JOB; skip if non-terminal)
    O->>S3: delete <job>/final.json + heartbeat.json (else stale completion finishes it)
    O->>RQ: re-kick job_task
    Note over O,R: same runner flow as a normal job
```

## Partial re-run — finished workflow (resume)

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant L as Lambda
    participant S3 as S3
    participant WQ as workflow SQS
    participant O2 as Orchestrator (fresh)
    participant CK as GitHub Checks

    GH->>L: check_run.rerequested (per-job check)
    L->>L: parse external_id; validate (head, maintainer); throttle
    L->>S3: get runs/<run_id>/state.json → finalized=true
    L->>WQ: enqueue {type:"rerun", run_id, rerun_jobs, +PR meta}
    O2->>WQ: receive rerun message
    O2->>S3: get runs/<run_id>/state.json (load snapshot)
    O2->>CK: reopen SAME top-level check (reuse run_id)
    O2->>O2: seed jobs from snapshot (statuses, check_ids, env)
    O2->>S3: clear stale cancel-request / cancel
    O2->>O2: apply_rerun(rerun_jobs) → reset target + failed downstream
    O2->>S3: put state.json (finalized=false)
    Note over O2: drive DAG loop → re-dispatch reset jobs (normal runner flow)
    O2->>S3: put state.json (finalized=true)
    O2->>CK: complete top-level check
```

## Liveness & recovery (S3-only, no SQS completions)

- **Runner dies mid-job** → its `job_task` isn't deleted → SQS redelivers after
  `visibility_timeout` → a fresh runner re-runs it, writing the same
  `heartbeat`/`final` keys. `sweep_liveness` rides through via heartbeat timers.
- **Orchestrator dies** → the workflow-trigger message redelivers → a fresh
  orchestrator re-processes and picks up already-written `final.json` from S3.
- **Guards before a runner does work**: skip if `runs/<run_id>/cancel` exists
  (cancelled), or if `state.json.finalized` is true (no orchestrator will consume
  the result).
