# praktika

Build production-grade CI infrastructure on top of **GitHub** and a public cloud
provider (**AWS**) — pipelines and infrastructure both declared in plain Python
and deployed with one command.

praktika gives you:

- **Declarative pipelines.** Jobs, dependencies, artifacts, parametrized runs,
  caching, secrets, and reports are all expressed as plain Python objects in
  `ci/workflows/*.py`. Errors in the pipeline config surface at generation
  time, not in a half-finished CI run.
- **Declarative infrastructure.** RunnerPools, an Orchestrator pool, S3 buckets
  for artifacts/reports, SQS queues for sync, SSM/Secrets Manager bindings —
  all defined in a single `ci/infra/cloud.py` and brought up with
  `python -m praktika infrastructure --deploy`.
- **Two execution engines, same pipeline.** Run pipelines on GitHub Actions
  (praktika emits the `.github/workflows/*.yml` for you) or on the standalone
  engine on EC2 (the orchestrator polls SQS, dispatches jobs to runner pools,
  and patches GitHub Check runs over the Checks API).

## How to start

See [GETTING_STARTED.md](./GETTING_STARTED.md) — it walks through creating the
GitHub App, publishing the praktika package, deploying the AWS infrastructure
in one command, and wiring up the GitHub webhook.

## Module references

- [`praktika/infrastructure/`](./praktika/infrastructure/README.md) — config
  components for declaring AWS infrastructure (`VPC`, `Storage`,
  `RunnerPool`, `OrchestratorPool`, `report_page_config`, ...) and the
  `praktika infrastructure --deploy / --shutdown / --restart-instances`
  commands.
- [`praktika/orchestrator/`](./praktika/orchestrator/README.md) — the
  standalone CI engine: webhook receiver, workflow agent, job agent, the
  task-shape contract over SQS, and how to run a workflow or a single job
  locally without AWS.

## What's supported today

**GitHub side**
- `pull_request` and `push` workflows
- Status reporting via the GitHub Checks API
- HTML CI report page (per-workflow, per-job, per-test breakdown)
- GitHub App auth (App ID + installation ID + private key in AWS Secrets Manager)

**Cloud side (AWS)**
- Runner pools (Auto Scaling Group + Launch Template + EC2 Linux VMs)
- Orchestrator pool (also ASG-managed)
- SQS queues for workflow trigger, job dispatch, and per-job completions
- S3 buckets for artifacts and the HTML report
- SSM Parameter Store and Secrets Manager bindings for workflow secrets
- API Gateway + Lambda webhook receiver to ingest GitHub events
- CI DB integration — every job/test result streamed for analytics. The CI DB
  itself (single-node OSS ClickHouse + embedded Keeper) is now a native
  component (`NativeComponents.CIDBCluster`), or you can point praktika at an
  existing endpoint via `Settings.SECRET_CI_DB_CONNECTION`.

## Roadmap

**Execution engine**

- **Runner pool autoscaling** — Lambda watching SQS queue depth to scale
  runner pools up/down on demand
- **Job cancel / job rerun** — cancel an in-flight job from the GitHub UI;
  re-run a single failed job without rerunning the whole workflow
- **`schedule` and `workflow_dispatch` workflows** — cron-driven and
  manually-triggered pipelines on the standalone engine
- **Config and Finish stages on the orchestrator** — run the auto-injected
  setup/teardown jobs in-process instead of consuming a runner slot
- **Centralized event routing** — have MainCI walk every workflow's active
  triggers (events, branch filters, cron schedules) and publish a routing
  table the webhook lambda consumes, so the lambda knows which branches to
  accept, which schedules to fire, and which events to drop without each
  workflow encoding that in the lambda by hand
- **Optional Result object** — let a job finish without dumping a `Result`
  and have praktika synthesize one from the script's exit code (0 → OK,
  non-zero → FAILURE) instead of stamping `KILLED`. Gated by a new flag on
  the workflow config so the strict default is preserved for jobs that
  *should* always produce a Result. Today, jobs like *Yaml Lint* that don't
  write a Result fail with `ERROR: Job killed or terminated, no Result
  provided`
- **S3-based job liveness** — colocate cancel flag, heartbeat, and final
  state under a single per-job S3 prefix (`runs/<run_id>/<job>/`):
  job agent posts a 30 s heartbeat (`{ts, status, step}`); orchestrator
  sweeps RUNNING jobs and marks any without a heartbeat in 90 s as
  `failure ("runner died")`, with a longer pickup grace covering
  never-started jobs (empty pool, agent crash before first heartbeat).
  Job writes its final state (RC + env snapshot) to the same prefix on
  exit, which lets the per-run completions SQS queue be retired in a
  follow-up. Restart-safe orchestrator (state is durable in S3),
  symmetric with the existing cancel flag

---DONE. VERIFY --- 
- **Workflow cancellation** — orchestrator must handle cancel signals while
  blocked in `wait()`: a runner that never picks up a job leaves the
  orchestrator stuck indefinitely, and a cancel that arrives (e.g. from a
  new push) is only processed after the blocked call returns; the fix
  requires either a timeout + cancel-queue poll loop inside `wait()`, or
  running cancel handling on a separate thread/task
- **GitHub App token refresh** — the orchestrator acquires a GH App token
  at startup and reuses it for the lifetime of the workflow; tokens expire
  after ~1 hour, so long-running workflows (or workflows that stall in
  `wait()`) start getting 401s on every check-run PATCH, leaving all
  check statuses stuck; the token must be re-acquired before each GitHub
  API call (or cached with an expiry check)

**Observability**
- **Log export for orchestrator and runners** — live tail and persisted
  archive, accessible without SSM
- **CI DB provisioning** — bring the ClickHouse cluster and schema under
  praktika-managed infrastructure (today only the writer side ships with
  praktika; the cluster is provisioned out-of-band)

**Networking**
- **Private-access gateway (VPN)** — reach the HTML report and CI DB when
  those run on private endpoints; optionally also SSH to runner instances
  for debugging

**Project ergonomics**
- **`praktika init`** — scaffold a new project with a starter
  `ci/workflows/` and `ci/infra/cloud.py` so adopters do not have to copy
  them by hand
- **Versioned releases** — pinned, semver-tagged praktika packages with a
  documented upgrade path between versions
