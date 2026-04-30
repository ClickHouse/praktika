#!/usr/bin/env bash
# Praktika job agent bootstrap. Polls a per-runner-type SQS queue and
# invokes `praktika orchestrate job ...` for each task. Deployed per runner
# type: one LT bakes this script with a different `RUNNER_QUEUE_NAME`. The
# agent body is gzip+base64 encoded to stay under the 16 KB EC2 user_data
# limit (same trick as the workflow agent's user_data).
set -xeuo pipefail

echo "=== Job agent bootstrap ==="

# Install dependencies. Explicitly pull Python 3.12 alongside the system
# Python 3.9 (AL2023 default) so boto3 stops spamming deprecation warnings
# and keeps receiving updates past its 2026-04-29 3.9 cutoff. We keep the
# system python3 around for the AL2023 tooling that depends on it
# (cloud-init, dnf-plugins, ...).
dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli docker
python3.12 -m pip install boto3 pyjwt cryptography requests
python3.12 -m pip install --force-reinstall \
  "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl" \
  --break-system-packages

# Job scripts shell out to `python3 ./ci/jobs/...` without qualifying the
# minor version, so we need `python3` in PATH to resolve to 3.12 too —
# otherwise the jobs pick up the system 3.9 interpreter and miss the pip
# deps we just installed (pyjwt, requests, ...). Symlinking under
# /usr/local/bin shadows /usr/bin/python3 via the default systemd PATH
# (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin) without touching
# /usr/bin/python3 itself, so AL2023 tooling that hard-codes the absolute
# path keeps working.
ln -sf /usr/bin/python3.12 /usr/local/bin/python3

# GitHub CLI (needed by praktika native jobs for posting CI reports)
curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo \
  -o /etc/yum.repos.d/gh-cli.repo
dnf install -y gh

# Install Node.js 20 and the GitHub Copilot CLI so `copilot_review_job.py`
# can invoke `copilot` directly. Mirrors the ClickHouse CI AMI setup — see
# tests/ci/terraform/worker/prepare-ci-ami.sh — except via the RPM-based
# Nodesource repo for AL2023.
curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -
dnf install -y nodejs
npm install -g @github/copilot

# Configure + start Docker. Mirrors the knobs the ClickHouse CI AMI uses
# (see tests/ci/terraform/worker/prepare-ci-ami.sh) — log rotation on by
# default, and the ec2-user (and root, implicitly) can invoke docker.
# `dnf install docker` on AL2023 ships Amazon's docker-ce build, which is
# enough for the `docker run ...` invocations praktika makes from jobs.
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOT'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-file": "5",
    "max-size": "1000m"
  }
}
EOT
usermod -aG docker ec2-user || true
systemctl enable --now docker

# Runner workdir
RUNNER_HOME=/opt/praktika
mkdir -p "$RUNNER_HOME" "$RUNNER_HOME/work"

# Instance identity via IMDS
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

# Write job_agent.py (body injected by user_data.runner_user_data at deploy time).
echo '__RUN_JOB_PY_CONTENTS__' | base64 -d | gunzip > "$RUNNER_HOME/job_agent.py"

# Write systemd service. RUNNER_QUEUE_NAME is baked in per runner type.
cat > /etc/systemd/system/job-agent.service << EOF
[Unit]
Description=Praktika Job Agent
After=network.target

[Service]
Type=simple
# HOME must be set explicitly: \`gh auth login --with-token\` writes auth
# state to \$HOME/.config/gh/hosts.yml, and Type=simple services don't get
# HOME from systemd by default. Without it the agent's gh-auth call
# silently no-ops (writes nowhere usable), and child processes can't post
# commit statuses or check-run updates.
Environment=HOME=/root
Environment=RUNNER_QUEUE_NAME=__RUNNER_QUEUE_NAME__
Environment=AWS_DEFAULT_REGION=$REGION
Environment=INSTANCE_ID=$INSTANCE_ID
ExecStart=/usr/bin/python3.12 -u $RUNNER_HOME/job_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable job-agent
systemctl start job-agent

echo "=== Job agent ready ==="
