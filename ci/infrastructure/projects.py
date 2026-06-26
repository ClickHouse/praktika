from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import Components, Storage, VPC
from ci.settings.settings import SECRET_CI_DB_CONNECTION


_PRAKTIKA_PACKAGE_BASE_URL = (
    "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages"
)
_PRAKTIKA_BASE_VERSION = "0.1.4"
# The latest praktika wheel is published to a fixed, version-less S3 location so
# that runner/orchestrator user-data and image recipes never need editing on a
# version bump. The "0.0.0" is a placeholder: pip requires a PEP 440-valid
# version in the wheel *filename*, but installs the real version from the
# wheel's dist-info metadata. Both publish scripts mirror the freshly built
# wheel to this key (ci/scripts/publish_wheel.sh,
# ci/scripts/build_and_publish_wheels.sh).
_PRAKTIKA_LATEST_WHL_NAME = "praktika-0.0.0-py3-none-any.whl"
_PRAKTIKA_WHL = f"{_PRAKTIKA_PACKAGE_BASE_URL}/latest/{_PRAKTIKA_LATEST_WHL_NAME}"

_PRAKTIKA_CONTROLLER_BASE_VERSION = "0.1.1"
_PRAKTIKA_CONTROLLER_BASE_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/"
    f"praktika_controller-{_PRAKTIKA_CONTROLLER_BASE_VERSION}-py3-none-any.whl"
)
# The latest controller wheel uses the same fixed, version-less "latest" S3
# location as praktika (see _PRAKTIKA_LATEST_WHL_NAME above), so user-data never
# needs editing on a controller version bump. Both publish scripts mirror the
# freshly built controller wheel to this key.
_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME = "praktika_controller-0.0.0-py3-none-any.whl"
_PRAKTIKA_CONTROLLER_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/latest/{_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME}"
)
_RUNTIME_BASE_VENV = "praktika-runtime"


def _component_factory(*names):
    for name in names:
        factory = getattr(Components, name, None)
        if factory:
            return factory
    raise AttributeError(f"Components has none of these factories: {names}")


_create_praktika_venv_config = _component_factory(
    "create_praktika_venv_config",
    "praktika_venv_config",
)
_create_awslinux_image_builder_config = _component_factory(
    "create_awslinux_image_builder_config",
    "image_builder_config",
)
_create_ubuntu_image_builder_config = _component_factory(
    "create_ubuntu_image_builder_config",
    "ubuntu_image_builder_config",
)


def _runtime_prebuilt_venvs():
    return [
        _create_praktika_venv_config(
            _RUNTIME_BASE_VENV,
            _PRAKTIKA_BASE_VERSION,
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
    ci_version = "1.0.11"
    ubuntu_ci_version = "1.0.8"

    return [
        _create_awslinux_image_builder_config(
            name="ci-arm64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t4g.small"],
        ),
        _create_awslinux_image_builder_config(
            name="ci-x86_64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t3.small"],
        ),
        _create_ubuntu_image_builder_config(
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

_orchestrator_pool = Components.OrchestratorPool(
    name="workflow-orchestrator",
    instance_type="t4g.small",
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    capacity_reserve=0,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
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
)

_cidb_cluster = Components.CIDBCluster(
    instance_type="t4g.large",
    size=1,
)

PROJECTS = [
    CloudInfrastructure.Config(
        name="praktika",
        min_praktika_version="0.1.4",
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
