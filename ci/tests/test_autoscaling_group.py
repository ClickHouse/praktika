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
