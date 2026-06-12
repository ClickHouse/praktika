import base64
from typing import Any, Dict, List, Optional

from ..image_builder import ImageBuilder

PRAKTIKA_PACKAGE_BASE_URL = (
    "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages"
)


def _write_file_from_base64(path: str, content: str) -> str:
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf '%s' '{payload}' | base64 -d > {path}"


def _setup_component(name: str, *, with_docker: bool):
    commands = [
        "dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli",
        "dnf install -y amazon-cloudwatch-agent",
        "ln -sf /usr/bin/python3.12 /usr/local/bin/python3",
        "curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo -o /etc/yum.repos.d/gh-cli.repo",
        "dnf install -y gh",
    ]
    if with_docker:
        commands.extend(
            [
                "dnf install -y docker",
                "mkdir -p /etc/docker",
                "printf '%s\n' '{' '  \"log-driver\": \"json-file\",' '  \"log-opts\": {' '    \"max-file\": \"5\",' '    \"max-size\": \"1000m\"' '  }' '}' > /etc/docker/daemon.json",
                "usermod -aG docker ec2-user || true",
                "systemctl enable docker || true",
            ]
        )
    return {
        "name": name,
        "platform": "Linux",
        "description": (
            "Install AL2023 system packages, GitHub CLI, and Docker"
            if with_docker
            else "Install AL2023 system packages and GitHub CLI"
        ),
        "commands": commands,
    }


def _controller_runtime_component(name: str, *, controller_package: str):
    return {
        "name": name,
        "platform": "Linux",
        "description": "Install Praktika controller runtime dependencies into the image",
        "commands": [
            "mkdir -p /opt/praktika /opt/praktika/work",
            "python3.12 -m pip install boto3 pyjwt cryptography requests",
            f"python3.12 -m pip install --force-reinstall {controller_package} --break-system-packages",
            "ln -sf /usr/bin/python3.12 /usr/local/bin/python3",
        ],
    }


def _praktika_controller_component(name: str):
    launcher = """#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=${AWS_DEFAULT_REGION:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)}
INSTANCE_ID=${INSTANCE_ID:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)}
PRAKTIKA_CONTROLLER_ROLE=${PRAKTIKA_CONTROLLER_ROLE:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_role || true)}
PRAKTIKA_CONTROLLER_QUEUE=${PRAKTIKA_CONTROLLER_QUEUE:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_queue || true)}
PRAKTIKA_PROJECT_SLUG=${PRAKTIKA_PROJECT_SLUG:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_project_slug || true)}
if [ -z "$PRAKTIKA_CONTROLLER_ROLE" ] || [ -z "$PRAKTIKA_CONTROLLER_QUEUE" ]; then
  echo "praktika_role or praktika_queue instance tag is unavailable" >&2
  exit 1
fi
export HOME=/root
export AWS_DEFAULT_REGION="$REGION"
export INSTANCE_ID="$INSTANCE_ID"
export PRAKTIKA_PROJECT_SLUG
export PRAKTIKA_CONTROLLER_ROLE
export PRAKTIKA_CONTROLLER_QUEUE
exec /usr/local/bin/praktika-controller
"""
    cloudwatch_configure = """#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
PRAKTIKA_PROJECT_SLUG=${PRAKTIKA_PROJECT_SLUG:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_project_slug || true)}
if [ -z "$PRAKTIKA_PROJECT_SLUG" ]; then
  echo "praktika_project_slug instance tag is unavailable" >&2
  exit 1
fi

cat > /etc/praktika/amazon-cloudwatch-agent.json <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/praktika-controller.log",
            "log_group_name": "/${PRAKTIKA_PROJECT_SLUG}/praktika-controller",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC"
          }
        ]
      }
    }
  }
}
EOF
"""
    unit = """[Unit]
Description=Praktika Controller
After=network.target docker.service

[Service]
Type=simple
Environment=HOME=/root
ExecStart=/usr/local/bin/praktika-controller-start
Restart=always
RestartSec=5
StandardOutput=append:/var/log/praktika-controller.log
StandardError=append:/var/log/praktika-controller.log

[Install]
WantedBy=multi-user.target
"""
    return {
        "name": name,
        "platform": "Linux",
        "description": "Bake the Praktika controller service into the image",
        "commands": [
            "mkdir -p /etc/praktika",
            "touch /var/log/praktika-controller.log",
            "chmod 0644 /var/log/praktika-controller.log",
            _write_file_from_base64(
                "/usr/local/bin/praktika-controller-start", launcher
            ),
            "chmod 0755 /usr/local/bin/praktika-controller-start",
            _write_file_from_base64(
                "/usr/local/bin/praktika-configure-cloudwatch-agent",
                cloudwatch_configure,
            ),
            "chmod 0755 /usr/local/bin/praktika-configure-cloudwatch-agent",
            _write_file_from_base64(
                "/etc/systemd/system/praktika-controller.service", unit
            ),
            "systemctl daemon-reload || true",
        ],
    }


def praktika_venv_config(name: str, praktika_version: str) -> ImageBuilder.PrebuiltVenv:
    return ImageBuilder.PrebuiltVenv(
        name=name,
        packages=[
            "boto3",
            "PyJWT",
            "cryptography",
            "requests",
            "pytest>=7.0.0",
            f"{PRAKTIKA_PACKAGE_BASE_URL}/praktika-{praktika_version}-py3-none-any.whl",
        ],
        description=(
            "Shared Python base venv with pytest, Praktika runtime deps, "
            "and Praktika"
        ),
    )


def image_builder_config(
    *,
    name: str,
    version: str,
    instance_types: List[str],
    components: Optional[List[Dict[str, Any]]] = None,
    prebuilt_venvs: Optional[List[ImageBuilder.PrebuiltVenv]] = None,
    controller_package: str = "praktika-controller",
) -> ImageBuilder.Config:
    return ImageBuilder.Config(
        name=name,
        image_recipe_version=version,
        inline_components=[
            _setup_component("praktika-controller-setup", with_docker=True),
            _controller_runtime_component(
                "praktika-controller-runtime",
                controller_package=controller_package,
            ),
            _praktika_controller_component("praktika-controller"),
            *(components or []),
        ],
        prebuilt_venvs=list(prebuilt_venvs or []),
        instance_types=instance_types,
    )
