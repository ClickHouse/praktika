---
title: CI Engine — Communication Protocol & Test Scenarios
description: Architecture, message formats, cancel semantics, and test checklist for the standalone CI engine.
sidebar_label: CI Engine Protocol
sidebar_position: 10
slug: /ci-engine/protocol
doc_type: reference
---

# CI Engine — Communication Protocol & Test Scenarios {#ci-engine-protocol}

## Components {#components}

| Component | Runs on | SQS queues |
|---|---|---|
| **Lambda** | AWS Lambda | produces → `praktika_clickhouse_workflows`, `praktika-wf-{pr}-{run_id}` |
| **Orchestrator** | EC2 ASG `praktika-workflow-orchestrator-asg` (×2) | consumes `praktika_clickhouse_workflows`, produces → `praktika-{runner-type}`, owns ↔ `praktika-wf-{pr}-{run_id}` |
| **Job runner** | EC2 ASG `praktika-{runner-type}` (e.g. `praktika-arm-2xsmall`) | consumes `praktika-{runner-type}`, produces → `praktika-wf-{pr}-{run_id}` |

## Queue design {#queue-design}

| Queue | Purpose |
|---|---|
| `praktika_clickhouse_workflows` | Workflow triggers (one message per PR push / rerun). |
| `praktika-{runner-type}` | Job tasks dispatched by the orchestrator to a specific runner pool. |
| `praktika-wf-{pr}-{run_id}` | **Per-run** bidirectional queue owned by one orchestrator. Carries `job_completion` (from runners) and `cancel` (from Lambda). Created when the orchestrator starts, deleted when it finishes. `run_id` is the top-level GitHub check run ID. |

## Design notes {#design-notes}

- **One queue per run, not per PR.** Every message on the queue is addressed to exactly one orchestrator, so `wait` needs no filtering — `cancel` means cancel, `job_completion` means advance the DAG. Concurrent runs for the same PR (e.g. a rerun while a push is in flight) use disjoint queues and never contend.
- **`run_id` = top-level check run ID.** Encoding it in the queue name lets the Lambda address a specific run by name, using only the information GitHub puts in the webhook payload. No external run-id ↔ queue mapping is needed.
- **Cancel dispatch is routing, not filtering.** UI Cancel hits exactly one queue (`praktika-wf-{pr}-{check_id}`); `synchronize` fans out via `list_queues(QueueNamePrefix="praktika-wf-{pr}-")`. A freshly pushed run hasn't created its queue yet, so the fan-out naturally excludes it.
- **Re-run (`check_suite` / `check_run.rerequested`) never sends a cancel.** A rerun spawns a new check run (new `run_id`, new queue). There is no previous run for that same queue to cancel into.
- **Orchestrator owns the queue.** `WorkflowState.__init__` creates it; `WorkflowState.cleanup` (in an `orchestrate` `finally`) deletes it on every exit path — normal, cancelled, or errored.

## Limitations {#limitations}

- **Orphan queues on hard kills.** If an orchestrator crashes or its EC2 instance is terminated between queue creation and `cleanup`, the queue is leaked. There is no periodic sweeper yet: orphan queues sit empty (messages expire after `MessageRetentionPeriod = 1 h`) and cost nothing, but they accumulate in the SQS console over time. A scheduled sweeper (`list_queues` with `praktika-wf-*` prefix + delete by `LastModifiedTimestamp`) is a viable follow-up; the protocol does not depend on one.
- **Cancel dropped in the startup window.** Between `CheckRun.start` (run_id exists in GitHub) and `WorkflowState.__init__` (queue exists in SQS) there is a sub-second window where a UI-triggered cancel hits `NonExistentQueue`. Too narrow to be triggerable in practice.
- **Partial rerun — "Re-run failed checks" restarts the whole workflow.** `check_run.rerequested` and `check_suite.rerequested` currently enqueue a plain `pull_request` trigger and the orchestrator runs every job in the DAG. Intended design: the Lambda captures the rerun job set — the single check's name on `check_run.rerequested`, or the names of every failed/cancelled check from the previous attempt on `check_suite.rerequested` — and passes it in the workflow message as `rerun_jobs`; the orchestrator reruns those jobs plus everything transitively downstream of them (since the target's artifact may change), and marks all other jobs `SKIPPED` (their artifacts are already in S3). Config Workflow always runs regardless so `WORKFLOW_CONFIG` is refreshed.

## Run lifecycle {#run-lifecycle}

```
GitHub webhook
  → Lambda validates HMAC-SHA256 signature
  → [on synchronize] list_queues(prefix=praktika-wf-{pr}-) → fan out cancel to every live run
  → enqueues workflow event to praktika_clickhouse_workflows

Orchestrator (one instance picks up the message)
  → creates top-level check run (status=in_progress) → run_id = check.id
  → clones PR head
  → creates praktika-wf-{pr}-{run_id}
  → builds DAG from workflow config, prints execution plan
  → Loop per DAG level:
      for each ready job:
        → creates per-job check run (status=queued)
        → dispatches job_task{check_run_id, completions_queue_url, ...}
          to praktika-{runner-type}
        → stub jobs (no matching runner pool): orchestrator drives check lifecycle
      wait() long-polls praktika-wf-{pr}-{run_id}:
        cancel         → stop loop
        job_completion → advance DAG
      on Config Workflow completion:
        → extracts WORKFLOW_CONFIG.filtered_jobs from returned environment
        → marks filtered jobs as SKIPPED, posts one aggregate "Skipped Jobs" check
  → completes top-level check run (neutral / failure / cancelled)
  → finally: delete_queue(praktika-wf-{pr}-{run_id})

Job runner
  → picks up job_task from praktika-{runner-type}
  → clones PR head
  → PATCHes per-job check run → in_progress
  → builds environment.json from task + carried environment (WORKFLOW_CONFIG, etc.)
  → runs Runner.run (praktika job, optionally inside Docker)
  → PATCHes per-job check run → completed (success / failure)
  → sends job_completion{rc, environment} to completions_queue_url
```

## Message formats {#message-formats}

### `job_task` (orchestrator → runner queue) {#job-task}

```json
{
  "type": "job_task",
  "repo": "ClickHouse/clickhouse-private",
  "pr_number": 55743,
  "head_sha": "abc123",
  "head_ref": "my-branch",
  "base_ref": "master",
  "sender": "maxknv",
  "title": "My PR title",
  "labels": [],
  "workflow_name": "PR",
  "job_name": "Style check",
  "runs_on": ["praktika-arm-2xsmall"],
  "completions_queue_url": "https://sqs.us-east-1.amazonaws.com/.../praktika-wf-55743-72611853552",
  "check_run_id": 72611853552,
  "environment": { "WORKFLOW_CONFIG": {}, "..." : "..." }
}
```

`environment` is `null` for the first job in a run (Config Workflow) and carries the
serialized `ci/tmp/environment.json` from the previous job for all subsequent jobs,
propagating `WORKFLOW_CONFIG`, `COMMIT_AUTHORS`, `JOB_KV_DATA`, etc.

`completions_queue_url` is the per-run queue: the runner sends its `job_completion`
only there, so no other run ever sees it.

### `job_completion` (runner → `praktika-wf-{pr}-{run_id}`) {#job-completion}

```json
{
  "type": "job_completion",
  "job_name": "Style check",
  "rc": 0,
  "repo": "ClickHouse/clickhouse-private",
  "pr_number": 55743,
  "head_sha": "abc123",
  "workflow_name": "PR",
  "environment": { "WORKFLOW_CONFIG": {}, "..." : "..." }
}
```

### `cancel` (Lambda → `praktika-wf-{pr}-{run_id}`) {#cancel}

```json
{"type": "cancel"}
```

Addressing is by queue name (`praktika-wf-{pr}-{run_id}`). The body needs no
discriminator — there is exactly one consumer of this queue and the message can
only mean "stop".

## Cancel semantics {#cancel-semantics}

| Trigger | Target | How it reaches the orchestrator |
|---|---|---|
| New push (`synchronize`) | Every in-flight run for the PR | Lambda lists `praktika-wf-{pr}-*` and sends `{"type":"cancel"}` to each |
| Manual Cancel button | Exactly one run | Lambda sends `{"type":"cancel"}` to `praktika-wf-{pr}-{check_run.id}` |
| Re-run (`rerequested`) | — | No cancel sent (new run has a new queue) |

Stale messages can't happen: a queue is created and destroyed with its owning
run, so any message on it by construction belongs to the current run.

## Use cases to test {#use-cases}

| # | Action | Expected result |
|---|---|---|
| 1 | Push a new commit while CI is running | Old run cancels (top-level check = `cancelled`); new run starts |
| 2 | Push two commits in quick succession | Both old runs cancel; only the latest SHA runs to completion |
| 3 | Click Cancel button on the `PR` check | That specific run cancels; no new run started |
| 4 | Click Cancel on a run that already finished | Lambda sees `NonExistentQueue` (queue was deleted on exit) and logs `[skip]`; no effect |
| 5 | Click Re-run all checks | Full workflow restarts for the same SHA; no self-cancel |
| 6 | Click Re-run on a specific failed check | Full workflow restarts; new run on the same SHA with a fresh queue |
| 7 | Two re-runs in quick succession | Each run uses its own queue; no cross-run traffic |
| 8 | Config Workflow succeeds with filtered jobs | `Skipped Jobs` check posted with Markdown breakdown grouped by reason |
| 9 | Config Workflow fails | All downstream jobs skipped; top-level check = `failure` |
| 10 | Style check runs inside Docker | `docker run` succeeds; per-job check flips `queued` → `in_progress` → `success/failure` |
| 11 | Runner instance is terminated mid-job | Visibility timeout expires; runner re-queues task; another runner picks it up |
| 12 | Orchestrator instance is terminated mid-run | SQS visibility timeout expires; other orchestrator re-processes workflow event. The old run's queue is leaked (see Limitations). |
