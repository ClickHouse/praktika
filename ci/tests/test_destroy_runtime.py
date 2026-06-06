from types import SimpleNamespace

from praktika.__main__ import create_parser
from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.native.github_token_minter import GitHubTokenMinter
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool


def test_infrastructure_parser_supports_destroy_runtime():
    parser = create_parser()
    args = parser.parse_args(["infrastructure", "--destroy-runtime"])

    assert args.command == "infrastructure"
    assert args.destroy_runtime is True
    assert args.deploy is False
    assert args.restart_instances is False


def test_destroy_runtime_keeps_webhook_lambda_and_data_plane(monkeypatch):
    runner_pool = RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
    )
    orchestrator_pool = OrchestratorPool(
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=OrchestratorPool.Scaling.Auto,
        size=0,
        max_size=1,
    )
    token_minter = GitHubTokenMinter()

    cloud = CloudInfrastructure.Config(
        name="cloud_ci_infra",
        runner_pools=[runner_pool],
        orchestrator_pool=orchestrator_pool,
        github_token_minters=[token_minter],
    )
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    from praktika.interactive import UserPrompt

    monkeypatch.setattr(UserPrompt, "confirm", staticmethod(lambda _: True))

    calls = []

    def _record(label):
        def _fn(*args, **kwargs):
            calls.append(label)
        return _fn

    for config in cloud.autoscaling_groups:
        monkeypatch.setattr(config, "delete", _record(f"asg:{config.name}"))
    for config in cloud.launch_templates:
        monkeypatch.setattr(config, "delete", _record(f"lt:{config.name}"))
    for config in cloud.sqs_queues:
        monkeypatch.setattr(config, "shutdown", _record(f"queue:{config.name}"))
    for autoscaler in cloud.pool_autoscalers:
        monkeypatch.setattr(
            autoscaler.lambda_config,
            "delete",
            _record(f"lambda:{autoscaler.lambda_config.name}"),
        )
        monkeypatch.setattr(
            autoscaler.lambda_role,
            "delete",
            _record(f"role:{autoscaler.lambda_role.name}"),
        )
    for minter in cloud.github_token_minters:
        monkeypatch.setattr(
            minter.lambda_config,
            "delete",
            _record(f"lambda:{minter.lambda_config.name}"),
        )
        monkeypatch.setattr(
            minter.lambda_role,
            "delete",
            _record(f"role:{minter.lambda_role.name}"),
        )
    monkeypatch.setattr(
        cloud.orchestrator_pool.lambda_config,
        "delete",
        _record(f"lambda:{cloud.orchestrator_pool.lambda_config.name}"),
    )

    cloud.destroy_runtime()

    assert f"asg:{cloud.runner_pools[0].autoscaling_group.name}" in calls
    assert f"asg:{cloud.orchestrator_pool.autoscaling_group.name}" in calls
    assert f"lt:{cloud.runner_pools[0].launch_template.name}" in calls
    assert f"lt:{cloud.orchestrator_pool.launch_template.name}" in calls
    assert f"queue:{cloud.runner_pools[0].queue.name}" in calls
    assert f"queue:{cloud.orchestrator_pool.queue.name}" in calls
    assert "lambda:cloud-ci-infra-praktika-pool-autoscaler" in calls
    assert "role:cloud-ci-infra-praktika-pool-autoscaler-role" in calls
    assert "lambda:cloud-ci-infra-praktika-gh-token" in calls
    assert "role:cloud-ci-infra-praktika-gh-token-role" in calls
    assert "lambda:cloud-ci-infra-workflow-orchestrator" not in calls
