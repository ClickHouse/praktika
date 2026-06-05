#!/usr/bin/env bash
# Praktika workflow agent bootstrap. Installs the standalone
# `praktika-bootstrap` package from S3 and runs its stable console
# entrypoint.
set -xeuo pipefail

echo "=== Workflow agent bootstrap ==="

# Install dependencies. Pull Python 3.12 alongside the AL2023 system Python
# 3.9 so boto3 stops spamming deprecation warnings (Python 3.9 support ends
# 2026-04-29). System python3 stays put for AL2023's own tooling.
dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli

# GitHub CLI (needed by praktika orchestrate to post check runs via gh auth token)
curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo \
  -o /etc/yum.repos.d/gh-cli.repo
dnf install -y gh
RUNNER_HOME=/opt/praktika
WHEELHOUSE_DIR="$RUNNER_HOME/wheelhouse"
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work" "$WHEELHOUSE_DIR"

python3.12 -m pip install boto3 pyjwt cryptography requests
python3.12 -m pip download \
  --dest "$WHEELHOUSE_DIR" \
  pip \
  setuptools \
  wheel \
  boto3 \
  pyjwt \
  cryptography \
  requests \
  pytest
PRAKTIKA_BOOTSTRAP_WHL="https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_bootstrap-0.1.1-py3-none-any.whl"
python3.12 -m pip install --force-reinstall "$PRAKTIKA_BOOTSTRAP_WHL" --break-system-packages

# Fetch instance identity via IMDS
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Write systemd service
cat > /etc/systemd/system/workflow-agent.service << EOF
[Unit]
Description=Praktika Workflow Agent
After=network.target

[Service]
Type=simple
# HOME must be set explicitly: \`gh auth login --with-token\` writes auth
# state to \$HOME/.config/gh/hosts.yml, and Type=simple services don't get
# HOME from systemd by default. Without it the agent's gh-auth call
# silently no-ops (writes nowhere usable), and child processes can't post
# commit statuses or check-run updates.
Environment=HOME=/root
Environment=SQS_QUEUE_NAME=__WORKFLOW_QUEUE_NAME__
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
Environment=PRAKTIKA_WHEELHOUSE=$WHEELHOUSE_DIR
ExecStart=/usr/local/bin/praktika_bootstrap workflow_orchestrator
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable workflow-agent
systemctl start workflow-agent

echo "=== Workflow agent ready ==="
