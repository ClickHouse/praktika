#!/usr/bin/env bash
# Minimal CI engine runner: polls SQS for workflow triggers and executes them.
# The `run.py` body below is substituted at deploy time from
# `ci/praktika/orchestrator/run.py` (see `ci/infra/cloud.py`), so the same
# code runs both locally and on the EC2 runners.
set -xeuo pipefail

echo "=== CI engine runner bootstrap ==="

# Install dependencies. Pull Python 3.12 alongside the AL2023 system Python
# 3.9 so boto3 stops spamming deprecation warnings (Python 3.9 support ends
# 2026-04-29). System python3 stays put for AL2023's own tooling.
dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli
python3.12 -m pip install boto3 pyjwt cryptography requests

# Create runner workdir
RUNNER_HOME=/opt/ci-engine
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work"

# Fetch instance identity via IMDS
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Write the runner script. The body is gzip+base64 encoded at deploy time
# (`ci/infra/cloud.py::_ci_engine_user_data`) so the script fits into the
# 16 KB EC2 user_data limit; we decode it back here.
echo '__RUN_PY_CONTENTS__' | base64 -d | gunzip > "$RUNNER_HOME/run.py"

chmod +x "$RUNNER_HOME/run.py"

# Write systemd service
cat > /etc/systemd/system/ci-engine.service << EOF
[Unit]
Description=Praktika CI Engine Runner
After=network.target

[Service]
Type=simple
Environment=SQS_QUEUE_NAME=praktika_clickhouse_workflows
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
ExecStart=/usr/bin/python3.12 -u $RUNNER_HOME/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ci-engine
systemctl start ci-engine

echo "=== CI engine runner ready ==="
