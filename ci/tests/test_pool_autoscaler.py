from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.native.pool_autoscaler import (
    PoolAutoscaler,
    _rate_expression_for_seconds,
)
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.runner_pool import RunnerPool
from praktika.infrastructure.native.lambda_pool_autoscaler import (
    _calculate_desired_capacity,
)


def test_pool_autoscaler_builds_scheduled_lambda():
    autoscaler = PoolAutoscaler(
        interval_seconds=75,
        pools=[
            PoolAutoscaler.Pool(
                name="arm-2xsmall",
            )
        ],
    )

    assert autoscaler.lambda_config.name == "praktika-pool-autoscaler"
    assert autoscaler.lambda_config.schedule_expression == "rate(2 minutes)"
    assert autoscaler.lambda_config.environments["POLL_INTERVAL_SECONDS"] == "75"
    assert '"name":"arm-2xsmall"' in autoscaler.lambda_config.environments["POOLS_CONFIG_JSON"]


def test_rate_expression_clamps_to_minute():
    assert _rate_expression_for_seconds(0) == "rate(1 minute)"
    assert _rate_expression_for_seconds(60) == "rate(1 minute)"
    assert _rate_expression_for_seconds(61) == "rate(2 minutes)"


def test_calculate_desired_capacity_scales_up_only_when_needed():
    assert (
        _calculate_desired_capacity(
            current_desired=1,
            max_size=5,
            visible_messages=3,
            in_flight_messages=1,
        )
        == 2
    )
    assert (
        _calculate_desired_capacity(
            current_desired=2,
            max_size=5,
            visible_messages=0,
            in_flight_messages=1,
        )
        == 2
    )


def test_cloud_infrastructure_registers_pool_autoscaler():
    autoscaler = PoolAutoscaler(
        pools=[
            PoolAutoscaler.Pool(
                name="arm-2xsmall",
            )
        ],
    )

    cloud = CloudInfrastructure.Config(
        name="test-cloud",
        pool_autoscalers=[autoscaler],
    )

    assert any(
        config.name == "test-cloud-praktika-pool-autoscaler"
        for config in cloud.lambda_functions
    )
    assert any(
        role.name == "test-cloud-praktika-pool-autoscaler-role"
        for role in cloud.iam_roles
    )


def test_cloud_infrastructure_creates_implicit_runner_autoscaler():
    auto_pool = RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=2,
    )
    disabled_pool = RunnerPool(
        name="amd-2xsmall",
        instance_type="t3.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Disabled,
        size=1,
        max_size=1,
    )

    cloud = CloudInfrastructure.Config(
        name="test-cloud",
        runner_pools=[auto_pool, disabled_pool],
        pool_autoscaler_interval_seconds=120,
    )

    autoscalers = [
        l for l in cloud.lambda_functions if l.name == "test-cloud-praktika-pool-autoscaler"
    ]
    assert len(autoscalers) == 1
    autoscaler = autoscalers[0]
    assert autoscaler.schedule_expression == "rate(2 minutes)"
    assert f'"name":"{auto_pool.name}"' in autoscaler.environments["POOLS_CONFIG_JSON"]
    assert f'"name":"{disabled_pool.name}"' not in autoscaler.environments["POOLS_CONFIG_JSON"]


def test_cloud_infrastructure_creates_implicit_orchestrator_autoscaler():
    orchestrator_pool = OrchestratorPool(
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=OrchestratorPool.Scaling.Auto,
        size=0,
        max_size=1,
    )

    cloud = CloudInfrastructure.Config(
        name="test-cloud",
        orchestrator_pool=orchestrator_pool,
        pool_autoscaler_interval_seconds=60,
    )

    autoscalers = [
        l for l in cloud.lambda_functions if l.name == "test-cloud-praktika-pool-autoscaler"
    ]
    assert len(autoscalers) == 1
    autoscaler = autoscalers[0]
    env = autoscaler.environments["POOLS_CONFIG_JSON"]
    assert '"name":"workflow-orchestrator"' in env
    assert '"queue_name":"test-cloud-workflow-orchestrator"' in env
    assert '"asg_name":"test-cloud-workflow-orchestrator"' in env
