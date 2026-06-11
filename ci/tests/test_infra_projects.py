import base64
from types import SimpleNamespace

import pytest

from ci.infrastructure.projects import (
    _IMAGE_BUILDERS_BY_NAME,
    _gh_token_minter,
    _orchestrator_pool,
    _orchestrator_pool_base,
    _runner_pools,
)
from praktika.mangle import _get_infra_config
from praktika.settings import Settings
from praktika.infrastructure import ImageBuilder, Storage, VPC
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
from praktika.version import current_praktika_version


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


def test_cloud_config_prefixes_embedded_pool_resources():
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
            )
        ],
        orchestrator_pool=OrchestratorPool(
            instance_type="t4g.small",
            vpc_name="praktika-ci",
            scaling=OrchestratorPool.Scaling.Auto,
            size=0,
            max_size=1,
        ),
        orchestrator_pools=[
            OrchestratorPool(
                name="workflow-orchestrator-base",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
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
                image_recipe_name="builder-recipe",
                infrastructure_configuration_name="builder-infra",
                distribution_configuration_name="builder-dist",
                image_pipeline_name="builder-pipeline",
                ami_name="builder-{{ imagebuilder:buildDate }}",
                instance_profile_name="worker-profile",
                vpc_name="ci",
                security_group_names=["ci-sg"],
                inline_components=[
                    {
                        "name": "builder-setup",
                        "platform": "Linux",
                        "commands": ["echo hi"],
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
    assert builder.infrastructure_configuration_name == "prefix-check-builder-infra"
    assert builder.distribution_configuration_name == "prefix-check-builder-dist"
    assert builder.image_pipeline_name == "prefix-check-builder-pipeline"
    assert builder.ami_name == "prefix-check-builder-{{ imagebuilder:buildDate }}"
    assert builder.instance_profile_name == "prefix-check-worker-profile"
    assert builder.vpc_name == "prefix-check-ci"
    assert builder.security_group_names == ["prefix-check-ci-sg"]
    assert builder.inline_components[0]["name"] == "prefix-check-builder-setup"
    assert builder.prebuilt_venvs[0].name == "prefix-check-runtime"


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
                image_pipeline_name="builder-pipeline",
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


def test_shared_controller_image_builders_are_declared():
    for name, arch, instance_type in [
        ("praktika-base-ci-arm64-image", "arm64", "t4g.small"),
        ("praktika-base-ci-x86_64-image", "x86_64", "t3.small"),
    ]:
        builder = _IMAGE_BUILDERS_BY_NAME[name]

        assert builder.ami_launch_permission == {}
        assert builder.ami_tags == {
            "praktika_resource_tag": "base_controller",
            "arch": arch,
        }
        assert builder.instance_types == [instance_type]
        assert [component["name"] for component in builder.inline_components] == [
            "praktika-base-controller-setup",
            "praktika-base-controller-runtime",
            "praktika-base-controller",
        ]
        runtime_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-base-controller-runtime"
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
        assert (
            _IMAGE_BUILDERS_BY_NAME[name]
            .prebuilt_venvs[0]
            .packages[-1]
            .endswith("/praktika-0.0.1-py3-none-any.whl")
        )
        agent_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-base-controller"
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
        cloudwatch = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/etc/praktika/amazon-cloudwatch-agent.json" in cmd
                and "printf" in cmd
            )
        )
        assert "praktika_role" in launcher
        assert "praktika_queue" in launcher
        assert "praktika_project_slug" in launcher
        assert "export PRAKTIKA_PROJECT_SLUG" in launcher
        assert 'export SQS_QUEUE_NAME="$PRAKTIKA_CONTROLLER_QUEUE"' in launcher
        assert "exec /usr/local/bin/praktika-controller" in launcher
        assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
        assert "StandardOutput=append:/var/log/praktika-controller.log" in unit
        assert "StandardError=append:/var/log/praktika-controller.log" in unit
        assert "EnvironmentFile=-/etc/praktika/praktika-controller.env" not in unit
        assert '"file_path": "/var/log/praktika-controller.log"' in cloudwatch
        assert '"log_group_name": "/praktika/controller"' in cloudwatch

    assert [
        lt.name
        for lt in _IMAGE_BUILDERS_BY_NAME[
            "praktika-base-ci-arm64-image"
        ].launch_templates
    ] == [
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-base-lt",
    ]
    assert (
        _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-x86_64-image"].launch_templates == []
    )


def test_all_image_builders_stay_private():
    for name in [
        "praktika-ci-arm64-image",
        "praktika-ci-x86_64-image",
        "praktika-base-ci-arm64-image",
        "praktika-base-ci-x86_64-image",
    ]:
        assert _IMAGE_BUILDERS_BY_NAME[name].ami_launch_permission == {}


def test_project_image_builders_rely_on_settings_region_defaults():
    for builder in _IMAGE_BUILDERS_BY_NAME.values():
        assert builder.region == ""
        assert builder.regions == []


def test_project_github_token_minter_uses_defaults_and_project_repo_scope():
    assert _gh_token_minter.name == "gh-token"
    assert _gh_token_minter.role_name == "gh-token-role"
    assert _gh_token_minter.secret_name == "gh-app"
    assert _gh_token_minter.repositories == ["praktika"]

    cloud = _get_infra_config("praktika")
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

    assert pool.image_builder is _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"]
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config" in pool.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller" in pool.launch_template.user_data
    )
    assert "pip install --force-reinstall" not in pool.launch_template.user_data
    assert pool.queue.name == "arm-2xsmall-base"


def test_non_base_runner_pools_patch_praktika_into_shared_base_venv():
    for pool_name in ["arm-2xsmall", "amd-2xsmall"]:
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
        assert (
            "amazon-cloudwatch-agent-ctl -a fetch-config"
            in pool.launch_template.user_data
        )
        assert "praktika-0.1.2-py3-none-any.whl" in pool.launch_template.user_data
        assert (
            "systemctl enable --now praktika-controller"
            in pool.launch_template.user_data
        )


def test_shared_arm64_images_are_used_by_runner_and_orchestrator_pools():
    builder = _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"]

    assert builder.ami_launch_permission == {}
    assert builder.ami_tags == {
        "praktika_resource_tag": "base_controller",
        "arch": "arm64",
    }
    assert builder.instance_types == ["t4g.small"]
    assert [lt.name for lt in builder.launch_templates] == [
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-base-lt",
    ]


def test_projects_orchestrator_pools_include_default_and_base_image_variants():
    assert _orchestrator_pool.name == "workflow-orchestrator"
    assert _orchestrator_pool.queue.name == "workflow-orchestrator"
    assert _orchestrator_pool.lambda_config.name == "workflow-orchestrator"
    assert (
        _orchestrator_pool.image_builder
        is _IMAGE_BUILDERS_BY_NAME["praktika-ci-arm64-image"]
    )
    assert (
        _orchestrator_pool.image_builder.prebuilt_venvs[0]
        .packages[-1]
        .endswith("/praktika-0.0.1-py3-none-any.whl")
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
        is _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"]
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
