#!/usr/bin/env bash
set -xeuo pipefail

echo "=== Job agent bootstrap ==="

RUNNER_HOME=/opt/praktika
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work"

PRAKTIKA_BOOTSTRAP_WHL="https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_bootstrap-0.1.0-py3-none-any.whl"
python3.12 -m pip install --force-reinstall "$PRAKTIKA_BOOTSTRAP_WHL" --break-system-packages

systemctl start docker || true

TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

cat > /etc/systemd/system/job-agent.service << EOF
[Unit]
Description=Praktika Job Agent
After=network.target docker.service

[Service]
Type=simple
Environment=HOME=/root
Environment=RUNNER_QUEUE_NAME=__RUNNER_QUEUE_NAME__
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
ExecStart=/usr/local/bin/praktika_bootstrap job_runner
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable job-agent
systemctl start job-agent

echo "=== Job agent ready ==="
