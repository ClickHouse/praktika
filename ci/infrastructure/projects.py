import base64
from pathlib import Path

from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import ImageBuilder, NativeComponents, Storage, VPC
from praktika.infrastructure.native.configs import (
    ORCHESTRATOR_INSTANCE_PROFILE_NAME,
    RUNNER_INSTANCE_PROFILE_NAME,
)

CI_VPC_NAME = "praktika-ci"
CI_REGION = "eu-north-1"
_HERE = Path(__file__).parent
_PRAKTIKA_BASE_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl"
_PRAKTIKA_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1.1-py3-none-any.whl"
_PRAKTIKA_BOOTSTRAP_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_bootstrap-0.1.1-py3-none-any.whl"
_RUNTIME_BASE_VENV = "praktika-runtime"


def _orchestrator_user_data(queue_name: str) -> str:
    template = (_HERE / "user_data_orchestrator.sh").read_text()
    placeholder = "__WORKFLOW_QUEUE_NAME__"
    if placeholder not in template:
        raise RuntimeError(f"user_data_orchestrator.sh is missing {placeholder}")
    return template.replace(placeholder, queue_name)


def _runner_user_data(queue_name: str) -> str:
    template = (_HERE / "user_data_runner.sh").read_text()
    placeholder = "__RUNNER_QUEUE_NAME__"
    if placeholder not in template:
        raise RuntimeError(f"user_data_runner.sh is missing {placeholder}")
    return template.replace(placeholder, queue_name)


def _write_file_from_base64(path: str, content: str) -> str:
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return f"printf '%s' '{payload}' | base64 -d > {path}"


def _setup_component(name: str, *, with_docker: bool):
    commands = [
        "dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli",
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


def _runner_runtime_component(name: str):
    return {
        "name": name,
        "platform": "Linux",
        "description": "Install Praktika bootstrap and runner runtime dependencies into the image",
        "commands": [
            "mkdir -p /opt/praktika /opt/praktika/work /opt/praktika/wheelhouse",
            "python3.12 -m pip install boto3 pyjwt cryptography requests",
            "python3.12 -m pip download --dest /opt/praktika/wheelhouse pip setuptools wheel boto3 pyjwt cryptography requests pytest",
            f"python3.12 -m pip download --dest /opt/praktika/wheelhouse {_PRAKTIKA_WHL}",
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_BOOTSTRAP_WHL} --break-system-packages",
            "ln -sf /usr/bin/python3.12 /usr/local/bin/python3",
        ],
    }


def _runner_agent_component(name: str):
    launcher = """#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=${AWS_DEFAULT_REGION:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)}
INSTANCE_ID=${INSTANCE_ID:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)}
RUNNER_QUEUE_NAME=${RUNNER_QUEUE_NAME:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_queue || true)}
if [ -z "$RUNNER_QUEUE_NAME" ]; then
  echo "RUNNER_QUEUE_NAME is not set and instance tag praktika_queue is unavailable" >&2
  exit 1
fi
export HOME=/root
export AWS_DEFAULT_REGION="$REGION"
export INSTANCE_ID="$INSTANCE_ID"
export RUNNER_QUEUE_NAME
export PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse
exec /usr/local/bin/praktika_bootstrap job_runner
"""
    unit = """[Unit]
Description=Praktika Job Agent
After=network.target docker.service

[Service]
Type=simple
Environment=HOME=/root
Environment=PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse
EnvironmentFile=-/etc/praktika/job-agent.env
ExecStart=/usr/local/bin/praktika-job-agent-start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    return {
        "name": name,
        "platform": "Linux",
        "description": "Bake the Praktika job-agent service into the image",
        "commands": [
            "mkdir -p /etc/praktika",
            _write_file_from_base64("/usr/local/bin/praktika-job-agent-start", launcher),
            "chmod 0755 /usr/local/bin/praktika-job-agent-start",
            _write_file_from_base64("/etc/systemd/system/job-agent.service", unit),
            "mkdir -p /etc/systemd/system/multi-user.target.wants",
            "ln -sfn /etc/systemd/system/job-agent.service /etc/systemd/system/multi-user.target.wants/job-agent.service",
        ],
    }


def _orchestrator_runtime_component(name: str):
    return {
        "name": name,
        "platform": "Linux",
        "description": "Install Praktika bootstrap and orchestrator runtime dependencies into the image",
        "commands": [
            "mkdir -p /opt/praktika /opt/praktika/work /opt/praktika/wheelhouse",
            "python3.12 -m pip install boto3 pyjwt cryptography requests",
            "python3.12 -m pip download --dest /opt/praktika/wheelhouse pip setuptools wheel boto3 pyjwt cryptography requests pytest",
            f"python3.12 -m pip download --dest /opt/praktika/wheelhouse {_PRAKTIKA_WHL}",
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_BOOTSTRAP_WHL} --break-system-packages",
        ],
    }


def _orchestrator_agent_component(name: str):
    launcher = """#!/usr/bin/env bash
set -euo pipefail

TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=${AWS_DEFAULT_REGION:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)}
INSTANCE_ID=${INSTANCE_ID:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)}
SQS_QUEUE_NAME=${SQS_QUEUE_NAME:-$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/tags/instance/praktika_queue || true)}
if [ -z "$SQS_QUEUE_NAME" ]; then
  echo "SQS_QUEUE_NAME is not set and instance tag praktika_queue is unavailable" >&2
  exit 1
fi
export HOME=/root
export AWS_DEFAULT_REGION="$REGION"
export INSTANCE_ID="$INSTANCE_ID"
export SQS_QUEUE_NAME
export PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse
exec /usr/local/bin/praktika_bootstrap workflow_orchestrator
"""
    unit = """[Unit]
Description=Praktika Workflow Agent
After=network.target

[Service]
Type=simple
Environment=HOME=/root
Environment=PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse
EnvironmentFile=-/etc/praktika/workflow-agent.env
ExecStart=/usr/local/bin/praktika-workflow-agent-start
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    return {
        "name": name,
        "platform": "Linux",
        "description": "Bake the Praktika workflow-agent service into the image",
        "commands": [
            "mkdir -p /etc/praktika",
            _write_file_from_base64(
                "/usr/local/bin/praktika-workflow-agent-start", launcher
            ),
            "chmod 0755 /usr/local/bin/praktika-workflow-agent-start",
            _write_file_from_base64("/etc/systemd/system/workflow-agent.service", unit),
            "mkdir -p /etc/systemd/system/multi-user.target.wants",
            "ln -sfn /etc/systemd/system/workflow-agent.service /etc/systemd/system/multi-user.target.wants/workflow-agent.service",
        ],
    }


def _runtime_base_packages(*, include_praktika: bool = False):
    packages = [
        "boto3",
        "PyJWT",
        "cryptography",
        "requests",
        "pytest>=7.0.0",
    ]
    if include_praktika:
        packages.append(_PRAKTIKA_BASE_WHL)
    return packages


def _runtime_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name=_RUNTIME_BASE_VENV,
            packages=_runtime_base_packages(),
            description=(
                "Shared Python base venv for Praktika workflow and job runs"
            ),
        ),
    ]

def _public_runtime_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name=_RUNTIME_BASE_VENV,
            packages=_runtime_base_packages(include_praktika=True),
            description=(
                "Shared Python base venv with pytest, Praktika runtime deps, and the published Praktika wheel"
            ),
        ),
    ]


def _image_builders():
    runner_arm64_version = "1.0.8"
    runner_x86_64_version = "1.0.8"
    orchestrator_arm64_version = "1.0.8"
    base_runner_arm64_version = "1.0.8"
    base_orchestrator_arm64_version = "1.0.8"
    base_runner_x86_64_version = "1.0.8"

    return [
        ImageBuilder.Config(
            name="praktika-runner-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-runner-arm64-image-recipe",
            image_recipe_version=runner_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-runner-setup",
                    with_docker=True,
                ),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-runner-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-runner-arm64-imagebuilder-dist",
            ami_name="praktika-runner-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "runner", "arch": "arm64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-runner-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-runner-x86_64-image",
            region=CI_REGION,
            image_recipe_name="praktika-runner-x86_64-image-recipe",
            image_recipe_version=runner_x86_64_version,
            inline_components=[
                _setup_component(
                    "praktika-runner-setup",
                    with_docker=True,
                ),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-runner-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-runner-x86_64-imagebuilder-dist",
            ami_name="praktika-runner-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "runner", "arch": "x86_64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-runner-x86_64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-orchestrator-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-orchestrator-arm64-image-recipe",
            image_recipe_version=orchestrator_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-orchestrator-setup",
                    with_docker=False,
                ),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-orchestrator-arm64-imagebuilder-infra",
            instance_profile_name=ORCHESTRATOR_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-orchestrator-arm64-imagebuilder-dist",
            ami_name="praktika-orchestrator-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "workflow_orchestrator", "arch": "arm64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-runner-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-base-runner-arm64-image-recipe",
            image_recipe_version=base_runner_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-base-runner-setup",
                    with_docker=True,
                ),
                _runner_runtime_component(
                    "praktika-base-runner-runtime",
                ),
                _runner_agent_component(
                    "praktika-base-runner-agent",
                ),
            ],
            prebuilt_venvs=_public_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-runner-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-runner-arm64-imagebuilder-dist",
            ami_name="praktika-base-runner-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_runner", "arch": "arm64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-base-runner-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-orchestrator-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-base-orchestrator-arm64-image-recipe",
            image_recipe_version=base_orchestrator_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-base-orchestrator-setup",
                    with_docker=False,
                ),
                _orchestrator_runtime_component(
                    "praktika-base-orchestrator-runtime",
                ),
                _orchestrator_agent_component(
                    "praktika-base-orchestrator-agent",
                ),
            ],
            prebuilt_venvs=_public_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-orchestrator-arm64-imagebuilder-infra",
            instance_profile_name=ORCHESTRATOR_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-orchestrator-arm64-imagebuilder-dist",
            ami_name="praktika-base-orchestrator-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_orchestrator", "arch": "arm64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-base-orchestrator-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-runner-x86_64-image",
            region=CI_REGION,
            image_recipe_name="praktika-base-runner-x86_64-image-recipe",
            image_recipe_version=base_runner_x86_64_version,
            inline_components=[
                _setup_component(
                    "praktika-base-runner-setup",
                    with_docker=True,
                ),
                _runner_runtime_component(
                    "praktika-base-runner-runtime",
                ),
                _runner_agent_component(
                    "praktika-base-runner-agent",
                ),
            ],
            prebuilt_venvs=_public_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-runner-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-runner-x86_64-imagebuilder-dist",
            ami_name="praktika-base-runner-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_runner", "arch": "x86_64"},
            regions=[CI_REGION],
            image_pipeline_name="praktika-base-runner-x86_64-imagebuilder-pipeline",
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}
_gh_token_minter = NativeComponents.GitHubTokenMinter(
    name="praktika-gh-token",
    role_name="praktika-gh-token-role",
    secret_name="praktika-gh-app",
    region=CI_REGION,
    repositories=["praktika"],
    permissions={
        "checks": "write",
        "contents": "read",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "write",
    },
)

_runner_pools = [
    NativeComponents.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling=NativeComponents.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-runner-arm64-image"],
        user_data=_runner_user_data("praktika-arm-2xsmall"),
    ),
    NativeComponents.RunnerPool(
        name="arm-2xsmall-base",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling=NativeComponents.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-base-runner-arm64-image"],
        # The AMI already contains the Praktika job-agent systemd unit; keep
        # launch-time user_data empty so this pool exercises the baked image.
        user_data="#!/usr/bin/env bash\ntrue\n",
    ),
    NativeComponents.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        vpc_name=CI_VPC_NAME,
        scaling=NativeComponents.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-runner-x86_64-image"],
        user_data=_runner_user_data("praktika-amd-2xsmall"),
    ),
]

_orchestrator_pool = NativeComponents.OrchestratorPool(
    name="workflow-orchestrator",
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    scaling=NativeComponents.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-orchestrator-arm64-image"],
    user_data=_orchestrator_user_data("workflow-orchestrator"),
    gh_trigger_role_name="gh-trigger-shared-role",
    gh_trigger_webhook_secret_name="gh-trigger-shared-secret",
)

_orchestrator_pool_base = NativeComponents.OrchestratorPool(
    name="workflow-orchestrator-base",
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    scaling=NativeComponents.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-base-orchestrator-arm64-image"],
    user_data="#!/usr/bin/env bash\ntrue\n",
    gh_trigger_role_name="gh-trigger-shared-role",
    gh_trigger_webhook_secret_name="gh-trigger-shared-secret",
)

_cidb_cluster = NativeComponents.CIDBCluster(
    vpc_name=CI_VPC_NAME,
    instance_type="t4g.large",
    size=1,
)

PROJECTS = [
    CloudInfrastructure.Config(
        name="praktika",
        vpcs=[
            VPC.Config(
                name=CI_VPC_NAME,
                subnets=[
                    VPC.Subnet(availability_zone="eu-north-1a"),
                ],
            )
        ],
        storages=[
            Storage.Config(name="praktika-artifacts-eu-north-1", retention_days=90, public=True),
        ],
        report_pages=[
            NativeComponents.report_page_config,
        ],
        image_builders=_IMAGE_BUILDERS,
        github_token_minters=[_gh_token_minter],
        orchestrator_pools=[_orchestrator_pool, _orchestrator_pool_base],
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
