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
