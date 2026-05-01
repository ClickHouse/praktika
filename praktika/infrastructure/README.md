# praktika.infrastructure

Declarative AWS infrastructure for praktika CI: VPCs, S3, runner / orchestrator
pools, and the HTML report page — all defined in `ci/infra/cloud.py` and
brought up with one command.

## Typical commands

```bash
# Deploy (or update) all components defined in ci/infra/cloud.py
python3 -m praktika infrastructure --deploy

# Deploy only specific component types
python3 -m praktika infrastructure --deploy --only ImageBuilder LaunchTemplate
python3 -m praktika infrastructure --deploy --only AutoScalingGroup

# Roll EC2 instances on every ASG (replace with the latest launch template version)
python3 -m praktika infrastructure --restart-instances

# Tear down EC2 instances / Dedicated Hosts (does not touch ASGs/LTs themselves)
python3 -m praktika infrastructure --shutdown --only EC2Instance
python3 -m praktika infrastructure --shutdown --only DedicatedHost
```

## Config components (used in `ci/infra/cloud.py`)

- **`CloudInfrastructure.Config`** — top-level container; aggregates all
  components below into a single deployable unit.
- **`VPC.Config`** — a VPC + subnets in declared availability zones. Runner
  and orchestrator pools attach to a VPC by name.
- **`Storage.Config`** — an S3 bucket for artifacts and the HTML report,
  with retention policy and public/private access.
- **`NativeComponents.OrchestratorPool`** — ASG of EC2 VMs that polls SQS,
  resolves workflow DAGs, and dispatches jobs to runner pools.
- **`NativeComponents.RunnerPool`** — ASG of EC2 VMs that pull job tasks
  from per-pool SQS queues and execute them. Pools are referenced by jobs
  via the `runs_on` label.
- **`NativeComponents.report_page_config`** — the static HTML page +
  bucket policy that renders a workflow's `result_*.json` files.

## TODO

- Pools of dedicated VMs / bare metal (e.g. EC2 Dedicated Hosts, baremetal
  instance types) as a first-class `RunnerPool` mode
- CI DB (ClickHouse) deployment as a managed component
- Private-access gateway (VPN / bastion) deployment for reaching the report
  page and CI DB on private endpoints, and optionally for SSH into runners
