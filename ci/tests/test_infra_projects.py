import base64
from types import SimpleNamespace

import pytest

from ci.infrastructure.projects import (
    _IMAGE_BUILDERS_BY_NAME,
    _orchestrator_pool,
    _orchestrator_pool_base,
    _runner_pools,
)
from praktika.mangle import _get_infra_config
from praktika.settings import Settings
from praktika.infrastructure import Components, ImageBuilder, Storage, VPC
from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool
from praktika.infrastructure.secret_parameter import SecretParameter
from praktika.infrastructure.sqs_queue import SQSQueue
from praktika.validator import Validator
from praktika.version import current_praktika_version
from ci.settings.settings import RunnerLabels


def _decode_embedded_file(command: str) -> str:
    payload = command.split("'")[3]
    return base64.b64decode(payload).decode("utf-8")


def test_get_infra_config_requires_project_when_multiple(tmp_path, monkeypatch):
    config_path = tmp_path / "cloud.py"
    config_path.write_text(
        "\n".join(
            [
                "from praktika.infrastructure.cloud import CloudInfrastructure",
                "PROJECTS = [",
                "    CloudInfrastructure.Config(name='alpha'),",
                "    CloudInfrastructure.Config(name='beta'),",
                "]",
            ]
        )
    )
    monkeypatch.setattr(Settings, "CLOUD_INFRASTRUCTURE_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="Use --project NAME"):
        _get_infra_config()

    assert _get_infra_config("beta").name == "beta"


def test_deploy_rejects_config_that_requires_newer_praktika(monkeypatch):
    current_version = current_praktika_version()
    cloud = CloudInfrastructure.Config(
        name="future",
        min_praktika_version="999.0.0",
    )
    monkeypatch.setattr(
        cloud,
        "_verify_account",
        lambda: (_ for _ in ()).throw(
            AssertionError("account check should not run on version mismatch")
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cloud.deploy()

    message = str(exc.value)
    assert "requires a newer Praktika runtime" in message
    assert "Config min_praktika_version: 999.0.0" in message
    assert f"Running Praktika version: {current_version}" in message
    assert "python3 -m praktika infrastructure --deploy" in message


def test_current_infrastructure_config_imports_all_component_groups(monkeypatch):
    monkeypatch.setattr(
        Settings,
        "CLOUD_INFRASTRUCTURE_CONFIG_PATH",
        "ci/infrastructure/projects.py",
    )

    cloud = _get_infra_config("praktika")

    assert cloud.min_praktika_version == current_praktika_version()
    assert cloud.vpcs
    assert cloud.storages
    assert cloud.report_pages
    assert cloud.image_builders
    assert cloud.runner_pools
    assert cloud.github_token_minters
    assert cloud.orchestrator_pools
    assert cloud.cidb_cluster
    assert cloud.lambda_functions
    assert cloud.iam_roles
    assert cloud.iam_instance_profiles
    assert cloud.secret_parameters
    assert cloud.launch_templates
    assert cloud.autoscaling_groups
    assert cloud.sqs_queues
    assert cloud.pool_autoscalers
    assert any(pool.capacity_reserve == 2 for pool in cloud.orchestrator_pools)
    image_builders = {builder.name: builder for builder in cloud.image_builders}
    assert {
        name: builder.instance_profile_name for name, builder in image_builders.items()
    } == {
        "praktika-ci-arm64-image": "praktika-arm-2xsmall-profile",
        "praktika-ci-x86_64-image": "praktika-amd-2xsmall-profile",
        "praktika-ci-ubuntu-x86_64-image": "praktika-amd-2xsmall-ubuntu-profile",
    }
    assert {builder.vpc_name for builder in image_builders.values()} == {"praktika-vpc"}
    assert {
        tuple(builder.security_group_names) for builder in image_builders.values()
    } == {("praktika-vpc-sg",)}
    assert cloud.cidb_cluster.vpc_name == "praktika-vpc"
    assert cloud.cidb_cluster.security_group_names == ["praktika-vpc-sg"]


def test_project_component_factory_accepts_previous_factory_names(monkeypatch):
    from ci.infrastructure import projects

    class _OldComponents:
        image_builder_config = object()

    monkeypatch.setattr(projects, "Components", _OldComponents)

    assert (
        projects._component_factory(
            "create_awslinux_image_builder_config",
            "image_builder_config",
        )
        is _OldComponents.image_builder_config
    )


def test_infrastructure_deploy_validation_accepts_matching_s3_settings(monkeypatch):
    bucket = "silk-artifacts-eu-north-1"
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", f"{bucket}/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {bucket: f"{bucket}.s3.amazonaws.com"},
    )

    Validator.validate_infrastructure_deploy(cloud)


def test_infrastructure_deploy_validation_rejects_missing_s3_endpoint(
    monkeypatch, capsys
):
    bucket = "silk-artifacts-eu-north-1"
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", f"{bucket}/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {"silk-artifacts": "silk-artifacts.s3.amazonaws.com"},
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    assert (
        f"S3_BUCKET_TO_HTTP_ENDPOINT must include bucket [{bucket}]"
        in capsys.readouterr().out
    )


def test_infrastructure_deploy_validation_rejects_storage_bucket_mismatch(
    monkeypatch, capsys
):
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", "silk-artifacts")
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", "silk-artifacts")
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", "silk-artifacts/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {"silk-artifacts": "silk-artifacts.s3.amazonaws.com"},
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert "Setting S3_ARTIFACT_BUCKET bucket [silk-artifacts]" in out
    assert "silk-artifacts-eu-north-1" in out


def test_infrastructure_deploy_validation_rejects_prefixed_resource_names(capsys):
    cloud = CloudInfrastructure.Config(
        name="silk",
        vpcs=[
            VPC.Config(
                name="silk-ci",
                subnets=[VPC.Subnet(availability_zone="eu-north-1a")],
            )
        ],
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert (
        "Infrastructure vpcs item name [silk-ci] already includes project prefix [silk]"
        in out
    )


def test_infrastructure_deploy_validation_accepts_base_venv_name(monkeypatch):
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-image",
                prebuilt_venvs=[ImageBuilder.PrebuiltVenv(name="praktika-runtime")],
            )
        ],
    )

    monkeypatch.setattr(Settings, "PRAKTIKA_BASE_VENV", "praktika-runtime")

    Validator.validate_infrastructure_deploy(cloud)


def test_infrastructure_deploy_validation_rejects_missing_base_venv(
    monkeypatch, capsys
):
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-image",
                prebuilt_venvs=[ImageBuilder.PrebuiltVenv(name="other-runtime")],
            )
        ],
    )

    monkeypatch.setattr(Settings, "PRAKTIKA_BASE_VENV", "praktika-runtime")

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert "Setting PRAKTIKA_BASE_VENV [praktika-runtime]" in out
    assert "other-runtime" in out


def test_cloud_config_prefixes_embedded_pool_resources():
    cloud = CloudInfrastructure.Config(
        name="sandbox",
        image_builders=[],
        vpcs=[
            VPC.Config(
                name="praktika-ci",
                subnets=[
                    VPC.Subnet(availability_zone="eu-north-1a"),
                ],
            )
        ],
        runner_pools=[
            RunnerPool(
                name="arm-2xsmall",
                instance_type="t4g.small",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            )
        ],
        orchestrator_pool=OrchestratorPool(
            instance_type="t4g.small",
            scaling=OrchestratorPool.Scaling.Auto,
            size=0,
            max_size=1,
        ),
        orchestrator_pools=[
            OrchestratorPool(
                name="workflow-orchestrator-base",
                instance_type="t4g.small",
                scaling=OrchestratorPool.Scaling.Auto,
                size=0,
                max_size=1,
            )
        ],
    )

    runner = cloud.runner_pools[0]
    orchestrator = cloud.orchestrator_pool
    base_orchestrator = next(
        pool
        for pool in cloud.orchestrator_pools
        if pool.name == "workflow-orchestrator-base"
    )

    assert runner.queue.name == "sandbox-arm-2xsmall"
    assert runner.ec2_role.name == "sandbox-arm-2xsmall-role"
    assert runner.instance_profile.name == "sandbox-arm-2xsmall-profile"
    assert runner.instance_profile.role_name == "sandbox-arm-2xsmall-role"
    assert runner.launch_template.name == "sandbox-arm-2xsmall-lt"
    assert runner.launch_template.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert runner.autoscaling_group.name == "sandbox-arm-2xsmall"
    assert runner.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.tags["praktika_role"] == "job_runner"
    assert runner.launch_template.tags["praktika_queue"] == runner.queue.name
    assert runner.launch_template.tags["praktika_project_slug"] == "sandbox"
    assert all(
        stmt.get("Sid") != "SecretsManagerRead"
        for stmt in runner.ec2_role.inline_policies["RunnerAccess"]["Statement"]
    )
    assert "praktika-configure-cloudwatch-agent" in runner.launch_template.user_data
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in runner.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller" in runner.launch_template.user_data
    )

    assert orchestrator is not None
    assert orchestrator.queue.name == "sandbox-workflow-orchestrator"
    assert orchestrator.ec2_role.name == "sandbox-workflow-orchestrator-role"
    assert orchestrator.instance_profile.name == "sandbox-workflow-orchestrator-profile"
    assert (
        orchestrator.instance_profile.role_name == "sandbox-workflow-orchestrator-role"
    )
    assert orchestrator.launch_template.name == "sandbox-workflow-orchestrator-lt"
    assert orchestrator.launch_template.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.launch_template.security_group_names == [
        "sandbox-praktika-ci-sg"
    ]
    assert orchestrator.autoscaling_group.name == "sandbox-workflow-orchestrator"
    assert orchestrator.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.lambda_config.name == "sandbox-workflow-orchestrator"
    assert orchestrator.lambda_config.role_name == "sandbox-gh-webhook-role"
    assert orchestrator.webhook_secret.name == "sandbox-gh-webhook-secret"
    assert orchestrator.launch_template.tags["praktika_role"] == "workflow_orchestrator"
    assert orchestrator.launch_template.tags["praktika_project_slug"] == "sandbox"
    assert all(
        stmt.get("Sid") != "SecretsManagerRead"
        for stmt in orchestrator.ec2_role.inline_policies["WorkflowOrchestratorAccess"][
            "Statement"
        ]
    )
    assert (
        "praktika-configure-cloudwatch-agent" in orchestrator.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in orchestrator.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in orchestrator.launch_template.user_data
    )
    assert (
        base_orchestrator.lambda_config.environments["SQS_QUEUE_NAME"]
        == "sandbox-workflow-orchestrator-base"
    )
    assert (
        base_orchestrator.autoscaling_group.tags["praktika_queue"]
        == "sandbox-workflow-orchestrator-base"
    )


def test_runner_pools_get_distinct_roles_and_profiles():
    cloud = CloudInfrastructure.Config(
        name="sandbox",
        image_builders=[],
        runner_pools=[
            RunnerPool(
                name="arm-2xsmall",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            ),
            RunnerPool(
                name="arm-2xsmall-base",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            ),
        ],
    )

    roles = {pool.ec2_role.name for pool in cloud.runner_pools}
    profiles = {pool.instance_profile.name for pool in cloud.runner_pools}

    assert roles == {
        "sandbox-arm-2xsmall-role",
        "sandbox-arm-2xsmall-base-role",
    }
    assert profiles == {
        "sandbox-arm-2xsmall-profile",
        "sandbox-arm-2xsmall-base-profile",
    }


def test_runner_pool_accepts_custom_role_and_profile_configs():
    role = IAMRole.Config(
        name="custom-runner-role",
        trust_service="ec2.amazonaws.com",
    )
    profile = IAMInstanceProfile.Config(
        name="custom-runner-profile",
        role_name="custom-runner-role",
    )

    pool = RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        ec2_role=role,
        instance_profile=profile,
    )

    assert pool.ec2_role is role
    assert pool.instance_profile is profile
    assert pool.launch_template.iam_instance_profile_name == "custom-runner-profile"


def test_cloud_config_prefixes_all_top_level_resource_types():
    cloud = CloudInfrastructure.Config(
        name="prefix-check",
        vpcs=[
            VPC.Config(
                name="ci",
                subnets=[VPC.Subnet(availability_zone="eu-north-1a")],
            )
        ],
        storages=[Storage.Config(name="artifacts", retention_days=7)],
        iam_roles=[
            IAMRole.Config(name="worker-role", trust_service="lambda.amazonaws.com")
        ],
        iam_instance_profiles=[
            IAMInstanceProfile.Config(name="worker-profile", role_name="worker-role")
        ],
        secret_parameters=[
            SecretParameter.Config(name="app-secret"),
            SecretParameter.Config(name="/path-secret"),
        ],
        sqs_queues=[SQSQueue.Config(name="jobs")],
        launch_templates=[
            LaunchTemplate.Config(
                name="runner-lt",
                image_id="ami-1234567890abcdef0",
                instance_type="t4g.small",
                vpc_name="ci",
                iam_instance_profile_name="worker-profile",
                security_group_names=["ci-sg"],
            )
        ],
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="runner",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="runner-lt",
            )
        ],
        lambda_functions=[
            Lambda.Config(
                name="worker",
                path=__file__,
                handler="handler.main",
                role_name="worker-role",
                secrets={"app-secret": "APP_SECRET", "/path-secret": "PATH_SECRET"},
            )
        ],
        image_builders=[
            ImageBuilder.Config(
                name="builder",
                instance_profile_name="worker-profile",
                vpc_name="ci",
                security_group_names=["ci-sg"],
                inline_components=[
                    {
                        "name": "builder-setup",
                        "platform": "Linux",
                        "commands": [
                            "test -x /opt/praktika/base-venvs/runtime/bin/python"
                        ],
                    }
                ],
                prebuilt_venvs=[
                    ImageBuilder.PrebuiltVenv(name="runtime", packages=["requests"])
                ],
            )
        ],
    )

    vpc = cloud.vpcs[0]
    storage = cloud.storages[0]
    role = cloud.iam_roles[0]
    profile = cloud.iam_instance_profiles[0]
    secret = cloud.secret_parameters[0]
    path_secret = cloud.secret_parameters[1]
    queue = cloud.sqs_queues[0]
    lt = cloud.launch_templates[0]
    asg = cloud.autoscaling_groups[0]
    lambda_cfg = cloud.lambda_functions[0]
    builder = cloud.image_builders[0]

    assert vpc.name == "prefix-check-ci"
    assert storage.name == "prefix-check-artifacts"
    assert role.name == "prefix-check-worker-role"
    assert profile.name == "prefix-check-worker-profile"
    assert profile.role_name == "prefix-check-worker-role"
    assert secret.name == "prefix-check-app-secret"
    assert path_secret.name == "/prefix-check-path-secret"
    assert queue.name == "prefix-check-jobs"
    assert lt.name == "prefix-check-runner-lt"
    assert lt.vpc_name == "prefix-check-ci"
    assert lt.iam_instance_profile_name == "prefix-check-worker-profile"
    assert lt.security_group_names == ["prefix-check-ci-sg"]
    assert asg.name == "prefix-check-runner"
    assert asg.vpc_name == "prefix-check-ci"
    assert asg.launch_template_name == "prefix-check-runner-lt"
    assert lambda_cfg.name == "prefix-check-worker"
    assert lambda_cfg.role_name == "prefix-check-worker-role"
    assert lambda_cfg.secrets == {
        "prefix-check-app-secret": "APP_SECRET",
        "/prefix-check-path-secret": "PATH_SECRET",
    }
    assert builder.name == "prefix-check-builder"
    assert builder.image_recipe_name == "prefix-check-builder-recipe"
    assert (
        builder.infrastructure_configuration_name
        == "prefix-check-builder-imagebuilder-infra"
    )
    assert (
        builder.distribution_configuration_name
        == "prefix-check-builder-imagebuilder-dist"
    )
    assert builder.image_pipeline_name == "prefix-check-builder-imagebuilder-pipeline"
    assert builder.ami_name == "prefix-check-builder-{{ imagebuilder:buildDate }}"
    assert builder.instance_profile_name == "prefix-check-worker-profile"
    assert builder.vpc_name == "prefix-check-ci"
    assert builder.security_group_names == ["prefix-check-ci-sg"]
    assert builder.inline_components[0]["name"] == "prefix-check-builder-setup"
    assert builder.inline_components[0]["commands"] == [
        "test -x /opt/praktika/base-venvs/runtime/bin/python"
    ]
    assert builder.prebuilt_venvs[0].name == "runtime"


def test_cloud_project_namespace_keeps_image_builder_venv_paths_project_local():
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-ubuntu-x86_64-image",
                inline_components=[
                    {
                        "name": "praktika-controller-ubuntu-image-test",
                        "platform": "Linux",
                        "phase": "test",
                        "commands": [
                            "test -x /opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python",
                            "/opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python -m pip show praktika",
                        ],
                    }
                ],
                prebuilt_venvs=[
                    ImageBuilder.PrebuiltVenv(name="praktika-runtime-0.1.2")
                ],
            )
        ],
    )

    builder = cloud.image_builders[0]

    assert builder.prebuilt_venvs[0].name == "praktika-runtime-0.1.2"
    assert builder.inline_components[0]["commands"] == [
        "test -x /opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python",
        "/opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python -m pip show praktika",
    ]


def test_cloud_project_namespace_does_not_rewrite_controller_local_paths():
    builder = Components.create_ubuntu_image_builder_config(
        name="ci-arm64-image",
        version="1.0.0",
        controller_package=(
            "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/"
            "packages/praktika_controller-0.1.1-py3-none-any.whl"
        ),
        prebuilt_venvs=[
            Components.create_praktika_venv_config("praktika-runtime-0.1.2", "0.1.2")
        ],
        instance_types=["t4g.small"],
    )

    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
        image_builders=[builder],
    )
    builder = cloud.image_builders[0]
    runtime_component = next(
        component
        for component in builder.inline_components
        if component["description"]
        == "Install Praktika controller runtime dependencies into the image"
    )
    controller_component = next(
        component
        for component in builder.inline_components
        if component["description"] == "Bake the Praktika controller service into the image"
    )
    runtime_commands = "\n".join(runtime_component["commands"])
    commands = "\n".join(controller_component["commands"])
    unit = _decode_embedded_file(
        next(
            cmd
            for cmd in controller_component["commands"]
            if "/etc/systemd/system/praktika-controller.service" in cmd
        )
    )

    assert runtime_component["name"] == "silk-praktika-controller-ubuntu-runtime"
    assert "https://praktika-artifacts-eu-north-1.s3.amazonaws.com" in runtime_commands
    assert "praktika-silk-artifacts-eu-north-1" not in runtime_commands
    assert controller_component["name"] == "silk-praktika-controller"
    assert "/usr/local/bin/praktika-controller-start" in commands
    assert "/etc/systemd/system/praktika-controller.service" in commands
    assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
    assert "silk-praktika-controller-start" not in commands
    assert "silk-praktika-controller.service" not in commands


def test_cloud_deploy_runs_lambdas_before_image_backed_compute(monkeypatch):
    calls = []

    cloud = CloudInfrastructure.Config(
        name="deploy-order",
        lambda_functions=[
            Lambda.Config(
                name="webhook",
                path=__file__,
                handler="handler.main",
                role_name="lambda-role",
            )
        ],
        image_builders=[
            ImageBuilder.Config(
                name="builder",
            )
        ],
        launch_templates=[
            LaunchTemplate.Config(
                name="runner-lt",
                image_id="ami-1234567890abcdef0",
                instance_type="t4g.small",
            )
        ],
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="runner-asg",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="runner-lt",
            )
        ],
    )
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1", EVENT_FEED_S3_PATH="")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    monkeypatch.setattr(
        cloud.lambda_functions[0],
        "deploy",
        lambda: calls.append(f"lambda:{cloud.lambda_functions[0].name}"),
    )
    monkeypatch.setattr(
        cloud.image_builders[0],
        "deploy",
        lambda: calls.append(f"imagebuilder:{cloud.image_builders[0].name}"),
    )
    monkeypatch.setattr(
        cloud.launch_templates[0],
        "deploy",
        lambda: calls.append(f"lt:{cloud.launch_templates[0].name}"),
    )
    monkeypatch.setattr(
        cloud.autoscaling_groups[0],
        "deploy",
        lambda: calls.append(f"asg:{cloud.autoscaling_groups[0].name}"),
    )

    cloud.deploy()

    assert calls == [
        "lambda:deploy-order-webhook",
        "imagebuilder:deploy-order-builder",
        "lt:deploy-order-runner-lt",
        "asg:deploy-order-runner-asg",
    ]


def test_cloud_deploy_prints_deferred_asg_warning_at_end(monkeypatch, capsys):
    cloud = CloudInfrastructure.Config(
        name="praktika",
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="workflow-orchestrator",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="workflow-orchestrator-lt",
            )
        ],
    )
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1", EVENT_FEED_S3_PATH="")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    def _defer_asg():
        cloud.autoscaling_groups[0].ext.update(
            {
                "deferred_missing_launch_template": True,
                "deployment_warning": (
                    "Launch Template is not available yet for ASG "
                    "'praktika-workflow-orchestrator'; skipping until the launch template exists"
                ),
            }
        )

    monkeypatch.setattr(cloud.autoscaling_groups[0], "deploy", _defer_asg)

    cloud.deploy(only=["ASG"])

    output = capsys.readouterr().out.rstrip()
    assert "WARNING: Infrastructure deployment completed with warnings" in output
    assert (
        "WARNING: Launch Template is not available yet for ASG "
        "'praktika-workflow-orchestrator'; skipping until the launch template exists"
    ) in output
    assert output.endswith(
        "WARNING: Rerun is required after the missing launch template exists."
    )


def test_controller_image_builders_are_declared():
    for name, ami_name, instance_type in [
        ("ci-arm64-image", "ci-arm64-{{ imagebuilder:buildDate }}", "t4g.small"),
        ("ci-x86_64-image", "ci-x86_64-{{ imagebuilder:buildDate }}", "t3.small"),
    ]:
        builder = _IMAGE_BUILDERS_BY_NAME[name]

        assert builder.ami_launch_permission == {}
        assert builder.ami_name == ami_name
        assert builder.instance_types == [instance_type]
        assert [component["name"] for component in builder.inline_components] == [
            "praktika-controller-setup",
            "praktika-controller-runtime",
            "praktika-controller",
        ]
        runtime_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-controller-runtime"
        )
        assert any(
            "amazon-cloudwatch-agent" in cmd
            for cmd in builder.inline_components[0]["commands"]
        )
        assert any(
            "praktika_controller-0.1.1-py3-none-any.whl" in cmd
            for cmd in runtime_component["commands"]
        )
        assert builder.prebuilt_venvs[0].name == "praktika-runtime"
        assert {
            "boto3",
            "PyJWT",
            "cryptography",
            "requests",
            "pytest>=7.0.0",
        }.issubset(builder.prebuilt_venvs[0].packages)
        assert (
            _IMAGE_BUILDERS_BY_NAME[name]
            .prebuilt_venvs[0]
            .packages[-1]
            .endswith("/praktika-0.0.1-py3-none-any.whl")
        )
        agent_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-controller"
        )
        launcher = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "praktika-controller-start" in cmd and "printf" in cmd
            )
        )
        unit = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/etc/systemd/system/praktika-controller.service" in cmd
                and "printf" in cmd
            )
        )
        cloudwatch_configure = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/usr/local/bin/praktika-configure-cloudwatch-agent" in cmd
                and "printf" in cmd
            )
        )
        assert "praktika_role" in launcher
        assert "praktika_queue" in launcher
        assert "praktika_project_slug" in launcher
        assert "export PRAKTIKA_PROJECT_SLUG" in launcher
        assert "SQS_QUEUE_NAME" not in launcher
        assert "exec /usr/local/bin/praktika-controller" in launcher
        assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
        assert "StandardOutput=append:/var/log/praktika-controller.log" in unit
        assert "StandardError=append:/var/log/praktika-controller.log" in unit
        assert "EnvironmentFile=-/etc/praktika/praktika-controller.env" not in unit
        assert (
            "latest/meta-data/tags/instance/praktika_project_slug"
            in cloudwatch_configure
        )
        assert '"file_path": "/var/log/praktika-controller.log"' in cloudwatch_configure
        assert (
            '"log_group_name": "/${PRAKTIKA_PROJECT_SLUG}/praktika-controller"'
            in cloudwatch_configure
        )


def test_ubuntu_runner_pool_uses_ubuntu_image_builder():
    builder = _IMAGE_BUILDERS_BY_NAME["ci-ubuntu-x86_64-image"]
    pool = next(pool for pool in _runner_pools if pool.name == "amd-2xsmall-ubuntu")
    setup_commands = "\n".join(builder.inline_components[0]["commands"])
    runtime_commands = builder.inline_components[1]["commands"]

    assert pool.image_builder is builder
    assert builder.ami_name == "ci-ubuntu-x86_64-{{ imagebuilder:buildDate }}"
    assert builder.image_recipe_version == "1.0.2"
    assert builder.image_tests_enabled is True
    assert builder.image_tests_timeout_minutes == 60
    assert builder.instance_types == ["t3.small"]
    assert [component["name"] for component in builder.inline_components] == [
        "praktika-controller-ubuntu-setup",
        "praktika-controller-ubuntu-runtime",
        "praktika-controller",
        "praktika-controller-ubuntu-image-test",
        "praktika-project-image-test",
    ]
    test_component = builder.inline_components[3]
    test_commands = "\n".join(test_component["commands"])
    assert test_component["phase"] == "test"
    assert "python3.12 -m pip show praktika-controller" in test_commands
    assert "docker buildx version" in test_commands
    assert "amazon-cloudwatch-agent-ctl" in test_commands
    assert "/opt/praktika/base-venvs/praktika-runtime/bin/python" in test_commands
    custom_test_component = builder.inline_components[4]
    assert custom_test_component["phase"] == "test"
    assert custom_test_component["commands"] == [
        "test -d /opt/praktika/work",
        "test -w /opt/praktika/work",
    ]
    assert builder.parent_image_resolver is not None
    assert "apt-get install --yes --no-install-recommends" in setup_commands
    assert "amazoncloudwatch-agent/ubuntu/${deb_arch}" in setup_commands
    assert "gpg --verify /tmp/amazon-cloudwatch-agent.deb.sig" in setup_commands
    assert "awscli-exe-linux-$(uname -m).zip" in setup_commands
    assert "download.docker.com/linux/ubuntu" in setup_commands
    assert "docker-ce docker-buildx-plugin docker-ce-cli containerd.io" in setup_commands
    assert "registry-mirrors" not in setup_commands
    assert "insecure-registries" not in setup_commands
    assert "dnf install" not in setup_commands
    assert any("--break-system-packages" in command for command in runtime_commands)
    assert any("--ignore-installed" in command for command in runtime_commands)
    assert not any("--force-reinstall" in command for command in runtime_commands)
    assert [lt.name for lt in builder.launch_templates] == [
        "amd-2xsmall-ubuntu-lt"
    ]


def test_project_image_builders_register_expected_launch_templates():
    assert [
        lt.name for lt in _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"].launch_templates
    ] == [
        "arm-2xsmall-lt",
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-lt",
        "workflow-orchestrator-base-lt",
    ]
    assert [
        lt.name for lt in _IMAGE_BUILDERS_BY_NAME["ci-x86_64-image"].launch_templates
    ] == ["amd-2xsmall-lt"]
    assert [
        lt.name
        for lt in _IMAGE_BUILDERS_BY_NAME[
            "ci-ubuntu-x86_64-image"
        ].launch_templates
    ] == ["amd-2xsmall-ubuntu-lt"]


def test_advanced_workflow_version_check_runs_on_ubuntu_pool():
    from ci.workflows.praktika_pr_advanced import workflow

    version_check = next(job for job in workflow.jobs if job.name == "Version Check")
    assert version_check.runs_on == [RunnerLabels.SMALL_AMD_UBUNTU]


def test_all_image_builders_stay_private():
    for name in [
        "ci-arm64-image",
        "ci-x86_64-image",
        "ci-ubuntu-x86_64-image",
    ]:
        assert _IMAGE_BUILDERS_BY_NAME[name].ami_launch_permission == {}


def test_project_image_builders_rely_on_settings_region_defaults():
    for builder in _IMAGE_BUILDERS_BY_NAME.values():
        assert builder.region == ""
        assert builder.regions == []


def test_project_github_token_minter_uses_defaults_and_project_repo_scope():
    cloud = _get_infra_config("praktika")
    gh_token_minter = cloud.github_token_minters[0]
    assert gh_token_minter.name == "praktika-gh-token"
    assert gh_token_minter.role_name == "praktika-gh-token-role"
    assert gh_token_minter.secret_name == "praktika-gh-app"
    assert gh_token_minter.repositories == ["praktika"]

    runner = next(pool for pool in cloud.runner_pools if pool.name == "arm-2xsmall")
    orchestrator = cloud.orchestrator_pool
    runner_invoke = runner.ec2_role.inline_policies["GitHubTokenMinterInvoke"][
        "Statement"
    ]
    orchestrator_invoke = orchestrator.ec2_role.inline_policies[
        "GitHubTokenMinterInvoke"
    ]["Statement"]

    assert any("lambda:InvokeFunction" in stmt["Action"] for stmt in runner_invoke)
    assert any(
        "lambda:InvokeFunction" in stmt["Action"] for stmt in orchestrator_invoke
    )
    assert runner.launch_template.tags["praktika_project_slug"] == "praktika"
    assert orchestrator.launch_template.tags["praktika_project_slug"] == "praktika"


def test_base_runner_pool_uses_base_image_without_bootstrap_user_data():
    pool = next(pool for pool in _runner_pools if pool.name == "arm-2xsmall-base")

    assert pool.image_builder is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    assert "praktika-configure-cloudwatch-agent" in pool.launch_template.user_data
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config" in pool.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller" in pool.launch_template.user_data
    )
    assert "pip install --force-reinstall" not in pool.launch_template.user_data
    assert pool.queue.name == "arm-2xsmall-base"


def test_non_base_runner_pools_patch_praktika_into_shared_base_venv():
    for pool_name in [
        "arm-2xsmall",
        "amd-2xsmall",
        "amd-2xsmall-ubuntu",
    ]:
        pool = next(pool for pool in _runner_pools if pool.name == pool_name)

        assert (
            pool.image_builder.prebuilt_venvs[0]
            .packages[-1]
            .endswith("/praktika-0.0.1-py3-none-any.whl")
        )
        assert (
            "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
            in pool.launch_template.user_data
        )
        assert "praktika-configure-cloudwatch-agent" in pool.launch_template.user_data
        assert (
            "amazon-cloudwatch-agent-ctl -a fetch-config"
            in pool.launch_template.user_data
        )
        assert "praktika-0.1.2-py3-none-any.whl" in pool.launch_template.user_data
        assert (
            "systemctl enable --now praktika-controller"
            in pool.launch_template.user_data
        )

    ubuntu = next(pool for pool in _runner_pools if pool.name == "amd-2xsmall-ubuntu")
    ubuntu_user_data = ubuntu.launch_template.user_data
    assert (
        ubuntu_user_data.index("praktika-configure-cloudwatch-agent")
        < ubuntu_user_data.index("praktika_controller-0.1.1-py3-none-any.whl")
    )
    assert (
        "python3.12 -m pip install --ignore-installed"
        in ubuntu.launch_template.user_data
    )
    assert (
        "python3.12 -m pip install --force-reinstall"
        not in ubuntu.launch_template.user_data
    )


def test_shared_arm64_images_are_used_by_runner_and_orchestrator_pools():
    builder = _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]

    assert builder.ami_launch_permission == {}
    assert builder.ami_name == "ci-arm64-{{ imagebuilder:buildDate }}"
    assert builder.instance_types == ["t4g.small"]
    assert [lt.name for lt in builder.launch_templates] == [
        "arm-2xsmall-lt",
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-lt",
        "workflow-orchestrator-base-lt",
    ]


def test_projects_orchestrator_pools_include_default_and_base_image_variants():
    assert _orchestrator_pool.name == "workflow-orchestrator"
    assert _orchestrator_pool.queue.name == "workflow-orchestrator"
    assert _orchestrator_pool.lambda_config.name == "workflow-orchestrator"
    assert _orchestrator_pool.image_builder is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    assert (
        _orchestrator_pool.image_builder.prebuilt_venvs[0]
        .packages[-1]
        .endswith("/praktika-0.0.1-py3-none-any.whl")
    )
    assert (
        "praktika-configure-cloudwatch-agent"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "praktika-0.1.2-py3-none-any.whl"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in _orchestrator_pool.launch_template.user_data
    )
    assert _orchestrator_pool.launch_template.name == "workflow-orchestrator-lt"
    assert _orchestrator_pool.autoscaling_group.name == "workflow-orchestrator"
    assert (
        _orchestrator_pool.launch_template.tags["praktika_role"]
        == "workflow_orchestrator"
    )

    assert _orchestrator_pool_base.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.queue.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.environments["SQS_QUEUE_NAME"] == (
        "workflow-orchestrator-base"
    )
    assert (
        _orchestrator_pool_base.image_builder
        is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    )
    assert (
        "praktika-configure-cloudwatch-agent"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "pip install --force-reinstall"
        not in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        _orchestrator_pool_base.launch_template.name == "workflow-orchestrator-base-lt"
    )
    assert (
        _orchestrator_pool_base.autoscaling_group.name == "workflow-orchestrator-base"
    )
    assert (
        _orchestrator_pool_base.launch_template.tags["praktika_role"]
        == "workflow_orchestrator"
    )
    assert (
        _orchestrator_pool_base.image_builder.prebuilt_venvs[0]
        .packages[-1]
        .endswith("/praktika-0.0.1-py3-none-any.whl")
    )
    assert _orchestrator_pool_base.autoscaling_group.tags["praktika_queue"] == (
        "workflow-orchestrator-base"
    )
    assert _orchestrator_pool_base.capacity_reserve == 2
    assert (
        _orchestrator_pool_base.autoscaling_group.tags["praktika_capacity_reserve"]
        == "2"
    )

    assert _orchestrator_pool.lambda_role.name == "gh-webhook-role"
    assert _orchestrator_pool_base.lambda_role.name == "gh-webhook-role"
    assert _orchestrator_pool.webhook_secret.name == "gh-webhook-secret"
    assert _orchestrator_pool_base.webhook_secret.name == "gh-webhook-secret"


def test_orchestrator_pools_share_default_lambda_role_and_hmac_secret():
    from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool

    pool_a = OrchestratorPool(
        name="orch-a",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
    )
    pool_b = OrchestratorPool(
        name="orch-b",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
    )

    assert pool_a.queue.name == pool_a.name == pool_a.lambda_config.name
    assert pool_b.queue.name == pool_b.name == pool_b.lambda_config.name
    assert pool_a.lambda_role.name == "gh-webhook-role"
    assert pool_b.lambda_role.name == "gh-webhook-role"
    assert pool_a.webhook_secret.name == "gh-webhook-secret"
    assert pool_b.webhook_secret.name == "gh-webhook-secret"
