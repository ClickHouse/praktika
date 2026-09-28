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

## Alternative (additive): GitHub Actions as an ignition

A simpler design that **coexists** with the lambda approach above as a second,
per-workflow-selectable option — not a replacement. Keep GitHub as the source of
timing *and* the manual-trigger UI, but stop doing real work there. `praktika`
generates a **minimal "ignition" workflow** whose only job enqueues a trigger
into the same orchestrator SQS the webhook path already uses; the real DAG then
runs on the native engine. This buys the one thing the lambda approach can't give
at all — a working **"Run workflow" button in the GitHub Actions UI** — while
deleting the lambda / manifest / reconciler / cron-matching / dedup machinery,
because GitHub owns the schedule.

### The switch: a third `engine` value

Today `engine` has two states and each half of the pipeline keys off
`== GH_ACTIONS`:
- `yaml_generator.generate` emits YAML only for `GH_ACTIONS`, **skips** the rest
  (`yaml_generator.py:285`).
- the orchestrator runs everything **except** `GH_ACTIONS`
  (`orchestrator/__init__.py:62`).

Ignition is a genuine third mode — *emit a minimal YAML **and** run natively* — so
add `Workflow.Engine.GH_IGNITION` (`workflow.py:18`). It slots in with no change
to the two existing guards' intent:
- The orchestrator guard is `== GH_ACTIONS`, so `GH_IGNITION` already **passes**
  it and runs natively. No change needed there.
- The yaml_generator guard (`!= GH_ACTIONS → skip`) must gain a branch:
  `GH_IGNITION → emit the ignition template` (a new small `TEMPLATE_IGNITION`,
  reusing the existing `cron_schedules` / `dispatch_inputs` rendering already in
  `PullRequestPushYamlGen.generate`, lines 441-472).

So a workflow is exactly one of three: GH-native (full YAML, orchestrator skips),
native (no YAML, orchestrator runs, fired by the minute-lambda above), or
ignition (thin YAML fires it, orchestrator runs). No double-fire.

### The ignition workflow (generated YAML)

`on: schedule: <cron_schedules>` + `on: workflow_dispatch: <inputs>`, one job on
**`ubuntu-latest`** (GitHub-hosted — a scheduled fire then does not depend on the
native pool being scaled up). Two steps:

1. **Enqueue.** HMAC-sign a small JSON body with the existing webhook secret and
   `POST` it to the trigger lambda's API Gateway URL
   (`configs.py` `lambda_gh_trigger_config`, `api_gateway=True`). Body carries
   everything the orchestrator needs, taken straight from the Actions context:
   `{"type":"schedule"|"dispatch","workflow_name":"<name>","repo":"<owner/repo>",
   "head_ref":"<branch>","head_sha":"${{ github.sha }}","inputs":{...}}`.
   Needs two repo-level values: secret `GH_WEBHOOK_SECRET` (to sign — the same
   secret the App already uses, mapped from SSM at `configs.py`) and the lambda
   URL as a repo var.
2. **Annotate.** Emit the deterministic report link as a run annotation +
   step summary (`echo "::notice title=Praktika::<url>"` and `$GITHUB_STEP_SUMMARY`).
   The URL is built with the existing
   `Info.get_specific_report_url_static(pr_number=0, branch, sha, "", workflow_name)`
   (`info.py:259`) — no new plumbing.

**Advantage over the lambda approach:** the sha is already known
(`github.sha` = the ignition commit), so unlike `lambda_cron_dispatcher.py` step 5
there is **no GitHub-API HEAD resolution**. The body is self-contained.

### Lambda change (one new branch)

`lambda_handler` (`lambda_gh_trigger.py:1242`) dispatches on `X-GitHub-Event`;
today it handles `check_run` / `check_suite` / `push` / `pull_request` and does
**not** handle `workflow_dispatch` (and GitHub sends no `schedule` webhook at
all). Add a branch keyed on a custom header the ignition step sets, e.g.
`X-GitHub-Event: praktika_ignition`. It reuses the existing HMAC verify
(`verify_github_signature`, line 1244) and repo allow-list (line 1269), then
builds `{type: body["type"], workflow_name, repo, head_ref, head_sha, event_ts,
inputs}` and calls the existing `_enqueue` (line 758). No GH-token / `_gh_api`
use — the body is complete.

Trust boundary is unchanged: the same webhook secret already gates the path, and
even a forged enqueue can only start a workflow that is *registered* as
`GH_IGNITION` with a matching branch — the orchestrator's `workflow_name` +
engine + branch filters (§4) bound it.

### Orchestrator change (shared with the lambda approach)

Identical to §4: `_EVENT_MAP["schedule"]` / `_EVENT_MAP["dispatch"]`, set
`branch = event.get("head_ref")` and match `wf.branches`, and rely on the
existing `workflow_name` filter so a fire runs exactly the one named workflow.
This layer is unaware of which mechanism (minute-lambda vs ignition) produced the
message — both proposals share it.

### Trade-offs vs. the lambda + manifest approach

Pro:
- No new lambda, no S3 cron manifest, no reconciler job, no cron-matching /
  catch-up / dedup code. GitHub owns timing; discovery is automatic (the cron
  lives in committed YAML — no manifest to keep in sync).
- **GitHub UI dispatch works** (`workflow_dispatch` button), which the lambda
  approach does not provide.
- Sha is known up front (no API resolution).

Con:
- Still depends on GitHub Actions for firing. GitHub's `schedule` cron is
  delayed/dropped under load — the same unreliability the minute-lambda was
  partly meant to escape. (Manual `workflow_dispatch` is unaffected.)
- Consumes a small amount of GH Actions minutes per fire (one short job).
- Needs `GH_WEBHOOK_SECRET` exposed as a repo secret (trust boundary unchanged,
  but one more place the secret lives).
- Dedup: GitHub effectively fires a scheduled run once, so exactly-once is not
  required the way `rate(1 minute)` needs it; a re-fire of the same `(workflow,
  sha)` is a cache hit / harmless rerun. Add a write-once idempotency key only if
  a stricter guarantee is wanted.

### Build order

1. Add `Workflow.Engine.GH_IGNITION` + the `yaml_generator` branch emitting the
   ignition template (reuse the existing cron / dispatch-input rendering).
2. Orchestrator `schedule` / `dispatch` event support (shared change — see §4).
3. Lambda `praktika_ignition` branch → `_enqueue`.
4. Deploy: publish `GH_WEBHOOK_SECRET` + the lambda URL to repo secret/var; flip
   one low-stakes workflow to `GH_IGNITION`, validate the SQS message +
   orchestrator run + the annotated report link, then convert the rest.

## Status

Two designs are on the table and can coexist per-workflow: the **minute-lambda +
manifest** (GH-free timing, no UI) and the **GH-ignition engine** (GH-driven
timing + UI button, minimal machinery).

- **Minute-lambda + manifest** — design only, nothing implemented.
- **GH-ignition engine** — **implemented** (opt-in via `engine=GH_IGNITION`; no
  behavior change until a workflow sets it):
  - `Workflow.Engine.GH_IGNITION` added (`workflow.py`), accepted by
    `validator.py`.
  - `yaml_generator.py` emits the thin ignition YAML (`TEMPLATE_IGNITION` +
    `IgnitionYamlGen`); the `schedule:` trigger is omitted for dispatch-only
    workflows.
  - `orchestrator/__init__.py` maps `schedule` / `dispatch` SQS types, matches
    `head_ref` against `wf.branches`, and honors the message's `workflow_name`
    (so the out-of-repo SQS consumer needs no change).
  - `lambda_gh_trigger.py` handles `X-GitHub-Event: praktika_ignition` — verifies
    the existing HMAC + repo allow-list, then `_enqueue`s the trigger.
  - Tests: `test_ignition_yaml.py`, plus schedule/dispatch cases in
    `test_workflow_routing.py` and `test_lambda_gh_trigger.py`.
  - **Deploy prerequisite (not code):** publish repo secret `GH_WEBHOOK_SECRET`
    and repo var `PRAKTIKA_TRIGGER_URL` (the gh-trigger lambda's API Gateway URL)
    so the ignition job can sign and POST. Then flip one low-stakes workflow to
    `GH_IGNITION` and validate end-to-end.
