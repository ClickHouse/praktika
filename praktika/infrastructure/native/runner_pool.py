from dataclasses import dataclass, field
from typing import List

from praktika.infrastructure.image_builder import ImageBuilder
from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.sqs_queue import SQSQueue

_DEFAULT_PRAKTIKA_CONTROLLER_USER_DATA = "\n".join(
    [
        "#!/usr/bin/env bash",
        "set -xeuo pipefail",
        "",
        "# Add any host customization you need above this line.",
        "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
        "systemctl enable --now praktika-controller",
        "",
    ]
)


@dataclass
class RunnerPool:
    """A self-contained CI runner pool: one LaunchTemplate, AutoScalingGroup,
    and SQSQueue that together form a single runner type.

    The ASG always starts with min_size=0; `size` sets the desired capacity
    and `max_size` caps the pool. The queue name matches the `runs_on` label
    1:1 so praktika routes jobs without extra configuration.

    The pool assumes the selected AMI already contains the Praktika runner
    runtime and systemd unit. By default it enables `praktika-controller` at
    boot; `user_data` can override that when extra instance boot customization
    is required.

    All three AWS components are created at construction time and registered
    into CloudInfrastructure.Config automatically via its runner_pools list.

    Example::

        pool = RunnerPool(
            name="arm-small",
            instance_type="m8g.2xlarge",
            ami_id="ami-...",
            security_group_ids=["sg-..."],
            vpc_name="ci-cd",
            scaling=RunnerPool.Scaling.Disabled,
            size=1,
            max_size=2,
            volume_size_gb=100,
        )
    """

    class Scaling:
        Disabled = "disabled"
        Auto = "auto"

    name: str
    instance_type: str
    # TODO: ami_id, security_group_ids, vpc_name are
    #  infrastructure-level constants shared across all pools — find a way to
    #  propagate them automatically (e.g. from CloudInfrastructure.Config) so
    #  callers don't have to repeat them per pool.
    vpc_name: str
    scaling: str
    size: int
    max_size: int
    ami_id: str = ""  # resolved at deploy time via SSM if empty
    image_builder: ImageBuilder.Config | None = None
    user_data: str = ""
    ec2_role: IAMRole.Config | None = None
    instance_profile: IAMInstanceProfile.Config | None = None
    security_group_ids: List[str] = field(default_factory=list)
    security_group_names: List[str] = field(default_factory=list)
    volume_size_gb: int = 30

    launch_template: LaunchTemplate.Config = field(init=False)
    autoscaling_group: AutoScalingGroup.Config = field(init=False)
    queue: SQSQueue.Config = field(init=False)

    def __post_init__(self):
        if not self.user_data:
            self.user_data = _DEFAULT_PRAKTIKA_CONTROLLER_USER_DATA
        if not self.security_group_ids and not self.security_group_names:
            self.security_group_names = [f"{self.vpc_name}-sg"]
        assert self.scaling in (self.Scaling.Disabled, self.Scaling.Auto), (
            f"RunnerPool scaling={self.scaling!r} is not supported; "
            f"use Scaling.Disabled or Scaling.Auto"
        )
        min_size = 0 if self.scaling == self.Scaling.Auto else 1
        assert self.size >= min_size, (
            f"size={self.size} is invalid for scaling={self.scaling!r}; "
            f"must be >= {min_size}"
        )
        assert self.max_size >= self.size, (
            f"max_size={self.max_size} must be >= size={self.size}"
        )

        queue_name = self.name
        asg_name = self.name
        launch_template_name = f"{self.name}-lt"

        if self.ec2_role is None:
            self.ec2_role = IAMRole.Config(
                name=f"{self.name}-role",
                trust_service="ec2.amazonaws.com",
                policy_arns=[
                    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                    "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess",
                    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
                    "arn:aws:iam::aws:policy/EC2InstanceProfileForImageBuilder",
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
                                "Resource": f"arn:aws:sqs:*:*:{queue_name}",
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
        if self.instance_profile is None:
            self.instance_profile = IAMInstanceProfile.Config(
                name=f"{self.name}-profile",
                role_name=self.ec2_role.name,
            )
        runtime_tags = {
            "praktika_pool": self.name,
            "praktika_role": "job_runner",
            "praktika_queue": queue_name,
            "praktika_asg": asg_name,
            "praktika_scaling": self.scaling,
        }
        self.launch_template = LaunchTemplate.Config(
            name=launch_template_name,
            image_id=self.ami_id,
            image_builder=self.image_builder,
            instance_type=self.instance_type,
            security_group_ids=self.security_group_ids,
            security_group_names=self.security_group_names,
            vpc_name=self.vpc_name,
            iam_instance_profile_name=self.instance_profile.name,
            set_default_version_to_latest=True,
            user_data=self.user_data,
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
            tags=runtime_tags,
            praktika_resource_tag="runner",
        )
        if self.image_builder:
            self.image_builder.launch_templates.append(self.launch_template)
        self.autoscaling_group = AutoScalingGroup.Config(
            name=asg_name,
            vpc_name=self.vpc_name,
            availability_zones=[],
            min_size=0,
            max_size=self.max_size,
            desired_capacity=self.size,
            launch_template_name=launch_template_name,
            launch_template_version="$Default" if self.image_builder else "$Latest",
            tags=runtime_tags,
            praktika_resource_tag="runner",
        )
        self.queue = SQSQueue.Config(
            name=queue_name,
            visibility_timeout=1800,
            message_retention=86400,
        )
