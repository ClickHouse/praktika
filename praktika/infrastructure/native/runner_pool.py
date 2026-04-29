from dataclasses import dataclass, field
from typing import List

from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.sqs_queue import SQSQueue

from .configs import RUNNER_INSTANCE_PROFILE_NAME, RUNNER_ROLE_NAME
from .user_data import runner_user_data


@dataclass
class RunnerPool:
    """A self-contained CI runner pool: one LaunchTemplate, AutoScalingGroup,
    and SQSQueue that together form a single runner type.

    The ASG always starts with min_size=0; `size` sets the desired capacity
    and `max_size` caps the pool. The queue name matches the `runs_on` label
    1:1 so praktika routes jobs without extra configuration.

    All three AWS components are created at construction time and registered
    into CloudInfrastructure.Config automatically via its runner_pools list.

    Example::

        pool = RunnerPool(
            name="arm-small",
            instance_type="m8g.2xlarge",
            ami_id="ami-...",
            security_group_ids=["sg-..."],
            vpc_name="ci-cd",
            iam_instance_profile_name="praktika-workflow-orchestrator-profile",
            scaling_type=RunnerPool.ScalingType.Fixed,
            size=1,
            max_size=2,
            volume_size_gb=100,
        )
    """

    class ScalingType:
        Auto = "auto"
        Fixed = "fixed"

    name: str
    instance_type: str
    # TODO: ami_id, security_group_ids, vpc_name, iam_instance_profile_name are
    #  infrastructure-level constants shared across all pools — find a way to
    #  propagate them automatically (e.g. from CloudInfrastructure.Config) so
    #  callers don't have to repeat them per pool.
    vpc_name: str
    scaling_type: str
    size: int
    max_size: int
    ami_id: str = ""  # resolved at deploy time via SSM if empty
    iam_instance_profile_name: str = RUNNER_INSTANCE_PROFILE_NAME
    ec2_role_name: str = RUNNER_ROLE_NAME
    security_group_ids: List[str] = field(default_factory=list)
    security_group_names: List[str] = field(default_factory=list)
    volume_size_gb: int = 30

    ec2_role: IAMRole.Config = field(init=False)
    instance_profile: IAMInstanceProfile.Config = field(init=False)
    launch_template: LaunchTemplate.Config = field(init=False)
    autoscaling_group: AutoScalingGroup.Config = field(init=False)
    queue: SQSQueue.Config = field(init=False)

    def __post_init__(self):
        if not self.security_group_ids and not self.security_group_names:
            self.security_group_names = [f"{self.vpc_name}-sg"]
        assert self.scaling_type == self.ScalingType.Fixed, (
            f"RunnerPool scaling_type={self.scaling_type!r} is not yet supported; "
            f"use ScalingType.Fixed"
        )
        assert self.size >= 1, (
            f"size={self.size} is invalid for Fixed scaling; "
            f"must be >= 1 (0 is only valid for Auto scaling)"
        )
        assert self.max_size >= self.size, (
            f"max_size={self.max_size} must be >= size={self.size}"
        )

        self.ec2_role = IAMRole.Config(
            name=self.ec2_role_name,
            trust_service="ec2.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess",
                "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
            ],
            inline_policies={
                "RunnerAccess": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "S3ReadWrite",
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:GetObjectTagging",
                                "s3:HeadObject",
                                "s3:ListBucket",
                                "s3:GetBucketLocation",
                                "s3:PutObject",
                                "s3:PutObjectTagging",
                                "s3:AbortMultipartUpload",
                                "s3:ListBucketMultipartUploads",
                                "s3:ListMultipartUploadParts",
                            ],
                            "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                        },
                        {
                            "Sid": "SQSReceiveDelete",
                            "Effect": "Allow",
                            "Action": [
                                "sqs:ReceiveMessage",
                                "sqs:DeleteMessage",
                                "sqs:ChangeMessageVisibility",
                                "sqs:SendMessage",
                                "sqs:GetQueueUrl",
                                "sqs:GetQueueAttributes",
                            ],
                            "Resource": "arn:aws:sqs:*:*:praktika-*",
                        },
                        {
                            "Sid": "SecretsManagerRead",
                            "Effect": "Allow",
                            "Action": [
                                "secretsmanager:DescribeSecret",
                                "secretsmanager:GetSecretValue",
                            ],
                            "Resource": "arn:aws:secretsmanager:*:*:secret:praktika-gh-app*",
                        },
                        {
                            "Sid": "AutoScalingScaleIn",
                            "Effect": "Allow",
                            "Action": [
                                "autoscaling:Describe*",
                                "autoscaling:TerminateInstanceInAutoScalingGroup",
                            ],
                            "Resource": "*",
                        },
                        {
                            "Sid": "EC2TerminateOnly",
                            "Effect": "Allow",
                            "Action": ["ec2:Describe*", "ec2:TerminateInstances"],
                            "Resource": "*",
                        },
                    ],
                }
            },
        )
        self.instance_profile = IAMInstanceProfile.Config(
            name=self.iam_instance_profile_name,
            role_name=self.ec2_role_name,
        )
        queue_name = f"praktika-{self.name}"
        self.launch_template = LaunchTemplate.Config(
            name=f"praktika-{self.name}-lt",
            image_id=self.ami_id,
            instance_type=self.instance_type,
            security_group_ids=self.security_group_ids,
            security_group_names=self.security_group_names,
            iam_instance_profile_name=self.iam_instance_profile_name,
            set_default_version_to_latest=True,
            user_data=runner_user_data(queue_name),
            block_device_mappings=[
                {
                    "DeviceName": "/dev/xvda",
                    "Ebs": {
                        "VolumeSize": self.volume_size_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                    },
                },
            ],
            praktika_resource_tag="runner",
        )
        self.autoscaling_group = AutoScalingGroup.Config(
            name=f"praktika-{self.name}",
            vpc_name=self.vpc_name,
            availability_zones=[],
            min_size=0,
            max_size=self.max_size,
            desired_capacity=self.size,
            launch_template_name=f"praktika-{self.name}-lt",
            launch_template_version="$Latest",
            praktika_resource_tag="runner",
        )
        self.queue = SQSQueue.Config(
            name=queue_name,
            visibility_timeout=1800,
            message_retention=86400,
        )
