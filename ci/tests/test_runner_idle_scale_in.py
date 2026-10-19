import json

from praktika_controller import common
from praktika.infrastructure.native.runner_pool import RunnerPool


def test_runner_pool_stamps_idle_scale_tags(monkeypatch):
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
    pool.launch_template.region = "eu-north-1"

    class _Client:
        def describe_images(self, ImageIds):
            assert ImageIds == ["ami-1234567890abcdef0"]
            return {"Images": [{"RootDeviceName": "/dev/xvda"}]}

    monkeypatch.setattr(
        "praktika.infrastructure.launch_template.aws_client",
        lambda *args, **kwargs: _Client(),
    )

    lt_data = pool.launch_template._build_launch_template_data()
    instance_tags = []
    for spec in lt_data["TagSpecifications"]:
        if spec["ResourceType"] == "instance":
            instance_tags.extend(spec["Tags"])

    assert {"Key": "praktika_scaling", "Value": "auto"} in instance_tags
    assert {"Key": "praktika_queue", "Value": "arm-2xsmall"} in instance_tags
    assert {"Key": "praktika_asg", "Value": "arm-2xsmall"} in instance_tags
    assert {"Key": "praktika_capacity_reserve", "Value": "0"} in instance_tags
    assert {"Key": "praktika_project_slug", "Value": "arm-2xsmall"} not in instance_tags
    assert lt_data["MetadataOptions"]["InstanceMetadataTags"] == "enabled"


def test_try_scale_in_if_idle_decrements_and_terminates(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {
        "praktika_scaling": "auto",
        "praktika_asg": "arm-2xsmall",
    }
    monkeypatch.setattr(
        common, "instance_tag", lambda name, token=None: tags.get(name, "")
    )

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
        def terminate_instance_in_auto_scaling_group(
            self, InstanceId, ShouldDecrementDesiredCapacity
        ):
            calls["terminate"] += 1
            assert InstanceId == "i-123"
            assert ShouldDecrementDesiredCapacity is True

    monkeypatch.setattr(
        common,
        "subprocess",
        type(
            "_Subprocess",
            (),
            {
                "Popen": lambda *args, **kwargs: calls.__setitem__(
                    "shutdown", calls["shutdown"] + 1
                )
            },
        ),
    )
    monkeypatch.setattr("boto3.client", lambda service_name, region_name=None: _ASG())

    class _Log:
        def info(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            raise AssertionError("exception log should not be called")

    assert (
        common.try_scale_in_if_idle(
            sqs=_SQS(),
            queue_url="queue-url",
            queue_name="arm-2xsmall",
            region="eu-north-1",
            instance_id="i-123",
            log=_Log(),
        )
        is True
    )
    assert calls["terminate"] == 1
    assert calls["shutdown"] == 1


def test_try_scale_in_if_idle_preserves_new_capacity_reserve_instance(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {
        "praktika_scaling": "auto",
        "praktika_asg": "arm-2xsmall",
        "praktika_capacity_reserve": "2",
    }
    monkeypatch.setattr(
        common, "instance_tag", lambda name, token=None: tags.get(name, "")
    )

    calls = {"terminate": 0, "shutdown": 0}

    monkeypatch.setattr(
        common,
        "subprocess",
        type(
            "_Subprocess",
            (),
            {
                "Popen": lambda *args, **kwargs: calls.__setitem__(
                    "shutdown", calls["shutdown"] + 1
                )
            },
        ),
    )
    monkeypatch.setattr(
        "boto3.client",
        lambda service_name, region_name=None: (_ for _ in ()).throw(
            AssertionError("boto3 client should not be created")
        ),
    )

    class _Log:
        def info(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            raise AssertionError("exception log should not be called")

    assert (
        common.try_scale_in_if_idle(
            sqs=None,
            queue_url="queue-url",
            queue_name="arm-2xsmall",
            region="eu-north-1",
            instance_id="i-123",
            has_received_message=False,
            log=_Log(),
        )
        is False
    )
    assert calls["terminate"] == 0
    assert calls["shutdown"] == 0


def test_try_scale_in_if_idle_throttles_capacity_reserve_preserve_log(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {
        "praktika_scaling": "auto",
        "praktika_asg": "arm-2xsmall",
        "praktika_capacity_reserve": "2",
    }
    monkeypatch.setattr(
        common, "instance_tag", lambda name, token=None: tags.get(name, "")
    )
    monkeypatch.setattr(
        "boto3.client",
        lambda service_name, region_name=None: (_ for _ in ()).throw(
            AssertionError("boto3 client should not be created")
        ),
    )

    now = [1000.0]
    limiter = common.LogRateLimiter(60 * 60, clock=lambda: now[0])

    class _Log:
        def __init__(self):
            self.messages = []

        def info(self, *args, **kwargs):
            self.messages.append(args)

        def exception(self, *args, **kwargs):
            raise AssertionError("exception log should not be called")

    log = _Log()

    for _ in range(2):
        assert (
            common.try_scale_in_if_idle(
                sqs=None,
                queue_url="queue-url",
                queue_name="arm-2xsmall",
                region="eu-north-1",
                instance_id="i-123",
                has_received_message=False,
                reserved_capacity_log_limiter=limiter,
                log=log,
            )
            is False
        )
    assert len(log.messages) == 1

    now[0] += 60 * 60
    assert (
        common.try_scale_in_if_idle(
            sqs=None,
            queue_url="queue-url",
            queue_name="arm-2xsmall",
            region="eu-north-1",
            instance_id="i-123",
            has_received_message=False,
            reserved_capacity_log_limiter=limiter,
            log=log,
        )
        is False
    )
    assert len(log.messages) == 2


class _RecycleLog:
    def info(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        raise AssertionError("exception log should not be called")


def _patch_ami_drift(
    monkeypatch, *, running_ami, target_ami, scaling="auto", s3_error=None
):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {
        "praktika_scaling": scaling,
        "praktika_asg": "arm-2xsmall",
        "praktika_artifact_bucket": "my-bucket",
    }
    monkeypatch.setattr(
        common, "instance_tag", lambda name, token=None: tags.get(name, "")
    )
    monkeypatch.setattr(common, "instance_ami_id", lambda token=None: running_ami)

    class _Body:
        def read(self):
            return json.dumps({"target_ami": target_ami}).encode("utf-8")

    class _S3:
        def get_object(self, Bucket, Key):
            assert Bucket == "my-bucket"
            assert Key == "ami-targets/arm-2xsmall.json"
            if s3_error is not None:
                raise s3_error
            return {"Body": _Body()}

    clients = {"s3": _S3()}
    monkeypatch.setattr(
        "boto3.client", lambda service_name, region_name=None: clients[service_name]
    )


def test_should_recycle_for_ami_detects_drift(monkeypatch):
    _patch_ami_drift(
        monkeypatch, running_ami="ami-old", target_ami="ami-new"
    )
    assert (
        common.should_recycle_for_ami(
            region="eu-north-1", instance_id="i-123", log=_RecycleLog()
        )
        is True
    )


def test_should_recycle_for_ami_no_drift(monkeypatch):
    _patch_ami_drift(
        monkeypatch, running_ami="ami-same", target_ami="ami-same"
    )
    assert (
        common.should_recycle_for_ami(
            region="eu-north-1", instance_id="i-123", log=_RecycleLog()
        )
        is False
    )


def test_should_recycle_for_ami_no_published_target(monkeypatch):
    # A missing target object (fresh pool / autoscaler has not published yet) must
    # fail-safe to False WITHOUT logging an exception on every idle poll.
    _patch_ami_drift(
        monkeypatch,
        running_ami="ami-old",
        target_ami="ignored",
        s3_error=RuntimeError("NoSuchKey"),
    )
    assert (
        common.should_recycle_for_ami(
            region="eu-north-1", instance_id="i-123", log=_RecycleLog()
        )
        is False
    )


def test_should_recycle_for_ami_skips_non_auto(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    monkeypatch.setattr(common, "instance_tag", lambda name, token=None: "")
    monkeypatch.setattr(
        "boto3.client",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("boto3 client should not be created")
        ),
    )
    assert (
        common.should_recycle_for_ami(
            region="eu-north-1", instance_id="i-123", log=_RecycleLog()
        )
        is False
    )


def test_should_recycle_for_ami_fails_safe_on_error(monkeypatch):
    monkeypatch.setattr(common, "imds_token", lambda: "token")
    tags = {"praktika_scaling": "auto", "praktika_asg": "arm-2xsmall"}
    monkeypatch.setattr(
        common, "instance_tag", lambda name, token=None: tags.get(name, "")
    )

    def _boom(token=None):
        raise RuntimeError("IMDS down")

    monkeypatch.setattr(common, "instance_ami_id", _boom)

    class _Log:
        def __init__(self):
            self.exceptions = 0

        def info(self, *args, **kwargs):
            return None

        def exception(self, *args, **kwargs):
            self.exceptions += 1

    log = _Log()
    assert (
        common.should_recycle_for_ami(
            region="eu-north-1", instance_id="i-123", log=log
        )
        is False
    )
    assert log.exceptions == 1
