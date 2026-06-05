#!/usr/bin/env bash
set -xeuo pipefail

echo "=== Workflow agent bootstrap ==="

RUNNER_HOME=/opt/praktika
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work"

PRAKTIKA_BOOTSTRAP_WHL="https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_bootstrap-0.1.1-py3-none-any.whl"
python3.12 -m pip install --force-reinstall "$PRAKTIKA_BOOTSTRAP_WHL" --break-system-packages

TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

cat > /etc/systemd/system/workflow-agent.service << EOF
[Unit]
Description=Praktika Workflow Agent
After=network.target

[Service]
Type=simple
Environment=HOME=/root
Environment=SQS_QUEUE_NAME=__WORKFLOW_QUEUE_NAME__
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
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
