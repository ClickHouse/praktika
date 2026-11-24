# praktika

**Praktika runs your CI on your own cloud account — typically 3–5× cheaper than
a managed CI, and deployed in one command.**

You describe the whole project — pipelines *and* the infrastructure to run them
— as plain Python, and Praktika brings it all up for you: runner pools, an
orchestrator, artifact/report storage, queues, and secret bindings. No clicking
through a console, no per-minute billing, no vendor lock-in.

```
python -m praktika infrastructure --deploy
```

## Why Praktika

- **Self-hosted, cost-efficient.** It's your cloud account and your instances
  (spot, autoscaled, scaled-to-zero when idle), so you pay only for raw
  resources — compute and storage — at cloud prices, typically 3–5× less than a
  managed CI for an equivalent VM size. And it's all *yours*: a feature-rich CI
  stack you own and control, not rented. The model isn't tied to one vendor;
  AWS is the integration implemented today.

## CI features out of the box

- **Deploy the whole project in one command.** RunnerPools, an orchestrator
  pool, S3 for artifacts/reports, SQS queues, SSM/Secrets Manager bindings —
  all declared in `ci/infrastructure/projects.py` and brought up with
  `praktika infrastructure --deploy`.
- **Declarative pipelines.** Jobs, dependencies, artifacts, parametrized runs,
  caching, secrets, and reports are plain Python objects in `ci/workflows/*.py`.
  Config errors surface at generation time, not halfway through a CI run.
- **Structured results, top to bottom.** Workflow → job → test all share the
  same `Result` type, so the whole run is one navigable tree — easy to drill
  into in the HTML report and easy for AI agents to parse and act on.
- **CI cache** that knows which jobs already succeeded and which artifacts can
  be reused, so unaffected and already-passed jobs are skipped natively and
  cached jobs reuse their artifacts instead of rebuilding.
- **Prebaked images** — named runtimes baked into AMIs so instances boot ready
  to run.
- **Docker pull-through proxy** for faster, more reliable image pulls that don't
  hit DockerHub rate limits.
- **Dynamic pipelines** — the pipeline content can be manipulated at runtime.

## Execution model

Praktika runs its own CI control plane on your cloud: the orchestrator polls
workflow queues, dispatches jobs to runner pools, and reports status back to the
Git host. GitHub Actions YAML generation exists as a compatibility option, but
it is not the primary model.

Today Praktika is developed around GitHub and AWS. The overall model is not
conceptually limited to them — those are the integrations implemented so far.

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
- Versioned runtimes via named base virtual environments, with `praktika_controller` selecting the workflow or job runtime and optionally installing Praktika at run time from checked-out source, per pool (`ext["runtime_source"]`; see `praktika/docs/installing-praktika.md`)
- HTML CI report page with per-workflow, per-job, and per-test drill-down
- Consistent test Docker image versioning: images rebuild automatically when inputs change, and versions stay pinned to code state across branches
- CI DB integration: job results, test results, timings, and related metadata are written automatically

**AI orchestration**
- Provider-agnostic AI advisor wired into the orchestrator loop: when a job
  result lands, the model gets a snapshot of the run plus the failing job's
  Result digest and investigates with sandboxed tools — reading job logs
  (allowlisted URLs) and the checked-out PR source (`grep_repo` / `read_file`) —
  then returns one constrained action:
  - **continue** — nothing actionable; let the run proceed
  - **drop run** (`cancel_run`) — a failure makes the rest of the run pointless;
    the orchestrator tears it down
  - **patch** (`cancel_and_patch`) — a clear, fixable defect: the model's exact
    edits are applied to the PR, committed and pushed as the GitHub App bot via
    the Git Data API (verified commit), and the superseded run is cancelled so
    the fresh run validates the fix
- Durable per-PR sessions persist every turn, decision, edit, and token/cost
  across CI runs — the AI's memory, so it doesn't retry a fix that already failed
- Bedrock / Anthropic providers (plus a no-op mock); enabled and configured via
  `Workflow.Config.ai_assist`

**GitHub side**
- Webhook ingestion via AWS Lambda
- `pull_request` and `push` pipeline triggers
- Status reporting through the GitHub Checks API
- Cancel and re-run from the GitHub UI:
  - **Cancel** the top-level check to stop an in-flight run.
  - **Re-run a single failed check** re-runs only that job and its failed
    downstream, in place on the same run — not the whole workflow. Every other
    job keeps its result, and Config Workflow is not re-run (the previous
    `WORKFLOW_CONFIG` is reused). Works whether the run is still going (the live
    orchestrator picks up the request) or already finished (a fresh orchestrator
    reopens the run from its persisted state). The re-run is rejected if the PR
    head has advanced past the check's commit, and fork-PR re-runs require a
    maintainer.
  - **Re-run all checks** re-runs the full workflow.
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
