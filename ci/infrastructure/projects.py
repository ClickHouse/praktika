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
_PRAKTIKA_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1-py3-none-any.whl"
_PRAKTIKA_BOOTSTRAP_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_bootstrap-0.1.0-py3-none-any.whl"


def _orchestrator_user_data() -> str:
    return (_HERE / "user_data_orchestrator.sh").read_text()


def _runner_user_data(queue_name: str) -> str:
    template = (_HERE / "user_data_runner.sh").read_text()
    placeholder = "__RUNNER_QUEUE_NAME__"
    if placeholder not in template:
        raise RuntimeError(f"user_data_runner.sh is missing {placeholder}")
    return template.replace(placeholder, queue_name)


def _common_linux_component(name: str):
    return {
        "name": name,
        "version": "1.0.2",
        "platform": "Linux",
        "description": "Install common AL2023 system packages for Praktika images",
        "commands": [
            "dnf install -y python3 python3-pip python3.12 python3.12-pip git jq awscli",
            "ln -sf /usr/bin/python3.12 /usr/local/bin/python3",
        ],
    }


def _gh_cli_component(name: str):
    return {
        "name": name,
        "version": "1.0.1",
        "platform": "Linux",
        "description": "Install GitHub CLI on the image",
        "commands": [
            "curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo -o /etc/yum.repos.d/gh-cli.repo",
            "dnf install -y gh",
        ],
    }


def _runner_tools_component(name: str):
    return {
        "name": name,
        "version": "1.0.1",
        "platform": "Linux",
        "description": "Install runner OS-level tools like Docker",
        "commands": [
            "dnf install -y docker",
            "mkdir -p /etc/docker",
            "printf '%s\n' '{' '  \"log-driver\": \"json-file\",' '  \"log-opts\": {' '    \"max-file\": \"5\",' '    \"max-size\": \"1000m\"' '  }' '}' > /etc/docker/daemon.json",
            "usermod -aG docker ec2-user || true",
            "systemctl enable docker || true",
        ],
    }


def _runner_runtime_component(name: str):
    return {
        "name": name,
        "version": "1.0.0",
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
    return {
        "name": name,
        "version": "1.0.0",
        "platform": "Linux",
        "description": "Bake the Praktika job-agent service into the image",
        "commands": [
            "mkdir -p /etc/praktika",
            "printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' '' 'TOKEN=$(curl -fsS -X PUT \"http://169.254.169.254/latest/api/token\" -H \"X-aws-ec2-metadata-token-ttl-seconds: 60\")' 'REGION=${AWS_DEFAULT_REGION:-$(curl -fsS -H \"X-aws-ec2-metadata-token: $TOKEN\" http://169.254.169.254/latest/meta-data/placement/region)}' 'INSTANCE_ID=${INSTANCE_ID:-$(curl -fsS -H \"X-aws-ec2-metadata-token: $TOKEN\" http://169.254.169.254/latest/meta-data/instance-id)}' 'RUNNER_QUEUE_NAME=${RUNNER_QUEUE_NAME:-$(curl -fsS -H \"X-aws-ec2-metadata-token: $TOKEN\" http://169.254.169.254/latest/meta-data/tags/instance/praktika_queue || true)}' 'if [ -z \"$RUNNER_QUEUE_NAME\" ]; then' '  echo \"RUNNER_QUEUE_NAME is not set and instance tag praktika_queue is unavailable\" >&2' '  exit 1' 'fi' 'export HOME=/root' 'export AWS_DEFAULT_REGION=\"$REGION\"' 'export INSTANCE_ID=\"$INSTANCE_ID\"' 'export RUNNER_QUEUE_NAME' 'export PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse' 'exec /usr/local/bin/praktika_bootstrap job_runner' > /usr/local/bin/praktika-job-agent-start",
            "chmod 0755 /usr/local/bin/praktika-job-agent-start",
            "printf '%s\n' '[Unit]' 'Description=Praktika Job Agent' 'After=network.target docker.service' '' '[Service]' 'Type=simple' 'Environment=HOME=/root' 'Environment=PRAKTIKA_WHEELHOUSE=/opt/praktika/wheelhouse' 'EnvironmentFile=-/etc/praktika/job-agent.env' 'ExecStart=/usr/local/bin/praktika-job-agent-start' 'Restart=always' 'RestartSec=5' '' '[Install]' 'WantedBy=multi-user.target' > /etc/systemd/system/job-agent.service",
            "systemctl daemon-reload",
            "systemctl enable job-agent",
        ],
    }


def _orchestrator_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name="praktika-orchestrator",
            packages=[
                "pip",
                "setuptools",
                "wheel",
                "boto3",
                "PyJWT",
                "cryptography",
                "requests",
            ],
            description=(
                "Minimal Python base venv for Praktika workflow/orchestrator runs"
            ),
        ),
    ]


def _runner_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name="praktika-runner-pytest",
            packages=[
                "pip",
                "setuptools",
                "wheel",
                "boto3",
                "PyJWT",
                "cryptography",
                "requests",
                "pytest>=7.0.0",
            ],
            description=(
                "Runner Python base venv with pytest and Praktika runtime deps"
            ),
        ),
    ]


def _public_base_runner_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name="praktika-runner-pytest",
            packages=[
                "pip",
                "setuptools",
                "wheel",
                "boto3",
                "PyJWT",
                "cryptography",
                "requests",
                "pytest>=7.0.0",
                _PRAKTIKA_WHL,
            ],
            description=(
                "Runner Python base venv with pytest, Praktika runtime deps, and the published Praktika wheel"
            ),
        ),
    ]


def _image_builders():
    return [
        ImageBuilder.Config(
            name="praktika-runner-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-runner-arm64-image-recipe",
            image_recipe_version="1.0.2",
            inline_components=[
                _common_linux_component("praktika-runner-common-linux"),
                _gh_cli_component("praktika-runner-gh-cli"),
                _runner_tools_component("praktika-runner-tools"),
            ],
            prebuilt_venvs=_runner_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-runner-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-runner-arm64-imagebuilder-dist",
            ami_name="praktika-runner-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "runner", "arch": "arm64"},
            ami_launch_permission={"userGroups": ["all"]},
            regions=[CI_REGION],
            image_pipeline_name="praktika-runner-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-runner-x86_64-image",
            region=CI_REGION,
            image_recipe_name="praktika-runner-x86_64-image-recipe",
            image_recipe_version="1.0.2",
            inline_components=[
                _common_linux_component("praktika-runner-common-linux"),
                _gh_cli_component("praktika-runner-gh-cli"),
                _runner_tools_component("praktika-runner-tools"),
            ],
            prebuilt_venvs=_runner_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-runner-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-runner-x86_64-imagebuilder-dist",
            ami_name="praktika-runner-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "runner", "arch": "x86_64"},
            ami_launch_permission={"userGroups": ["all"]},
            regions=[CI_REGION],
            image_pipeline_name="praktika-runner-x86_64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-orchestrator-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-orchestrator-arm64-image-recipe",
            image_recipe_version="1.0.2",
            inline_components=[
                _common_linux_component("praktika-orchestrator-common-linux"),
                _gh_cli_component("praktika-orchestrator-gh-cli"),
            ],
            prebuilt_venvs=_orchestrator_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-orchestrator-arm64-imagebuilder-infra",
            instance_profile_name=ORCHESTRATOR_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-orchestrator-arm64-imagebuilder-dist",
            ami_name="praktika-orchestrator-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "workflow_orchestrator", "arch": "arm64"},
            ami_launch_permission={"userGroups": ["all"]},
            regions=[CI_REGION],
            image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-runner-arm64-image",
            region=CI_REGION,
            image_recipe_name="praktika-base-runner-arm64-image-recipe",
            image_recipe_version="1.0.0",
            inline_components=[
                _common_linux_component("praktika-base-runner-common-linux"),
                _gh_cli_component("praktika-base-runner-gh-cli"),
                _runner_tools_component("praktika-base-runner-tools"),
                _runner_runtime_component("praktika-base-runner-runtime"),
                _runner_agent_component("praktika-base-runner-agent"),
            ],
            prebuilt_venvs=_public_base_runner_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-runner-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-runner-arm64-imagebuilder-dist",
            ami_name="praktika-base-runner-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_runner", "arch": "arm64"},
            ami_launch_permission={"userGroups": ["all"]},
            regions=[CI_REGION],
            image_pipeline_name="praktika-base-runner-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-runner-x86_64-image",
            region=CI_REGION,
            image_recipe_name="praktika-base-runner-x86_64-image-recipe",
            image_recipe_version="1.0.0",
            inline_components=[
                _common_linux_component("praktika-base-runner-common-linux"),
                _gh_cli_component("praktika-base-runner-gh-cli"),
                _runner_tools_component("praktika-base-runner-tools"),
                _runner_runtime_component("praktika-base-runner-runtime"),
                _runner_agent_component("praktika-base-runner-agent"),
            ],
            prebuilt_venvs=_public_base_runner_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-runner-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-runner-x86_64-imagebuilder-dist",
            ami_name="praktika-base-runner-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_runner", "arch": "x86_64"},
            ami_launch_permission={"userGroups": ["all"]},
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
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    scaling=NativeComponents.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-orchestrator-arm64-image"],
    user_data=_orchestrator_user_data(),
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
        orchestrator_pool=_orchestrator_pool,
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
