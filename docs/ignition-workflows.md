# Cron & dispatch workflows via GitHub Actions ignition

`Workflow.Engine.GH_IGNITION` runs `SCHEDULE` (cron) and `DISPATCH` (manual)
workflows on the native Praktika engine while letting GitHub own the timing and
the manual "Run workflow" button.

## How it works

1. `praktika yaml` generates a thin GitHub Actions workflow (`on: schedule` +
   `on: workflow_dispatch`). Its only job signs a small trigger with
   `GH_WEBHOOK_SECRET` and POSTs it to the gh-trigger lambda
   (`PRAKTIKA_TRIGGER_URL`, header `X-GitHub-Event: praktika_ignition`).
2. The lambda verifies the HMAC + repo allow-list and enqueues a
   `{"type": "schedule"|"dispatch", "workflow_name", "repo", "head_ref",
   "head_sha", "inputs"}` message onto the orchestrator queue.
3. The orchestrator matches by `workflow_name` and runs the real jobs natively.

The enqueued `type` is the workflow's own event, so a schedule workflow's manual
button means "run the scheduled workflow now".

## Defining one

```python
Workflow.Config(
    name="Nightly Style",
    event=Workflow.Event.SCHEDULE,          # or DISPATCH
    engine=Workflow.Engine.GH_IGNITION,
    cron_schedules=["0 * * * *"],           # SCHEDULE only
    branches=["main"],                       # optional; empty = any ref
    inputs=[...],                            # DISPATCH inputs, read via
                                             # Info.get_workflow_input_value(name)
    jobs=[...],
)
```

- `branches` is an optional ref restriction: empty means the native run fires on
  whatever ref the cron/dispatch used (any ref chosen in the GitHub UI).
- Only the default orchestrator pool is supported (the lambda enqueues to one
  queue); a non-default `orchestrator_filter` is rejected at validation.

## Setup

- Repo secret `GH_WEBHOOK_SECRET` (the App's webhook secret) and repo variable
  `PRAKTIKA_TRIGGER_URL` (the gh-trigger lambda API Gateway URL).
- The workflow must be on the default branch for GitHub to schedule it / show the
  button.

See the design notes in `proposals/cron_scheduled_workflows.md`.
