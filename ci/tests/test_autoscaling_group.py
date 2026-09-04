from praktika.infrastructure.autoscaling_group import AutoScalingGroup


def test_asg_deploy_skips_update_when_config_and_tags_match(monkeypatch):
    config = AutoScalingGroup.Config(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=1,
        desired_capacity=1,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
        praktika_resource_tag="workflow_orchestrator",
    )

    def _fetch():
        config.ext.update(
            {
                "min_size": 0,
                "max_size": 1,
                "desired_capacity": 1,
                "health_check_type": "EC2",
                "health_check_grace_period": 0,
                "vpc_zone_identifier": "subnet-1",
                "target_group_arns": [],
                "launch_template": {
                    "name": "praktika-workflow-orchestrator-lt",
                    "version": "29",
                },
                "tags": {
                    "praktika_rn": "praktika-workflow-orchestrator",
                    "praktika_resource_tag": "workflow_orchestrator",
                },
            }
        )
        return config

    monkeypatch.setattr(config, "fetch", _fetch)

    class _Client:
        def __init__(self):
            self.updated = False
            self.tagged = False

        def update_auto_scaling_group(self, **kwargs):
            self.updated = True

        def create_or_update_tags(self, **kwargs):
            self.tagged = True

    client = _Client()
    monkeypatch.setattr(
        "praktika.infrastructure.autoscaling_group.aws_client",
        lambda *args, **kwargs: client,
    )

    config.deploy()

    assert client.updated is False
    assert client.tagged is False


def test_asg_deploy_never_scales_down_a_busy_pool(monkeypatch):
    # Configured desired is 0 (scale-from-zero pool) but the pool currently has a
    # running instance (desired=1). A deploy that updates for another reason (here
    # a tag change) must NOT force desired back to 0 and kill the in-flight
    # instance — it must preserve the running capacity. See
    # INCIDENT_2026-09-03_stale_report.md.
    config = AutoScalingGroup.Config(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=10,
        desired_capacity=0,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
        praktika_resource_tag="workflow_orchestrator",
    )

    def _fetch():
        config.ext.update(
            {
                "min_size": 0,
                "max_size": 10,
                "desired_capacity": 1,  # a running instance
                "health_check_type": "EC2",
                "health_check_grace_period": 0,
                "vpc_zone_identifier": "subnet-1",
                "target_group_arns": [],
                "launch_template": {
                    "name": "praktika-workflow-orchestrator-lt",
                    "version": "29",
                },
                "tags": {},  # stale tags -> forces an update
            }
        )
        return config

    monkeypatch.setattr(config, "fetch", _fetch)

    class _Client:
        def __init__(self):
            self.update_kwargs = None

        def update_auto_scaling_group(self, **kwargs):
            self.update_kwargs = kwargs

        def create_or_update_tags(self, **kwargs):
            pass

    client = _Client()
    monkeypatch.setattr(
        "praktika.infrastructure.autoscaling_group.aws_client",
        lambda *args, **kwargs: client,
    )

    config.deploy()

    assert client.update_kwargs is not None  # an update did happen (tags changed)
    # ... but desired was preserved at the running value, not reset to 0.
    assert client.update_kwargs["DesiredCapacity"] == 1


def test_asg_deploy_up_to_date_when_current_exceeds_configured_desired(monkeypatch):
    # Only the desired capacity differs, and current (1) already exceeds the
    # configured floor (0). Because deploys never scale down, this is "up to date"
    # and must not trigger an update that would shrink a busy pool.
    config = AutoScalingGroup.Config(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=10,
        desired_capacity=0,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
        praktika_resource_tag="workflow_orchestrator",
    )

    def _fetch():
        config.ext.update(
            {
                "min_size": 0,
                "max_size": 10,
                "desired_capacity": 1,
                "health_check_type": "EC2",
                "health_check_grace_period": 0,
                "vpc_zone_identifier": "subnet-1",
                "target_group_arns": [],
                "launch_template": {
                    "name": "praktika-workflow-orchestrator-lt",
                    "version": "29",
                },
                "tags": {
                    "praktika_rn": "praktika-workflow-orchestrator",
                    "praktika_resource_tag": "workflow_orchestrator",
                },
            }
        )
        return config

    monkeypatch.setattr(config, "fetch", _fetch)

    class _Client:
        def __init__(self):
            self.updated = False
            self.tagged = False

        def update_auto_scaling_group(self, **kwargs):
            self.updated = True

        def create_or_update_tags(self, **kwargs):
            self.tagged = True

    client = _Client()
    monkeypatch.setattr(
        "praktika.infrastructure.autoscaling_group.aws_client",
        lambda *args, **kwargs: client,
    )

    config.deploy()

    assert client.updated is False
    assert client.tagged is False


def test_asg_deploy_skips_create_when_launch_template_is_missing(monkeypatch, capsys):
    config = AutoScalingGroup.Config(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=10,
        desired_capacity=0,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
    )

    monkeypatch.setattr(
        config,
        "fetch",
        lambda: (_ for _ in ()).throw(Exception("Auto Scaling group not found")),
    )

    class _Client:
        def create_auto_scaling_group(self, **kwargs):
            raise Exception(
                "An error occurred (ValidationError) when calling the "
                "CreateAutoScalingGroup operation: The specified launch template, "
                "with template name praktika-workflow-orchestrator-lt, does not exist."
            )

        def create_or_update_tags(self, **kwargs):
            raise AssertionError("tags should not be updated when create is deferred")

    monkeypatch.setattr(
        "praktika.infrastructure.autoscaling_group.aws_client",
        lambda *args, **kwargs: _Client(),
    )

    result = config.deploy()

    assert result is config
    assert config.ext["deferred_missing_launch_template"] is True
    assert (
        config.ext["deployment_warning"]
        == "Launch Template is not available yet for ASG "
        "'praktika-workflow-orchestrator'; skipping until the launch template exists"
    )
    assert "WARNING: Launch Template is not available yet" in capsys.readouterr().out


def test_asg_deploy_clamps_desired_to_reduced_max(monkeypatch):
    # Reducing max_size below the current desired must still send a valid request
    # (DesiredCapacity <= MaxSize) — not desired=10 with max=5, which AWS rejects.
    config = AutoScalingGroup.Config(
        name="praktika-workflow-orchestrator",
        region="eu-north-1",
        vpc_name="praktika-ci",
        subnet_ids=["subnet-1"],
        min_size=0,
        max_size=5,
        desired_capacity=0,
        launch_template_name="praktika-workflow-orchestrator-lt",
        launch_template_version="$Default",
        praktika_resource_tag="workflow_orchestrator",
    )

    def _fetch():
        config.ext.update(
            {
                "min_size": 0,
                "max_size": 10,
                "desired_capacity": 10,  # a busy pool
                "health_check_type": "EC2",
                "health_check_grace_period": 0,
                "vpc_zone_identifier": "subnet-1",
                "target_group_arns": [],
                "launch_template": {"name": "praktika-workflow-orchestrator-lt", "version": "1"},
                "tags": {},  # forces an update
            }
        )
        return config

    monkeypatch.setattr(config, "fetch", _fetch)

    class _Client:
        def __init__(self):
            self.update_kwargs = None

        def update_auto_scaling_group(self, **kwargs):
            self.update_kwargs = kwargs

        def create_or_update_tags(self, **kwargs):
            pass

    client = _Client()
    monkeypatch.setattr(
        "praktika.infrastructure.autoscaling_group.aws_client",
        lambda *args, **kwargs: client,
    )

    config.deploy()
    assert client.update_kwargs is not None
    assert client.update_kwargs["MaxSize"] == 5
    # max(current 10, configured 0) = 10, clamped to max_size 5.
    assert client.update_kwargs["DesiredCapacity"] == 5
