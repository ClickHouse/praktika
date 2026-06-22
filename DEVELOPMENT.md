# Development

Notes for working on praktika itself (the Python package), not for adopting it
to drive your own CI.

## Build and publish `praktika` / `praktika-controller`

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
VERSION="$(.build-venv/bin/python -c 'from praktika.version import current_praktika_version; print(current_praktika_version())')"
.build-venv/bin/python -m build --wheel --no-isolation --outdir dist/
aws --profile Box s3 cp \
  "dist/praktika-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika-${VERSION}-py3-none-any.whl"
```

Build and upload `praktika-controller`:

```bash
VERSION="$(.build-venv/bin/python -c 'from praktika.version import current_praktika_controller_version; print(current_praktika_controller_version())')"
.build-venv/bin/python -m build --wheel --no-isolation --outdir bootstrap/dist bootstrap
aws --profile Box s3 cp \
  "bootstrap/dist/praktika_controller-${VERSION}-py3-none-any.whl" \
  "s3://praktika-artifacts-eu-north-1/packages/praktika_controller-${VERSION}-py3-none-any.whl"
```

Optionally, refresh the local install of `praktika` from the same S3 URL:

```bash
VERSION="$(python3 -c 'from praktika.version import current_praktika_version; print(current_praktika_version())')"
pip install --force-reinstall \
  "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-${VERSION}-py3-none-any.whl" \
  --break-system-packages
```

If you change the bootstrap package version, update the wheel name in both:

- `ci/infrastructure/projects.py`
- `ci/scripts/publish_controller_wheel.sh`

## Base vs non-base routing

Praktika can route workflows to different orchestrator queues and runner pools.
In this repo that split is used to keep one pipeline on "base" images, where
the previous Praktika release is baked into the AMI, while the normal pipelines
force-reinstall the current Praktika wheel into the shared base venv at boot
before starting the controller. This routing is usually not required for
ordinary Praktika projects; here it exists so we can keep a
backward-compatibility pipeline running against a previous Praktika release.

The workflow-side knobs are `Workflow.Config.orchestrator_filter` and
`Workflow.Config.native_job_runs_on`. The first decides which orchestrator
queue/pool is allowed to pick up the workflow, and the second lets Praktika's
built-in native jobs (`Config Workflow`, `Finish Workflow`) follow the same
runner family as the user jobs.

Infrastructure-wise this project defines two orchestrator pools instead of the
usual single one: the normal `workflow-orchestrator` pool and the
`workflow-orchestrator-base` pool. Base-routed workflows are picked up only by
the latter.

## Check logs on orchestrator or runners

Use the systemd journal on the live instance.

### A. Tail the systemd journal via SSM

Pick the ASG and unit for the side you're debugging:

| What | ASG | systemd unit |
|---|---|---|
| Workflow orchestrator | `praktika-workflow-orchestrator` | `praktika-controller` |
| Runner pool          | `praktika-arm-2xsmall` (or `praktika-amd-2xsmall`) | `praktika-controller` |

```bash
# Pick a side
ASG=praktika-amd-2xsmall ; UNIT=praktika-controller
# or:
# ASG=praktika-workflow-orchestrator ; UNIT=praktika-controller

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
