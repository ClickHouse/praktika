import base64

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
from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool


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
        pool for pool in cloud.orchestrator_pools if pool.name == "workflow-orchestrator-base"
    )

    assert runner.queue.name == "sandbox-praktika-arm-2xsmall"
    assert runner.launch_template.name == "sandbox-praktika-arm-2xsmall-lt"
    assert runner.launch_template.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert runner.autoscaling_group.name == "sandbox-praktika-arm-2xsmall"
    assert runner.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.tags["praktika_role"] == "job_runner"
    assert runner.launch_template.tags["praktika_queue"] == runner.queue.name
    assert "systemctl enable --now praktika-controller" in runner.launch_template.user_data

    assert orchestrator is not None
    assert orchestrator.queue.name == "sandbox-workflow-orchestrator"
    assert orchestrator.launch_template.name == "sandbox-workflow-orchestrator-lt"
    assert orchestrator.launch_template.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert orchestrator.autoscaling_group.name == "sandbox-workflow-orchestrator"
    assert orchestrator.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.lambda_config.name == "sandbox-workflow-orchestrator"
    assert orchestrator.webhook_secret.name == "sandbox-workflow-orchestrator-webhook-secret"
    assert orchestrator.launch_template.tags["praktika_role"] == "workflow_orchestrator"
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
            "praktika_controller-0.1.1-py3-none-any.whl" in cmd
            for cmd in runtime_component["commands"]
        )
        assert builder.prebuilt_venvs[0].name == "praktika-runtime"
        assert _IMAGE_BUILDERS_BY_NAME[name].prebuilt_venvs[0].packages[-1].endswith(
            "/praktika-0.1-py3-none-any.whl"
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
                if "/etc/systemd/system/praktika-controller.service" in cmd and "printf" in cmd
            )
        )
        assert "praktika_role" in launcher
        assert "praktika_queue" in launcher
        assert "exec /usr/local/bin/praktika-controller" in launcher
        assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
        assert "EnvironmentFile=-/etc/praktika/praktika-controller.env" not in unit

    assert [lt.name for lt in _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"].launch_templates] == [
        "praktika-arm-2xsmall-base-lt",
        "workflow-orchestrator-base-lt",
    ]
    assert _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-x86_64-image"].launch_templates == []


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
    assert _gh_token_minter.name == "praktika-gh-token"
    assert _gh_token_minter.role_name == "praktika-gh-token-role"
    assert _gh_token_minter.secret_name == "praktika-gh-app"
    assert _gh_token_minter.repositories == ["praktika"]


def test_base_runner_pool_uses_base_image_without_bootstrap_user_data():
    pool = next(pool for pool in _runner_pools if pool.name == "arm-2xsmall-base")

    assert pool.image_builder is _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"]
    assert "systemctl enable --now praktika-controller" in pool.launch_template.user_data
    assert "pip install --force-reinstall" not in pool.launch_template.user_data
    assert pool.queue.name == "praktika-arm-2xsmall-base"


def test_non_base_runner_pools_patch_praktika_into_shared_base_venv():
    for pool_name in ["arm-2xsmall", "amd-2xsmall"]:
        pool = next(pool for pool in _runner_pools if pool.name == pool_name)

        assert pool.image_builder.prebuilt_venvs[0].packages[-1].endswith(
            "/praktika-0.1-py3-none-any.whl"
        )
        assert (
            "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
            in pool.launch_template.user_data
        )
        assert "praktika-0.1.1-py3-none-any.whl" in pool.launch_template.user_data
        assert "systemctl enable --now praktika-controller" in pool.launch_template.user_data


def test_shared_arm64_images_are_used_by_runner_and_orchestrator_pools():
    builder = _IMAGE_BUILDERS_BY_NAME["praktika-base-ci-arm64-image"]

    assert builder.ami_launch_permission == {}
    assert builder.ami_tags == {
        "praktika_resource_tag": "base_controller",
        "arch": "arm64",
    }
    assert builder.instance_types == ["t4g.small"]
    assert [lt.name for lt in builder.launch_templates] == [
        "praktika-arm-2xsmall-base-lt",
        "workflow-orchestrator-base-lt"
    ]


def test_projects_orchestrator_pools_include_default_and_base_image_variants():
    assert _orchestrator_pool.name == "workflow-orchestrator"
    assert _orchestrator_pool.queue.name == "workflow-orchestrator"
    assert _orchestrator_pool.lambda_config.name == "workflow-orchestrator"
    assert _orchestrator_pool.image_builder is _IMAGE_BUILDERS_BY_NAME[
        "praktika-ci-arm64-image"
    ]
    assert _orchestrator_pool.image_builder.prebuilt_venvs[0].packages[-1].endswith(
        "/praktika-0.1-py3-none-any.whl"
    )
    assert (
        "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
        in _orchestrator_pool.launch_template.user_data
    )
    assert "praktika-0.1.1-py3-none-any.whl" in _orchestrator_pool.launch_template.user_data
    assert "systemctl enable --now praktika-controller" in _orchestrator_pool.launch_template.user_data
    assert _orchestrator_pool.launch_template.name == "workflow-orchestrator-lt"
    assert _orchestrator_pool.autoscaling_group.name == "workflow-orchestrator"
    assert _orchestrator_pool.launch_template.tags["praktika_role"] == "workflow_orchestrator"

    assert _orchestrator_pool_base.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.queue.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.environments["SQS_QUEUE_NAME"] == (
        "workflow-orchestrator-base"
    )
    assert _orchestrator_pool_base.image_builder is _IMAGE_BUILDERS_BY_NAME[
        "praktika-base-ci-arm64-image"
    ]
    assert "systemctl enable --now praktika-controller" in _orchestrator_pool_base.launch_template.user_data
    assert "pip install --force-reinstall" not in _orchestrator_pool_base.launch_template.user_data
    assert _orchestrator_pool_base.launch_template.name == "workflow-orchestrator-base-lt"
    assert _orchestrator_pool_base.autoscaling_group.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.launch_template.tags["praktika_role"] == "workflow_orchestrator"
    assert _orchestrator_pool_base.image_builder.prebuilt_venvs[0].packages[-1].endswith(
        "/praktika-0.1-py3-none-any.whl"
    )
    assert _orchestrator_pool_base.autoscaling_group.tags["praktika_queue"] == (
        "workflow-orchestrator-base"
    )

    assert _orchestrator_pool.lambda_role.name == "gh-trigger-shared-role"
    assert _orchestrator_pool_base.lambda_role.name == "gh-trigger-shared-role"
    assert _orchestrator_pool.webhook_secret.name == "gh-trigger-shared-secret"
    assert _orchestrator_pool_base.webhook_secret.name == "gh-trigger-shared-secret"


def test_orchestrator_pools_can_share_lambda_role_and_hmac_secret():
    from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool

    role_name = "shared-gh-role"
    secret_name = "shared-gh-secret"
    pool_a = OrchestratorPool(
        name="orch-a",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        gh_trigger_role_name=role_name,
        gh_trigger_webhook_secret_name=secret_name,
    )
    pool_b = OrchestratorPool(
        name="orch-b",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        gh_trigger_role_name=role_name,
        gh_trigger_webhook_secret_name=secret_name,
    )

    assert pool_a.queue.name == pool_a.name == pool_a.lambda_config.name
    assert pool_b.queue.name == pool_b.name == pool_b.lambda_config.name
    assert pool_a.lambda_role.name == role_name
    assert pool_b.lambda_role.name == role_name
    assert pool_a.webhook_secret.name == secret_name
    assert pool_b.webhook_secret.name == secret_name
