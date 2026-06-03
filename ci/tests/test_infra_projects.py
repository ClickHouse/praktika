import pytest

from ci.infrastructure.projects import _IMAGE_BUILDERS_BY_NAME
from praktika.mangle import _get_infra_config
from praktika.settings import Settings
from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool


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
    )

    runner = cloud.runner_pools[0]
    orchestrator = cloud.orchestrator_pool

    assert runner.queue.name == "sandbox-praktika-arm-2xsmall"
    assert runner.launch_template.name == "sandbox-praktika-arm-2xsmall-lt"
    assert runner.launch_template.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert runner.autoscaling_group.name == "sandbox-praktika-arm-2xsmall"
    assert runner.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.tags["praktika_queue"] == runner.queue.name
    assert "sandbox-praktika-arm-2xsmall" in runner.launch_template.user_data

    assert orchestrator is not None
    assert orchestrator.queue.name == "sandbox-praktika-workflows"
    assert orchestrator.launch_template.name == "sandbox-praktika-workflow-orchestrator-lt"
    assert orchestrator.launch_template.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert orchestrator.autoscaling_group.name == "sandbox-praktika-workflow-orchestrator"
    assert orchestrator.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.lambda_config.name == "sandbox-praktika-gh-trigger"
    assert orchestrator.webhook_secret.name == "sandbox-praktika-gh-trigger-webhook-secret"


def test_public_base_runner_image_builders_are_declared():
    for name, arch, instance_type in [
        ("praktika-base-runner-arm64-image", "arm64", "t4g.small"),
        ("praktika-base-runner-x86_64-image", "x86_64", "t3.small"),
    ]:
        builder = _IMAGE_BUILDERS_BY_NAME[name]

        assert builder.ami_launch_permission == {"userGroups": ["all"]}
        assert builder.ami_tags == {
            "praktika_resource_tag": "base_runner",
            "arch": arch,
        }
        assert builder.instance_types == [instance_type]
        assert builder.launch_templates == []
        assert [component["name"] for component in builder.inline_components] == [
            "praktika-base-runner-common-linux",
            "praktika-base-runner-gh-cli",
            "praktika-base-runner-tools",
            "praktika-base-runner-runtime",
            "praktika-base-runner-agent",
        ]
        runtime_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-base-runner-runtime"
        )
        assert any(
            "praktika_bootstrap-0.1.0-py3-none-any.whl" in cmd
            for cmd in runtime_component["commands"]
        )
        assert any(
            "praktika-0.1-py3-none-any.whl" in cmd
            for cmd in runtime_component["commands"]
        )
        assert builder.prebuilt_venvs[0].name == "praktika-runner-pytest"
        assert _IMAGE_BUILDERS_BY_NAME[name].prebuilt_venvs[0].packages[-1].endswith(
            "/praktika-0.1-py3-none-any.whl"
        )
        agent_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-base-runner-agent"
        )
        assert any("job-agent.service" in cmd for cmd in agent_component["commands"])
        assert any("systemctl enable job-agent" == cmd for cmd in agent_component["commands"])
