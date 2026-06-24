# praktika

praktika is a self-contained CI system that you configure and deploy on top of
a cloud provider.
praktika gives you:

- **Declarative pipelines.** Jobs, dependencies, artifacts, parametrized runs,
  caching, secrets, and reports are all expressed as plain Python objects in
  `ci/workflows/*.py`. Errors in the pipeline config surface at generation
  time, not in a half-finished CI run.
- **Unified CI outcome model.** Workflows, jobs, sub-tasks, and tests all
  produce the same `Result` type. That single shape powers smooth HTML report
  visualization, telemetry, and consistent navigation for humans and AI
  agents.
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

For deployment security considerations, see [SECURITY.md](./SECURITY.md).

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
- Starter hello-world setup with `praktika init`, which scaffolds
  `ci/workflows/` and `ci/infrastructure/projects.py` for a new project
- Versioned runtimes via named base virtual environments, with `praktika_controller` selecting the workflow or job runtime and optionally overlaying Praktika from checked-out source
- HTML CI report page with per-workflow, per-job, and per-test drill-down
- Consistent test Docker image versioning: images rebuild automatically when inputs change, and versions stay pinned to code state across branches
- CI DB integration: job results, test results, timings, and related metadata are written automatically

**GitHub side**
- Webhook ingestion via AWS Lambda
- `pull_request` and `push` pipeline triggers
- Status reporting through the GitHub Checks API
- GitHub App authentication through a token-broker Lambda
- GitHub Pages publishing for hosted reports

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

## Roadmap
**Execution engine**
- **Approve and Run alternative for forks in OSS** — provide a standalone-engine
  flow for safely reviewing and explicitly allowing CI runs from forked pull
  requests
- **Job cancel / job rerun** — cancel an in-flight job from the GitHub UI;
  re-run a single failed job without rerunning the whole workflow
- **Dispatch and cron workflows** — support manually-triggered
  `workflow_dispatch` runs and scheduled `cron` / `schedule` pipelines on the
  standalone engine
- **Ephemeral merge-commit PR runs** — run pull-request CI against an
  ephemeral merge commit by default instead of the branch head, with an
  explicit opt-in mode for testing the raw head commit when needed. PRs with
  merge conflicts should not start CI runs until the conflicts are resolved.
  For future AI-edit sessions, prefer keeping parent 2 of the merge commit
  stable across automatic commits so Praktika can reuse CI cache state and
  avoid rerunning jobs that already passed earlier in the same session.
- **Config and Finish stages on the orchestrator** — run the auto-injected
  setup/teardown jobs in-process instead of consuming a runner slot
- **AI-assisted orchestrator loop** — allow the orchestrator to call a
  provider-agnostic AI decision hook at each execution-graph advance. The hook
  should receive the current workflow state and return constrained actions such
  as continuing normally, reordering runnable jobs within dependency and
  resource limits, canceling the workflow, or canceling and starting an
  automated fix attempt.
- **Centralized event routing** — have MainCI walk every workflow's active
  triggers (events, branch filters, cron schedules) and publish a routing
  table the webhook lambda consumes, so the lambda knows which branches to
  accept, which schedules to fire, and which events to drop without each
  workflow encoding that in the lambda by hand
- **Remove AWS CLI dependency from CI runtime** — Praktika runtime code should
  use boto3 APIs directly instead of shelling out to `aws`

**Infrastructure / deployment**
- **Incremental deploys with component hashes** — compute a stable hash for
  every deployable infrastructure component and persist the deployment
  manifest in Parameter Store together with the Praktika version used for that
  deploy. On redeploy, compare the current component hashes with the stored
  manifest, validate the Praktika version compatibility, and update only the
  components whose inputs changed so routine deploys complete faster.

**Observability**
- **Log export for orchestrator and runners** — live tail and persisted
  archive, accessible without SSM
- **Infra watch agent** — continuously monitor SQS queues, runner and
  orchestrator pools, Lambda functions, CI DB, and other managed services;
  log abnormal state, health regressions, and infrastructure problems for
  follow-up

**Networking**
- **Private-access gateway (VPN)** — reach the HTML report and CI DB when
  those run on private endpoints; optionally also SSH to runner instances
  for debugging
- **S3-backed Docker proxy** — create a native component that caches Docker
  image pulls in S3 so runners get fast, local pulls without registry rate
  limiting

**Report / UX**
- **Pre/post-hook results in report** — move hook sub-results out of
  `Result.results` into `Result.ext.pre_hook_result` / `Result.ext.post_hook_result`
  so they are hidden by default and revealed only when the user clicks something like
  "show infra results"; if any hook result is failed the report page must still surface
  a Warning note regardless of the toggle state
