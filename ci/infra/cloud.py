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
        "description": "Install runner OS-level tools like Docker and Copilot CLI",
        "commands": [
            "dnf install -y docker",
            "mkdir -p /etc/docker",
            "printf '%s\n' '{' '  \"log-driver\": \"json-file\",' '  \"log-opts\": {' '    \"max-file\": \"5\",' '    \"max-size\": \"1000m\"' '  }' '}' > /etc/docker/daemon.json",
            "usermod -aG docker ec2-user || true",
            "systemctl enable docker || true",
            "curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -",
            "dnf install -y nodejs",
            "npm install -g @github/copilot",
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
            regions=[CI_REGION],
            image_pipeline_name="praktika-orchestrator-arm64-imagebuilder-pipeline",
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}

_runner_pools = [
    NativeComponents.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling=NativeComponents.RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-runner-arm64-image"],
        user_data=_runner_user_data("praktika-arm-2xsmall"),
    ),
    NativeComponents.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        vpc_name=CI_VPC_NAME,
        scaling=NativeComponents.RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-runner-x86_64-image"],
        user_data=_runner_user_data("praktika-amd-2xsmall"),
    ),
]

_orchestrator_pool = NativeComponents.OrchestratorPool(
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    size=1,
    max_size=1,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-orchestrator-arm64-image"],
    user_data=_orchestrator_user_data(),
)

_cidb_cluster = NativeComponents.CIDBCluster(
    vpc_name=CI_VPC_NAME,
    instance_type="t4g.large",
    size=1,
)

CLOUD = CloudInfrastructure.Config(
    name="cloud_ci_infra",
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
    # Prebuild named Praktika runtimes into AMIs. Runner/orchestrator pools
    # still use the stock AL2023 AMIs until those LaunchTemplates are
    # explicitly pointed at the built AMI ids.
    image_builders=_IMAGE_BUILDERS,
    orchestrator_pool=_orchestrator_pool,
    runner_pools=_runner_pools,
    cidb_cluster=_cidb_cluster,
)
