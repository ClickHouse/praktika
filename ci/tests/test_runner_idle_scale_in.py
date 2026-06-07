from praktika_controller import common
from praktika.infrastructure.native.runner_pool import RunnerPool


def test_runner_pool_stamps_idle_scale_tags():
    pool = RunnerPool(
        name="arm-2xsmall",
        ami_id="ami-1234567890abcdef0",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        security_group_ids=["sg-12345678"],
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
    )

    lt_data = pool.launch_template._build_launch_template_data()
    instance_tags = []
    for spec in lt_data["TagSpecifications"]:
        if spec["ResourceType"] == "instance":
            instance_tags.extend(spec["Tags"])

    assert {"Key": "praktika_scaling", "Value": "auto"} in instance_tags
    assert {"Key": "praktika_queue", "Value": "arm-2xsmall"} in instance_tags
    assert {"Key": "praktika_asg", "Value": "arm-2xsmall"} in instance_tags
    assert {"Key": "praktika_project_slug", "Value": "arm-2xsmall"} not in instance_tags
    assert lt_data["MetadataOptions"]["InstanceMetadataTags"] == "enabled"


def test_try_scale_in_if_idle_decrements_and_terminates(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {
        "praktika_scaling": "auto",
        "praktika_asg": "arm-2xsmall",
    }
    monkeypatch.setattr(common, "instance_tag", lambda name, token=None: tags.get(name, ""))

    class _SQS:
        def get_queue_attributes(self, QueueUrl, AttributeNames):
            return {
                "Attributes": {
                    "ApproximateNumberOfMessages": "0",
                    "ApproximateNumberOfMessagesNotVisible": "0",
                }
            }

    calls = {"terminate": 0, "shutdown": 0}

    class _ASG:
        def terminate_instance_in_auto_scaling_group(self, InstanceId, ShouldDecrementDesiredCapacity):
            calls["terminate"] += 1
            assert InstanceId == "i-123"
            assert ShouldDecrementDesiredCapacity is True

    monkeypatch.setattr(common, "subprocess", type("_Subprocess", (), {"Popen": lambda *args, **kwargs: calls.__setitem__("shutdown", calls["shutdown"] + 1)}))
    monkeypatch.setattr("boto3.client", lambda service_name, region_name=None: _ASG())

    class _Log:
        def info(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            raise AssertionError("exception log should not be called")

    assert common.try_scale_in_if_idle(
        sqs=_SQS(),
        queue_url="queue-url",
        queue_name="arm-2xsmall",
        region="eu-north-1",
        instance_id="i-123",
        log=_Log(),
    ) is True
    assert calls["terminate"] == 1
    assert calls["shutdown"] == 1
