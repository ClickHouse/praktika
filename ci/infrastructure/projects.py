from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import Components, ImageBuilder, Storage, VPC
from ci.settings.settings import SECRET_CI_DB_CONNECTION


_PRAKTIKA_PACKAGE_BASE_URL = (
    "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages"
)
_PRAKTIKA_BASE_VERSION = "0.1.6"
# The baked AMI venv pins an exact Praktika version so image builds are
# reproducible and a version bump forces a fresh AMI (see _image_builders).
_PRAKTIKA_BASE_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/praktika-{_PRAKTIKA_BASE_VERSION}-py3-none-any.whl"
)
# The latest praktika wheel is published to a fixed, version-less S3 location so
# that runner/orchestrator user-data and image recipes never need editing on a
# version bump. The "0.0.0" is a placeholder: pip requires a PEP 440-valid
# version in the wheel *filename*, but installs the real version from the
# wheel's dist-info metadata. MainCI publish scripts also mirror the same wheel
# to the major.minor compat version for external consumers that want a BC branch
# alias.
_PRAKTIKA_LATEST_WHL_NAME = "praktika-0.0.0-py3-none-any.whl"
_PRAKTIKA_WHL = f"{_PRAKTIKA_PACKAGE_BASE_URL}/latest/{_PRAKTIKA_LATEST_WHL_NAME}"

_PRAKTIKA_CONTROLLER_BASE_VERSION = "0.1.3"
_PRAKTIKA_CONTROLLER_BASE_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/"
    f"praktika_controller-{_PRAKTIKA_CONTROLLER_BASE_VERSION}-py3-none-any.whl"
)
# The latest controller wheel uses the same fixed, version-less "latest" S3
# location as praktika. MainCI publish scripts also mirror it to a major.minor
# compat version.
_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME = "praktika_controller-0.0.0-py3-none-any.whl"
_PRAKTIKA_CONTROLLER_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/latest/{_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME}"
)
_RUNTIME_BASE_VENV = "praktika-runtime"


def _runtime_prebuilt_venvs():
    # The `infrastructure` extra pulls Praktika's runtime deps
    # (boto3/PyJWT/cryptography/requests) automatically; pytest and the Bedrock
    # AI SDK are optional extras the runner/orchestrator need, so list them
    # explicitly. The orchestrator's AI advisor (AI_PROVIDER="bedrock") imports
    # `anthropic[bedrock]` lazily at decide() time; baking it into this shared
    # venv keeps it present on every AMI — including the base pool, which has no
    # boot-time user_data to pip-install into (harmless on job runners).
    return [
        ImageBuilder.PrebuiltVenv(
            name=_RUNTIME_BASE_VENV,
            packages=[
                "pytest>=7.0.0",
                "pytest-reportlog>=0.4.0",
                "anthropic[bedrock]",
                f"praktika[infrastructure] @ {_PRAKTIKA_BASE_WHL}",
            ],
            description=(
                "Shared Python base venv: Praktika (+infrastructure extra), "
                "pytest, and the Bedrock AI SDK"
            ),
        ),
    ]


def _custom_image_tests():
    return [
        Components.create_image_test_component(
            name="praktika-project-image-test",
            commands=[
                "test -d /opt/praktika/work",
                "test -w /opt/praktika/work",
            ],
        ),
    ]


def _image_builders():
    # Bump on any change to the baked venv contents (see _runtime_prebuilt_venvs)
    # so Image Builder produces a fresh AMI.
    ci_version = "1.0.13"
    ubuntu_ci_version = "1.0.10"

    return [
        Components.create_awslinux_image_builder_config(
            name="ci-arm64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t4g.small"],
        ),
        Components.create_awslinux_image_builder_config(
            name="ci-x86_64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t3.small"],
        ),
        Components.create_ubuntu_image_builder_config(
            name="ci-ubuntu-x86_64-image",
            version=ubuntu_ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            components=_custom_image_tests(),
            instance_types=["t3.small"],
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}
_RUNNER_ALLOWED_SSM_PARAMETERS = [SECRET_CI_DB_CONNECTION]
_RUNNER_ALLOWED_SECRETS = []
_RUNNER_ALLOWED_S3_PREFIXES = ["artifacts-eu-north-1"]
_RUNNER_ALLOW_ALL_SSM_PARAMETERS = False
_RUNNER_ALLOW_ALL_SECRETS = False
_RUNNER_ALLOW_ALL_S3_PREFIXES = False
_RUNNER_ALLOW_SSM_DEBUG = False

_runner_pools = [
    Components.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
        allowed_ssm_parameters=list(_RUNNER_ALLOWED_SSM_PARAMETERS),
        allowed_secrets=list(_RUNNER_ALLOWED_SECRETS),
        allowed_s3_prefixes=list(_RUNNER_ALLOWED_S3_PREFIXES),
        allow_all_ssm_parameters=_RUNNER_ALLOW_ALL_SSM_PARAMETERS,
        allow_all_secrets=_RUNNER_ALLOW_ALL_SECRETS,
        allow_all_s3_prefixes=_RUNNER_ALLOW_ALL_S3_PREFIXES,
        allow_ssm_debug=_RUNNER_ALLOW_SSM_DEBUG,
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Update the controller if changed (to test new version w/o image rebuild)",
                f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
                "# Add any host customization you need above this line.",
                "/usr/local/bin/praktika-configure-cloudwatch-agent",
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
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
        allowed_ssm_parameters=list(_RUNNER_ALLOWED_SSM_PARAMETERS),
        allowed_secrets=list(_RUNNER_ALLOWED_SECRETS),
        allowed_s3_prefixes=list(_RUNNER_ALLOWED_S3_PREFIXES),
        allow_all_ssm_parameters=_RUNNER_ALLOW_ALL_SSM_PARAMETERS,
        allow_all_secrets=_RUNNER_ALLOW_ALL_SECRETS,
        allow_all_s3_prefixes=_RUNNER_ALLOW_ALL_S3_PREFIXES,
        allow_ssm_debug=_RUNNER_ALLOW_SSM_DEBUG,
    ),
    Components.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-x86_64-image"],
        allowed_ssm_parameters=list(_RUNNER_ALLOWED_SSM_PARAMETERS),
        allowed_secrets=list(_RUNNER_ALLOWED_SECRETS),
        allowed_s3_prefixes=list(_RUNNER_ALLOWED_S3_PREFIXES),
        allow_all_ssm_parameters=_RUNNER_ALLOW_ALL_SSM_PARAMETERS,
        allow_all_secrets=_RUNNER_ALLOW_ALL_SECRETS,
        allow_all_s3_prefixes=_RUNNER_ALLOW_ALL_S3_PREFIXES,
        allow_ssm_debug=_RUNNER_ALLOW_SSM_DEBUG,
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Update the controller if changed (to test new version w/o image rebuild)",
                f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
                "# Add any host customization you need above this line.",
                "/usr/local/bin/praktika-configure-cloudwatch-agent",
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
        name="amd-2xsmall-ubuntu",
        instance_type="t3.small",
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-ubuntu-x86_64-image"],
        allowed_ssm_parameters=list(_RUNNER_ALLOWED_SSM_PARAMETERS),
        allowed_secrets=list(_RUNNER_ALLOWED_SECRETS),
        allowed_s3_prefixes=list(_RUNNER_ALLOWED_S3_PREFIXES),
        allow_all_ssm_parameters=_RUNNER_ALLOW_ALL_SSM_PARAMETERS,
        allow_all_secrets=_RUNNER_ALLOW_ALL_SECRETS,
        allow_all_s3_prefixes=_RUNNER_ALLOW_ALL_S3_PREFIXES,
        allow_ssm_debug=_RUNNER_ALLOW_SSM_DEBUG,
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Add any host customization you need above this line.",
                "/usr/local/bin/praktika-configure-cloudwatch-agent",
                "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
                "# Update the controller if changed (to test new version w/o image rebuild)",
                f"python3.12 -m pip install --ignore-installed {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
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

# The AI advisor (AI_PROVIDER="bedrock") reaches Claude through Bedrock Runtime,
# which the orchestrator's instance role must be allowed to invoke. Both
# orchestrator pools share one role and get the grant (the base pool doesn't run
# the advisor today, but the permission is harmless there).
_ORCHESTRATOR_BEDROCK_IAM_STATEMENT = {
    "Sid": "BedrockRuntimeInference",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": "*",
}

_orchestrator_pool = Components.OrchestratorPool(
    name="workflow-orchestrator",
    instance_type="t4g.small",
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    capacity_reserve=0,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
    ext={
        "iam_statements": [_ORCHESTRATOR_BEDROCK_IAM_STATEMENT],
        "external_pr_autoapprove_paths": [
            #"**/*"
            "praktika/*"
        ],
    },
    user_data="\n".join(
        [
            "#!/usr/bin/env bash",
            "set -xeuo pipefail",
            "",
            "# Update the controller if changed (to test new version w/o image rebuild)",
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
            "# Add any host customization you need above this line.",
            "/usr/local/bin/praktika-configure-cloudwatch-agent",
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
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    capacity_reserve=2,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
    ext={"iam_statements": [_ORCHESTRATOR_BEDROCK_IAM_STATEMENT]},
)

_cidb_cluster = Components.CIDBCluster(
    instance_type="t4g.large",
    size=1,
)

PROJECTS = [
    CloudInfrastructure.Config(
        name="praktika",
        min_praktika_version="0.1.6",
        vpcs=[
            VPC.Config(
                subnets=[
                    VPC.Subnet(availability_zone="eu-north-1a"),
                ],
            )
        ],
        storages=[
            Storage.Config(name="artifacts-eu-north-1", retention_days=90, public=True),
        ],
        report_pages=[
            Components.report_page_config,
        ],
        image_builders=_IMAGE_BUILDERS,
        github_token_minters=[Components.GitHubTokenMinter(secret_name="gh-app-echt")],
        orchestrator_pools=[_orchestrator_pool, _orchestrator_pool_base],
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
