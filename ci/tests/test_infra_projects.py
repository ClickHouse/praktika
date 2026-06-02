import pytest

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
    assert runner.autoscaling_group.name == "sandbox-praktika-arm-2xsmall"
    assert runner.launch_template.tags["praktika_queue"] == runner.queue.name
    assert "sandbox-praktika-arm-2xsmall" in runner.launch_template.user_data

    assert orchestrator is not None
    assert orchestrator.queue.name == "sandbox-praktika-workflows"
    assert orchestrator.launch_template.name == "sandbox-praktika-workflow-orchestrator-lt"
    assert orchestrator.autoscaling_group.name == "sandbox-praktika-workflow-orchestrator"
    assert orchestrator.lambda_config.name == "sandbox-praktika-gh-trigger"
    assert orchestrator.webhook_secret.name == "sandbox-praktika-gh-trigger-webhook-secret"
