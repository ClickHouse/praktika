# Cron / scheduled workflows on the praktika engine

**Goal.** Run `SCHEDULE`-event workflows (the ~30 `nightly_*` / `hourly` /
`weekly_*` / `private_nightly_*` workflows in `ci/workflows/`) through the native
CI engine instead of GitHub Actions. Today every one of them sets
`engine=Workflow.Engine.GH_ACTIONS` and its cron is realized by generated GitHub
Actions YAML (`on: schedule: cron`, `yaml_generator.py:82`). We want a mechanism
that fires each workflow at its cron time by writing a trigger message into the
orchestrator SQS, exactly like the webhook path does for PR/push.

## What already exists (do not reinvent)

- **Trigger lambda** `ci/praktika/infrastructure/native/lambda_gh_trigger.py` —
  HMAC-verifies GitHub webhooks and `send_message`s a JSON trigger into the
  `workflow-orchestrator` SQS. Message shapes today: `{"type":"pull_request",...}`
  and `{"type":"push","head_ref","head_sha","repo","sender","event_ts"}`
  (`_build_push_workflow`, ~line 406). It already holds GitHub App auth
  (`_get_github_token`, `_gh_api`) and already does write-once S3
  (`IfNoneMatch="*"`, line 244).
- **Orchestrator** `ci/praktika/orchestrator/__init__.py` —
  `find_workflows_for_event` maps SQS `type` -> `Workflow.Event` via `_EVENT_MAP`
  (currently only `pull_request`, `push`), matches the event branch against
  `wf.base_branches` / `wf.branches`, **skips `engine == GH_ACTIONS`**, then
  builds the DAG and dispatches jobs to `praktika-<runs_on>` queues. It already
  supports a `workflow_name=` filter.
- **Workflows already model cron.** `Workflow.Config` has
  `event=Workflow.Event.SCHEDULE` and `cron_schedules=[...]`, validated in
  `validator.py` (`is_valid_cron_field`, 5-field POSIX cron). Example:
  `ci/workflows/nightly_sqlancer.py` (`cron_schedules=["13 6 */3 * *"]`).
- **Scheduled-lambda plumbing exists.** `Lambda.Config.schedule_expression`
  (`lambda_function.py:39`) wires an EventBridge rule -> lambda
  (`put_rule` / `put_targets` / `add_permission`, ~lines 578-619). `pool_autoscaler`
  already deploys a `rate(...)`-triggered lambda this way — direct precedent.
- **`_environment.py:152`** already maps a schedule run to a `SCHEDULE`
  environment, and **`job_runner.py:142`** already handles the non-PR ref case
  (`pr_number=0`, ref lives in the base repo).

So the whole "trigger message -> SQS -> orchestrator -> jobs" path exists. What's
missing is (1) a `schedule` event type end-to-end, and (2) something that fires it
on time and learns the schedules defined on the base branch.

## Two sub-problems

1. **Timing** — fire at each workflow's cron minute.
2. **Discovery** — when a new `SCHEDULE` workflow is merged to the base branch,
   the firing mechanism must learn its cron with no human redeploy.

## Chosen approach: minute lambda + S3 manifest

A single lambda on `rate(1 minute)` reads a small cron manifest from S3, matches
each cron against the current UTC minute, and enqueues a `schedule` message for
each match. Discovery is "rewrite the manifest whenever the base branch changes."

Decisions already made:
- **Firing mechanism: single `rate(1 minute)` lambda + S3 manifest** (not one
  EventBridge rule per cron). Fewer AWS objects; the cost is that we own
  cron-matching + dedup — acceptable because dedup is a one-liner here (see below).
- **Reconciler runs in BOTH places:** a post-merge master job (automatic) AND
  the `praktika infrastructure --deploy` path (manual fallback). Both call the
  same builder and write the identical artifact, so they can't diverge.
- **`engine` is the single migration switch.** `yaml_generator.generate` skips
  any workflow whose engine `!= GH_ACTIONS` (line 285); the manifest builder
  includes only `engine != GH_ACTIONS`. So a workflow is either GH-cron (YAML
  emits it, manifest excludes it) or native-cron (YAML skips it, manifest
  includes it) — never both, no double-fire.
- **Dedup = write-once S3** (`PutObject(..., IfNoneMatch="*")`), the same
  primitive already used at `s3.py:390`, `native_jobs.py:271`, and
  `lambda_gh_trigger.py:244`. No DynamoDB.

### 1. Manifest (discovery)

`s3://<S3_ARTIFACT_BUCKET>/ci-engine/cron_manifest.json`:

```json
{
  "generated_at": "2026-09-18T20:00:00Z",
  "repo": "ClickHouse/clickhouse-private",
  "schedules": [
    {"workflow_name": "NightlySQLancer", "branch": "<BASE_BRANCH>", "cron": "13 6 */3 * *"},
    {"workflow_name": "Nightly",         "branch": "<BASE_BRANCH>", "cron": "23 2 * * *"}
  ]
}
```

Built by a shared `build_cron_manifest()` that enumerates workflows via the
existing `_get_workflows()` and includes only `event == SCHEDULE and
engine != GH_ACTIONS`, expanding each `cron_schedules` entry into one row.

### 2. Reconciler (both triggers)

Same `build_cron_manifest()` + S3 upload, called from:
- **Post-merge master job** — a small job in `ci/workflows/private_master.py` /
  `ci/workflows/master.py` (both already `Event.PUSH` on the base branch).
  Merging a nightly -> master run -> manifest rewritten -> picked up within a
  minute. Fully automatic.
- **`praktika infrastructure --deploy`** — call the same builder so a manual
  deploy also reconciles.

Idempotent; both write the identical artifact.

### 3. Minute lambda (`native/lambda_cron_dispatcher.py`)

Deployed with `Lambda.Config(schedule_expression="rate(1 minute)")` (same wiring
as `pool_autoscaler`). Each invocation:

1. `GetObject` the manifest (one cheap S3 read).
2. Compute `now` in **UTC**, quantized to the minute. To absorb cold-start/skew,
   evaluate the small window of minutes since a stored `last_run_minute` marker
   (bounded, e.g. <=10 min) rather than only the current minute — so a delayed
   invocation doesn't silently skip a fire.
3. For each `(schedule, minute)` pair, match the 5-field cron (`*`, `,`, `-`,
   `*/step`) — reuse the field-parse logic from `validator.py::is_valid_cron_field`.
4. **Dedup = exactly-once** via write-once S3:
   `PutObject(Key="ci-engine/cron-fired/<workflow>/<minute_key>", IfNoneMatch="*")`.
   Success -> first, proceed; `PreconditionFailed` -> a duplicate/overlapping
   invocation already fired it, skip. Put a short S3 lifecycle expiry on that prefix.
5. Resolve the branch's current HEAD sha via the GitHub API (reuse
   `_get_github_token` / `_gh_api` from the trigger lambda — factor them into a
   shared module both lambdas import). Cron has no sha, unlike a webhook.
6. `send_message` into the orchestrator SQS:

```json
{"type":"schedule","workflow_name":"NightlySQLancer","repo":"...",
 "head_ref":"<branch>","head_sha":"<resolved>","event_ts":<ts>,"cron":"13 6 */3 * *"}
```

### 4. Orchestrator changes (small)

In `orchestrator/__init__.py`:
- `_EVENT_MAP["schedule"] = Workflow.Event.SCHEDULE`.
- In `find_workflows_for_event`, for `type == "schedule"` set
  `branch = event.get("head_ref")` and match against `wf.branches` (same as push).
- Name-filtering already works: pass the message's `workflow_name` so a cron
  message runs exactly the one named workflow — no accidental fan-out.

Everything downstream (`build_job_dag`, `WorkflowState`, SQS dispatch to
`praktika-<runs_on>`) is unchanged.

### 5. Migration path

Per workflow, one line: `engine=GH_ACTIONS` -> native engine, then
`PYTHONPATH=./ci:. python3 -m praktika yaml` and merge. On merge: YAML drops the
GitHub `schedule`, the reconciler adds it to the manifest, the minute lambda
starts firing it. No window where both fire. Start with one low-stakes nightly
(`Nightly` or `NightlySQLancer`), validate the SQS message + orchestrator run,
then convert the rest.

## Build order

1. `build_cron_manifest()` + S3 upload helper (shared module).
2. Orchestrator `schedule` event support (3-line change) — testable locally:
   `praktika orchestrate workflow event.json --name NightlySQLancer` with a
   hand-written schedule event.
3. `native/lambda_cron_dispatcher.py` + `Lambda.Config(schedule_expression=
   "rate(1 minute)")` in `native/configs.py`; factor GH-token / `_gh_api` helpers
   out of `lambda_gh_trigger.py` for reuse.
4. Reconciler job in `master.py` / `private_master.py`, and the deploy-path call.
5. Flip one workflow, validate end-to-end.

## Caveat

`rate(1 minute)` EventBridge is at-least-once and unaligned — it can fire twice
in a minute or drift a few seconds off the boundary. The write-once S3 marker
(step 4) is what makes the pipeline exactly-once regardless; **do not skip it.**

## Rejected alternative: one EventBridge rule per cron

Create one EB rule per `cron_schedules` entry (`ScheduleExpression=cron(...)`)
with a constant Input targeting a thin dispatcher lambda; reconcile the rule set
on merge. Pro: EventBridge owns timing (no cron-matching / dedup / catch-up code
to write); mirrors GH Actions and `pool_autoscaler`. Con: more AWS objects and a
rule-reconcile step (`put_rule` / `delete_rule`). Not chosen — we preferred a
single manifest + single lambda.

## Status

Open — design only. Nothing implemented. `_EVENT_MAP` has no `schedule` entry;
all `SCHEDULE` workflows still run under `engine=GH_ACTIONS`.
