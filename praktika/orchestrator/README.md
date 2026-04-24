# CI Engine

Standalone CI engine that replaces GitHub Actions scheduling with direct
webhook-driven workflow orchestration + SQS-based job dispatch to pools of
long-lived EC2 runners.

## Architecture

```
GitHub webhook
    |
    v  (HMAC-verified PR event)
Lambda (ci/praktika/infrastructure/native/lambda_ci_engine.py)
    |
    v  (enqueues {type, repo, pr_number, head_sha, ...})
SQS praktika_clickhouse_workflows
    |
    v
Orchestrator ASG (praktika-workflow-orchestrator-asg)
    user_data -> run.py -> orchestrate()
        |-- open per-workflow GitHub check run (`PR`, in_progress)
        |-- find_workflow_for_event  -- match event + branch to Workflow.Config
        |-- build_job_dag            -- resolve requires/run_after into topo levels
        |-- WorkflowState + execution loop
        |       for each JobState kicked:
        |         open per-job GitHub check run (queued -> in_progress)
        |         if runs_on contains a "praktika-*" label:
        |           send {type: "job_task", ...} to SQS queue <label>
        |         else (stub): wait() completes as success immediately
        |-- close per-workflow check (neutral, plan text in output.text)
    |
    v  (SQS: one queue per runner type, named after runs_on label)
Runner ASGs (e.g. praktika-arm-2xsmall)
    user_data -> run_job.py -> job_runner.run_job()
        |-- look up praktika Workflow + Job from the task
        |-- Runner().run(workflow=wf, job=job, local_run=False, run_hooks=True, ...)
```

## Code split (intentional)

The orchestrator and runner are each composed of two pieces — one stable
"runner script" that's baked into the EC2 user_data, and one orchestrator
module that ships with each PR:

|                   | Baked into user_data (needs LT+ASG redeploy to change) | Ships with each PR (plain `git push`) |
|-------------------|--------------------------------------------------------|---------------------------------------|
| **Orchestrator**  | `run.py` — SQS poll, clone, GH App token, `CheckRun` HTTP, S3 log | `__init__.py::orchestrate`, `state.py` (`WorkflowState`, `JobState`, `JobCheckRun`) |
| **Job runner**    | `run_job.py` — SQS poll, clone, GH App token, S3 log   | `job_runner.py::run_job` (maps task -> `praktika.Runner.run`) |

When you want to tweak workflow orchestration or job-execution policy, you
only need `git push`. Only the stable scripts require an LT/ASG redeploy.

## Debugging runner logs

`fetch_job_log.sh` fetches the full `ci-runner` systemd journal from a live
runner via SSM and extracts the log for a specific job run. It uploads the
journal to S3 first to bypass the 48 KB SSM output limit.

```bash
# List all job names seen in the current runner's journal
./ci/praktika/orchestrator/fetch_job_log.sh --list

# Fetch the most recent run of a specific job
./ci/praktika/orchestrator/fetch_job_log.sh -j "Config Workflow"

# Fetch the second-most-recent run
./ci/praktika/orchestrator/fetch_job_log.sh -j "Config Workflow" -n 2

# Target a specific runner instance
./ci/praktika/orchestrator/fetch_job_log.sh -j "Config Workflow" -i i-0abc123

# Save to a file
./ci/praktika/orchestrator/fetch_job_log.sh -j "Config Workflow" > /tmp/cw.log
```

Auto-detects the running `praktika-arm-2xsmall` runner if no `-i` is given.
Requires AWS SSM access and S3 write permission to `clickhouse-test-reports-private`.

## Local testing

### Orchestrator (no AWS required)

```bash
python3 ci/praktika/orchestrator/run.py --local tmp/sandbox/test_message.json
```

Loads workflow configs, matches the event to a workflow, builds the DAG, walks
the execution loop (currently stubs job kick + wait), prints the plan + START
/ DONE / summary. Posts real GitHub check runs only if `CI_ENGINE_POST_CHECKS=1`
and AWS creds are available.

### Job runner (single-job sandbox)

```bash
python3 ci/praktika/orchestrator/run_job.py --local tmp/sandbox/style_check_task.json
```

Invokes `praktika.Runner.run` with `local_run=True` for the job named in the
task. The real EC2 runner uses `local_run=False` + `run_hooks=True`.

Task JSON shape (what the orchestrator sends over SQS):

```json
{
  "type": "job_task",
  "repo": "ClickHouse/clickhouse-private",
  "pr_number": 55743,
  "head_sha": "...",
  "head_ref": "ci-engine",
  "workflow_name": "PR",
  "job_name": "Style check",
  "runs_on": ["self-hosted", "praktika-arm-2xsmall"]
}
```

## Naming conventions

Adding a new runner type means one call to `_runner_infra(name, instance_type)`
in `ci/infra/cloud.py`. Names derive from a single base:

| Resource | Name |
|----------|------|
| ASG      | `praktika-{name}` |
| LT       | `praktika-{name}-lt` |
| SQS queue | `praktika-{name}` (matches the `runs_on` label 1:1) |

A job's `runs_on` list is checked for any label starting with `praktika-`; the
first match is treated as the SQS queue name. Labels that don't match
(e.g. `self-hosted`, legacy `private-*`) fall through to the no-op stub so
partially-migrated workflows keep flowing.

## Deploy

```bash
# Orchestrator infra (changes to run.py / user_data_ci_engine.sh):
python3 -m ci.praktika infrastructure --deploy --only LaunchTemplate AutoScalingGroup
# Plus terminate running orchestrator runners so the ASG relaunches on the new LT.

# Runner infra (new runner types, or changes to run_job.py / user_data_ci_runner.sh):
python3 -m ci.praktika infrastructure --deploy --only LaunchTemplate AutoScalingGroup SQSQueue
# Plus terminate running runners on the affected pool.
```

The Praktika ASG deploy resolves `launch_template_version="$Latest"` to a
concrete version number at deploy time, so an LT bump also needs an ASG
redeploy. `$Latest` is not honored at runtime.

## What works

- Webhook -> Lambda -> SQS -> orchestrator pickup -> clone -> orchestrate
- Per-workflow `PR` GitHub check run with the full execution plan as output
- Per-job GitHub check runs (`queued` at plan time, `in_progress` on kick,
  `success`/`skipped` on completion)
- DAG-aware execution loop (`WorkflowState.get_ready` / `kick` / `wait`)
- SQS dispatch from `JobState.kick()` to per-type runner queues
- Job runner infra helper (`_runner_infra`), one deployed pool: `praktika-arm-2xsmall`
- Local sandbox: `run_job.py --local <task.json>` runs a real job end-to-end
  through `praktika.Runner.run`

## TODO

- Completion path: runner posts a "done" event back, orchestrator's `wait()`
  long-polls it and flips `JobState` to SUCCESS/FAILURE. Today the orchestrator
  stubs a job SUCCESS the instant it's dispatched.
- Run `Config Workflow` to filter jobs (file-change / cache-hit).
- Artifact flow via `s3://clickhouse-builds/PRs/<pr>/<workflow>/<job>/...`.
- Workspace cleanup between jobs on long-lived runners (`git clean -ffdx`,
  cache reset).
- Orphan runner sweeper Lambda (mandatory tags + periodic termination).
- Self-termination on job completion (EXIT trap in `Runner.run`).
- Per-runner-type scaling (queue depth -> ASG target tracking) — currently all
  pools are fixed size 1.
