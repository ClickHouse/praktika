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
  itself (ClickHouse cluster + table schema) is provisioned and managed
  outside of praktika; praktika just writes to whatever endpoint
  `Settings.SECRET_CI_DB_URL` points at.

## Roadmap

Short-term:

* **Runner pool autoscaling** — Lambda watching SQS queue depth to scale
   runner pools up/down on demand
* **CI DB** — provisioning and configuration for an analytics database to
   stream every job/test result into

* **Private-access gateway (VPN)** — for reaching the HTML report page and
   the CI DB when those run on private (non-public) endpoints. Optionally
   also gives developers SSH access to runner instances for debugging.
* **Job cancel / job rerun** — cancel an in-flight job from the GitHub UI;
   re-run a single failed job without re-running the whole workflow
* **`praktika init`** — scaffold a new project with a starter
   `ci/workflows/` and `ci/infra/cloud.py` so adopters do not have to copy
   them by hand
* **`schedule` and `workflow_dispatch` workflows** — cron-driven and
   manually-triggered pipelines on the standalone engine
