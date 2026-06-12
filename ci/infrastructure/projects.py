from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import Components, Storage, VPC


_PRAKTIKA_BASE_VERSION = "0.0.1"
_PRAKTIKA_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika-0.1.2-py3-none-any.whl"
_PRAKTIKA_CONTROLLER_WHL = "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/praktika_controller-0.1.1-py3-none-any.whl"
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
    ci_version = "1.0.2"
    ubuntu_ci_version = "1.0.2"

    return [
        _create_awslinux_image_builder_config(
            name="ci-arm64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t4g.small"],
        ),
        _create_awslinux_image_builder_config(
            name="ci-x86_64-image",
            version=ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t3.small"],
        ),
        _create_ubuntu_image_builder_config(
            name="ci-ubuntu-x86_64-image",
            version=ubuntu_ci_version,
            controller_package=_PRAKTIKA_CONTROLLER_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            components=_custom_image_tests(),
            instance_types=["t3.small"],
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}

_runner_pools = [
    Components.RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
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
    ),
    Components.RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=10,
        image_builder=_IMAGE_BUILDERS_BY_NAME["ci-x86_64-image"],
        user_data="\n".join(
            [
                "#!/usr/bin/env bash",
                "set -xeuo pipefail",
                "",
                "# Update the controller if changed (to test new version w/o inage rebuild)",
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
    capacity_reserve=2,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
    user_data="\n".join(
        [
            "#!/usr/bin/env bash",
            "set -xeuo pipefail",
            "",
            "# Update the controller if changed (to test new version w/o inage rebuild)",
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
        min_praktika_version="0.1.2",
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
        github_token_minters=[Components.GitHubTokenMinter()],
        orchestrator_pools=[_orchestrator_pool, _orchestrator_pool_base],
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
