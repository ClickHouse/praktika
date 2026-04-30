#!/usr/bin/env bash
# Praktika workflow agent: polls SQS for workflow triggers and dispatches
# each one. The agent body below is substituted at deploy time from
# `praktika/orchestrator/workflow_agent.py` (see `user_data.py`), so the
# same code runs both locally and on the EC2 orchestrator instances.
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
python3.12 -m pip install boto3 pyjwt cryptography requests
python3.12 -m pip install --force-reinstall \
  "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl" \
  --break-system-packages

# Create runner workdir
RUNNER_HOME=/opt/praktika
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work"

# Fetch instance identity via IMDS
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Write the agent script. The body is gzip+base64 encoded at deploy time
# (user_data.ci_engine_user_data) so the script fits into the 16 KB EC2
# user_data limit; we decode it back here.
echo '__RUN_PY_CONTENTS__' | base64 -d | gunzip > "$RUNNER_HOME/workflow_agent.py"

chmod +x "$RUNNER_HOME/workflow_agent.py"

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
Environment=SQS_QUEUE_NAME=praktika-workflows
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
ExecStart=/usr/bin/python3.12 -u $RUNNER_HOME/workflow_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable workflow-agent
systemctl start workflow-agent

echo "=== Workflow agent ready ==="
