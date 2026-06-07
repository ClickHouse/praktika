import base64

from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import ImageBuilder, Components, Storage, VPC
from praktika.infrastructure.native.configs import RUNNER_INSTANCE_PROFILE_NAME


CI_VPC_NAME = "praktika-ci"
_PRAKTIKA_BASE_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.0.1-py3-none-any.whl"
_PRAKTIKA_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1.1-py3-none-any.whl"
_PRAKTIKA_CONTROLLER_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_controller-0.1.1-py3-none-any.whl"
_RUNTIME_BASE_VENV = "praktika-runtime"


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


def _controller_runtime_component(name: str):
    return {
        "name": name,
        "platform": "Linux",
        "description": "Install Praktika controller runtime dependencies into the image",
        "commands": [
            "mkdir -p /opt/praktika /opt/praktika/work",
            "python3.12 -m pip install boto3 pyjwt cryptography requests",
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
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
if [ -z "$PRAKTIKA_CONTROLLER_ROLE" ] || [ -z "$PRAKTIKA_CONTROLLER_QUEUE" ]; then
  echo "praktika_role or praktika_queue instance tag is unavailable" >&2
  exit 1
fi
export HOME=/root
export AWS_DEFAULT_REGION="$REGION"
export INSTANCE_ID="$INSTANCE_ID"
export PRAKTIKA_CONTROLLER_ROLE
export PRAKTIKA_CONTROLLER_QUEUE
exec /usr/local/bin/praktika-controller
"""
    cloudwatch = """{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/praktika-controller.log",
            "log_group_name": "/praktika/controller",
            "log_stream_name": "{instance_id}",
            "timezone": "UTC"
          }
        ]
      }
    }
  }
}
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
            _write_file_from_base64("/usr/local/bin/praktika-controller-start", launcher),
            "chmod 0755 /usr/local/bin/praktika-controller-start",
            _write_file_from_base64("/etc/praktika/amazon-cloudwatch-agent.json", cloudwatch),
            _write_file_from_base64("/etc/systemd/system/praktika-controller.service", unit),
            "systemctl daemon-reload || true",
        ],
    }


def _runtime_base_packages():
    return [
        "boto3",
        "PyJWT",
        "cryptography",
        "requests",
        "pytest>=7.0.0",
        _PRAKTIKA_BASE_WHL,
    ]


def _runtime_prebuilt_venvs():
    return [
        ImageBuilder.PrebuiltVenv(
            name=_RUNTIME_BASE_VENV,
            packages=_runtime_base_packages(),
            description=(
                "Shared Python base venv with pytest, Praktika runtime deps, and the published base Praktika wheel"
            ),
        ),
    ]


def _image_builders():
    ci_arm64_version = "1.0.11"
    ci_x86_64_version = "1.0.11"
    base_ci_arm64_version = "1.0.11"
    base_ci_x86_64_version = "1.0.11"

    return [
        ImageBuilder.Config(
            name="praktika-ci-arm64-image",
            image_recipe_name="praktika-ci-arm64-image-recipe",
            image_recipe_version=ci_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-controller-setup",
                    with_docker=True,
                ),
                _controller_runtime_component("praktika-controller-runtime"),
                _praktika_controller_component("praktika-controller"),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-ci-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-ci-arm64-imagebuilder-dist",
            ami_name="praktika-ci-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "controller", "arch": "arm64"},
            image_pipeline_name="praktika-ci-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-ci-arm64-image",
            image_recipe_name="praktika-base-ci-arm64-image-recipe",
            image_recipe_version=base_ci_arm64_version,
            inline_components=[
                _setup_component(
                    "praktika-base-controller-setup",
                    with_docker=True,
                ),
                _controller_runtime_component("praktika-base-controller-runtime"),
                _praktika_controller_component("praktika-base-controller"),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-ci-arm64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t4g.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-ci-arm64-imagebuilder-dist",
            ami_name="praktika-base-ci-arm64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_controller", "arch": "arm64"},
            image_pipeline_name="praktika-base-ci-arm64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-ci-x86_64-image",
            image_recipe_name="praktika-ci-x86_64-image-recipe",
            image_recipe_version=ci_x86_64_version,
            inline_components=[
                _setup_component(
                    "praktika-controller-setup",
                    with_docker=True,
                ),
                _controller_runtime_component("praktika-controller-runtime"),
                _praktika_controller_component("praktika-controller"),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-ci-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-ci-x86_64-imagebuilder-dist",
            ami_name="praktika-ci-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "controller", "arch": "x86_64"},
            image_pipeline_name="praktika-ci-x86_64-imagebuilder-pipeline",
        ),
        ImageBuilder.Config(
            name="praktika-base-ci-x86_64-image",
            image_recipe_name="praktika-base-ci-x86_64-image-recipe",
            image_recipe_version=base_ci_x86_64_version,
            inline_components=[
                _setup_component(
                    "praktika-base-controller-setup",
                    with_docker=True,
                ),
                _controller_runtime_component("praktika-base-controller-runtime"),
                _praktika_controller_component("praktika-base-controller"),
            ],
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            infrastructure_configuration_name="praktika-base-ci-x86_64-imagebuilder-infra",
            instance_profile_name=RUNNER_INSTANCE_PROFILE_NAME,
            instance_types=["t3.small"],
            vpc_name=CI_VPC_NAME,
            security_group_names=[f"{CI_VPC_NAME}-sg"],
            distribution_configuration_name="praktika-base-ci-x86_64-imagebuilder-dist",
            ami_name="praktika-base-ci-x86_64-{{ imagebuilder:buildDate }}",
            ami_tags={"praktika_resource_tag": "base_controller", "arch": "x86_64"},
            image_pipeline_name="praktika-base-ci-x86_64-imagebuilder-pipeline",
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}
_gh_token_minter = Components.GitHubTokenMinter()

_runner_pools = [
    Components.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-ci-arm64-image"],
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Add any host customization you need above this line.",
                "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
                (
                    f"/opt/praktika/base-venvs/{_RUNTIME_BASE_VENV}/bin/python "
                    f"-m pip install --force-reinstall {_PRAKTIKA_WHL}"
                ),
                "systemctl enable --now praktika-controller",
                "",
            ]
        ),
    ),
    Components.RunnerPool(
        name="arm-2xsmall-base",
        instance_type="t4g.small",
        vpc_name=CI_VPC_NAME,
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"],
    ),
    Components.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        vpc_name=CI_VPC_NAME,
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-ci-x86_64-image"],
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Add any host customization you need above this line.",
                "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
                (
                    f"/opt/praktika/base-venvs/{_RUNTIME_BASE_VENV}/bin/python "
                    f"-m pip install --force-reinstall {_PRAKTIKA_WHL}"
                ),
                "systemctl enable --now praktika-controller",
                "",
            ]
        ),
    ),
]

_orchestrator_pool = Components.OrchestratorPool(
    name="workflow-orchestrator",
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-ci-arm64-image"],
    user_data="\n".join(
        [
            "#!/usr/bin/env bash",
            "set -xeuo pipefail",
            "",
            "# Add any host customization you need above this line.",
            "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
            (
                f"/opt/praktika/base-venvs/{_RUNTIME_BASE_VENV}/bin/python "
                f"-m pip install --force-reinstall {_PRAKTIKA_WHL}"
            ),
            "systemctl enable --now praktika-controller",
            "",
        ]
    ),
)

_orchestrator_pool_base = Components.OrchestratorPool(
    name="workflow-orchestrator-base",
    instance_type="t4g.small",
    vpc_name=CI_VPC_NAME,
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    image_builder=_IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"],
)

_cidb_cluster = Components.CIDBCluster(
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
            Components.report_page_config,
        ],
        image_builders=_IMAGE_BUILDERS,
        github_token_minters=[_gh_token_minter],
        orchestrator_pools=[_orchestrator_pool, _orchestrator_pool_base],
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
