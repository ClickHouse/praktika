# praktika

praktika is a self-contained CI system that you configure and deploy on top of
a cloud provider. Pipelines and infrastructure are both declared in plain
Python and deployed with one command.

praktika gives you:

- **Declarative pipelines.** Jobs, dependencies, artifacts, parametrized runs,
  caching, secrets, and reports are all expressed as plain Python objects in
  `ci/workflows/*.py`. Errors in the pipeline config surface at generation
  time, not in a half-finished CI run.
- **Declarative infrastructure.** RunnerPools, an Orchestrator pool, S3 buckets
  for artifacts/reports, SQS queues for sync, SSM/Secrets Manager bindings —
  all defined in a single `ci/infrastructure/projects.py` and brought up with
  `python -m praktika infrastructure --deploy`.
- **Standalone-first execution model.** Praktika is designed to run its own CI
  control plane on cloud infrastructure: the orchestrator polls workflow
  queues, dispatches jobs to runner pools, and reports status back to the Git
  hosting system. GitHub Actions YAML generation is available as a compatibility
  option, but it is not the primary model.

Today, Praktika is developed around GitHub and AWS. The overall model is not
conceptually limited to them, but those are the integrations implemented today.

## How to start

See [GETTING_STARTED.md](./GETTING_STARTED.md) — it walks you through
infrastructure configuration and deployment, and writing your first Praktika
pipeline.

## Module references

- [`praktika/infrastructure/`](./praktika/infrastructure/README.md) — config
  components for declaring AWS infrastructure (`VPC`, `Storage`,
  `RunnerPool`, `OrchestratorPool`, `report_page_config`, ...) and the
  `praktika infrastructure --deploy / --destroy-runtime / --destroy-all / --restart-instances`
  commands.
- [`praktika/orchestrator/`](./praktika/orchestrator/README.md) — the
  standalone CI engine: webhook receiver, workflow agent, job agent, the
  task-shape contract over SQS, and how to run a workflow or a single job
  locally without AWS.

## What's supported today

**Native Praktika features**
- Declarative jobs and pipelines in plain Python
- Declarative CI infrastructure configuration and deployment
- Built-in CI cache with awareness of successful jobs and reusable artifacts
- Versioned runtimes via named base virtual environments, with `praktika_bootstrap` selecting the workflow or job runtime and optionally overlaying Praktika from checked-out source
- HTML CI report page with per-workflow, per-job, and per-test drill-down
- Consistent test Docker image versioning: images rebuild automatically when inputs change, and versions stay pinned to code state across branches
- CI DB integration: job results, test results, timings, and related metadata are written automatically

**GitHub side**
- Webhook ingestion via AWS Lambda
- `pull_request` and `push` pipeline triggers
- Status reporting through the GitHub Checks API
- GitHub App authentication through a token-broker Lambda

**Cloud side (AWS)**
- Runner pools based on Auto Scaling Groups, Launch Templates, and EC2 Linux VMs
- An orchestrator pool managed the same way
- Queue-driven autoscaling for runner and orchestrator pools: a scheduled Lambda scales pools up from SQS backlog, and auto-scaled instances scale themselves back in when their queue is idle
- Image Builder pipelines for baking named base runtime environments into AMIs
- SQS queues for workflow triggers and job dispatch
- S3 buckets for artifacts and the HTML report
- SSM Parameter Store and Secrets Manager bindings for workflow secrets
- API Gateway plus Lambda webhook receiver for inbound Git events
- CI DB integration for analytics: every job and test result can be streamed to a CI DB, and Praktika can also provision its own native CI DB component (`Components.CIDBCluster`) or use an existing endpoint via `Settings.SECRET_CI_DB_CONNECTION`

## Known limitations

- Only one workflow is processed at a time per trigger type (`pull_request`,
  `push`). If multiple workflows match the same trigger, the current
  orchestrator processes them sequentially in the same orchestrator run rather
  than in parallel.

## Roadmap
**Blockers**
- Approve and Run alternative for forks in OSS

**Execution engine**
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

**Report / UX**
- **Pre/post-hook results in report** — move hook sub-results out of
  `Result.results` into `Result.ext.pre_hook_result` / `Result.ext.post_hook_result`
  so they are hidden by default and revealed only when the user clicks something like
  "show infra results"; if any hook result is failed the report page must still surface
  a Warning note regardless of the toggle state

**Project ergonomics**
- **`praktika init`** — scaffold a new project with a starter
  `ci/workflows/` and `ci/infrastructure/projects.py` so adopters do not have to copy
  them by hand
