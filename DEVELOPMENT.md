# Development

Notes for working on praktika itself (the Python package), not for adopting it
to drive your own CI.

## Build and publish `praktika` / `praktika_bootstrap`

Runners, orchestrators, and AMI builds install both wheels from fixed S3 keys,
so after changing either package you need to rebuild it and overwrite the
matching object in `s3://praktika-artifacts-eu-north-1/packages/`.

Create one local build env and reuse it for both packages:

```bash
python3.12 -m venv .build-venv
.build-venv/bin/python -m pip install setuptools wheel build
```

Build and upload `praktika`:

```bash
.build-venv/bin/python -m build --wheel --no-isolation --outdir dist/
aws --profile Box s3 cp \
  dist/praktika-0.1-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika-0.1-py3-none-any.whl
```

Build and upload `praktika_bootstrap`:

```bash
.build-venv/bin/python -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap
aws --profile Box s3 cp \
  bootstrap/dist/praktika_bootstrap-0.1.0-py3-none-any.whl \
  s3://praktika-artifacts-eu-north-1/packages/praktika_bootstrap-0.1.0-py3-none-any.whl
```

Optionally, refresh the local install of `praktika` from the same S3 URL:

```bash
pip install --force-reinstall \
  "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl" \
  --break-system-packages
```

If you change the bootstrap package version, update the wheel name in both:

- `praktika/infrastructure/native/user_data_orchestrator.sh`
- `praktika/infrastructure/native/user_data_runner.sh`

## Prebaked wheelhouse on runner images

Both user-data scripts now populate a local wheelhouse at
`/opt/praktika/wheelhouse` and export `PRAKTIKA_WHEELHOUSE` into the agent
systemd unit. `praktika_bootstrap` will install the per-source Praktika venv
from that wheelhouse with `pip --no-index --find-links=...` when the directory
is present; otherwise it falls back to normal network installs.

If you add a new core Praktika dependency that should be available in prebaked
images, update the download list in both:

- `praktika/infrastructure/native/user_data_orchestrator.sh`
- `praktika/infrastructure/native/user_data_runner.sh`

## Check logs on orchestrator or runners

Two ways. The SSM grep is convenient for live tailing; the S3 dump is the
authoritative full journal (no 24 KB SSM truncation).

### A. Tail the systemd journal via SSM

Pick the ASG and unit for the side you're debugging:

| What | ASG | systemd unit |
|---|---|---|
| Workflow orchestrator | `praktika-workflow-orchestrator` | `workflow-agent` |
| Runner pool          | `praktika-arm-2xsmall` (or `praktika-amd-2xsmall`) | `job-agent` |

```bash
# Pick a side
ASG=praktika-amd-2xsmall ; UNIT=job-agent
# or:
# ASG=praktika-workflow-orchestrator ; UNIT=workflow-agent

INST=$(aws autoscaling describe-auto-scaling-instances \
  --region eu-north-1 --profile Box \
  --query "AutoScalingInstances[?AutoScalingGroupName=='$ASG'].InstanceId | [0]" \
  --output text)

# Grep for tracebacks with surrounding context (last 30 minutes)
CMD=$(aws ssm send-command --document-name AWS-RunShellScript \
  --instance-ids "$INST" --region eu-north-1 --profile Box \
  --parameters "commands=[\"journalctl -u $UNIT --since '30 min ago' --no-pager | grep -B 2 -A 30 Traceback | tail -200\"]" \
  --query 'Command.CommandId' --output text)

sleep 4
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$INST" \
  --region eu-north-1 --profile Box --query 'StandardOutputContent' --output text
```

For a free-form filter swap the inner pipeline for whatever you need
(e.g. `grep RECEIVED`, `tail -200`, `--since '5 min ago'`).

### B. Pull the full journal from S3 (no truncation)

Both agents upload a journal snapshot after every task / workflow:

| Side | S3 prefix |
|---|---|
| Workflow orchestrator | `s3://praktika-artifacts-eu-north-1/workflow-orchestrator/<date>/<instance>/<HH-MM-SS-...>.json` |
| Runner pool          | `s3://praktika-artifacts-eu-north-1/job-runner/<date>/<instance>/<HH-MM-SS-...>.json` |

```bash
# List today's runner logs, newest last
aws s3 ls --profile Box --recursive \
  "s3://praktika-artifacts-eu-north-1/job-runner/$(date -u +%Y-%m-%d)/" | sort | tail

# Fetch one
aws s3 cp --profile Box \
  s3://praktika-artifacts-eu-north-1/job-runner/2026-05-01/i-0e45fb9dbab778f40/15-09-57-998957.json - \
  | jq .
```
