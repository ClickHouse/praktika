from dataclasses import dataclass, field
import copy
from typing import List

from praktika.infrastructure.image_builder import ImageBuilder
from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.secret_parameter import SecretParameter
from praktika.infrastructure.sqs_queue import SQSQueue

from .configs import (
    ORCHESTRATOR_INSTANCE_PROFILE_NAME,
    ORCHESTRATOR_ROLE_NAME,
    lambda_gh_trigger_config,
)
from .user_data import ci_engine_user_data


@dataclass
class OrchestratorPool:
    """A self-contained CI workflow orchestrator pool: one LaunchTemplate
    and one AutoScalingGroup that run the praktika orchestrator process.

    The orchestrator polls the workflow-trigger SQS queue and dispatches
    jobs to per-runner-type queues. min_size is always 0; `size` sets the
    desired capacity and `max_size` caps the pool.

    Registered into CloudInfrastructure.Config automatically via its
    orchestrator_pool field.

    Example::

        pool = OrchestratorPool(
            ami_id="ami-...",
            security_group_ids=["sg-..."],
            vpc_name="ci-cd",
            iam_instance_profile_name="praktika-workflow-orchestrator-profile",
            instance_type="t4g.small",
            size=2,
            max_size=2,
        )
    """

    class Scaling:
        Disabled = "disabled"
        Auto = "auto"

    # TODO: ami_id, security_group_ids, vpc_name, iam_instance_profile_name are
    #  infrastructure-level constants shared across all pools — find a way to
    #  propagate them automatically (e.g. from CloudInfrastructure.Config) so
    #  callers don't have to repeat them per pool.
    vpc_name: str
    instance_type: str
    size: int
    max_size: int
    name: str = "workflow-orchestrator"
    scaling: str = Scaling.Disabled
    ami_id: str = ""  # resolved at deploy time via SSM if empty
    image_builder: ImageBuilder.Config | None = None
    user_data: str = ""
    iam_instance_profile_name: str = ORCHESTRATOR_INSTANCE_PROFILE_NAME
    ec2_role_name: str = ORCHESTRATOR_ROLE_NAME
    gh_trigger_role_name: str = ""
    gh_trigger_webhook_secret_name: str = ""
    security_group_ids: List[str] = field(default_factory=list)
    security_group_names: List[str] = field(default_factory=list)
    volume_size_gb: int = 30

    launch_template: LaunchTemplate.Config = field(init=False)
    autoscaling_group: AutoScalingGroup.Config = field(init=False)
    ec2_role: IAMRole.Config = field(init=False)
    instance_profile: IAMInstanceProfile.Config = field(init=False)
    lambda_config: Lambda.Config = field(init=False)
    lambda_role: IAMRole.Config = field(init=False)
    webhook_secret: SecretParameter.Config = field(init=False)
    queue: SQSQueue.Config = field(init=False)

    def _queue_name(self) -> str:
        return self.name

    def _asg_name(self) -> str:
        return self.name

    def _launch_template_name(self) -> str:
        return f"{self.name}-lt"

    def _lambda_name(self) -> str:
        return self.name

    def _lambda_role_name(self) -> str:
        return self.gh_trigger_role_name or f"{self.name}-role"

    def _webhook_secret_name(self) -> str:
        return self.gh_trigger_webhook_secret_name or f"{self.name}-webhook-secret"

    def __post_init__(self):
        if not self.security_group_ids and not self.security_group_names:
            self.security_group_names = [f"{self.vpc_name}-sg"]
        assert self.scaling in (self.Scaling.Disabled, self.Scaling.Auto), (
            f"OrchestratorPool scaling={self.scaling!r} is not supported; "
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
        queue_name = self._queue_name()
        asg_name = self._asg_name()

        self.ec2_role = IAMRole.Config(
            name=self.ec2_role_name,
            trust_service="ec2.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
                "arn:aws:iam::aws:policy/EC2InstanceProfileForImageBuilder",
            ],
            inline_policies={
                "WorkflowOrchestratorAccess": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "SQSReadDeleteSend",
                            "Effect": "Allow",
                            "Action": [
                                "sqs:ReceiveMessage",
                                "sqs:DeleteMessage",
                                "sqs:ChangeMessageVisibility",
                                "sqs:SendMessage",
                                "sqs:GetQueueUrl",
                                "sqs:GetQueueAttributes",
                                "sqs:CreateQueue",
                                "sqs:DeleteQueue",
                            ],
                            "Resource": [
                                f"arn:aws:sqs:*:*:{queue_name}",
                                "arn:aws:sqs:*:*:*",
                            ],
                        },
                        {
                            "Sid": "S3ReadWrite",
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:HeadObject",
                                "s3:ListBucket",
                                "s3:GetBucketLocation",
                                "s3:PutObject",
                                "s3:AbortMultipartUpload",
                            ],
                            "Resource": ["arn:aws:s3:::*", "arn:aws:s3:::*/*"],
                        },
                        {
                            "Sid": "EC2CreateTerminate",
                            "Effect": "Allow",
                            "Action": [
                                "autoscaling:Describe*",
                                "autoscaling:TerminateInstanceInAutoScalingGroup",
                                "ec2:Describe*",
                                "ec2:RunInstances",
                                "ec2:TerminateInstances",
                                "ec2:CreateTags",
                            ],
                            "Resource": "*",
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
                            "Sid": "CloudWatchLogs",
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            "Resource": "arn:aws:logs:*:*:*",
                        },
                    ],
                }
            },
        )
        self.instance_profile = IAMInstanceProfile.Config(
            name=self.iam_instance_profile_name,
            role_name=self.ec2_role_name,
        )
        runtime_tags = {
            "praktika_pool": self.name,
            "praktika_queue": queue_name,
            "praktika_asg": asg_name,
            "praktika_scaling": self.scaling,
        }
        self.launch_template = LaunchTemplate.Config(
            name=self._launch_template_name(),
            image_id=self.ami_id,
            image_builder=self.image_builder,
            instance_type=self.instance_type,
            security_group_ids=self.security_group_ids,
            security_group_names=self.security_group_names,
            vpc_name=self.vpc_name,
            iam_instance_profile_name=self.iam_instance_profile_name,
            set_default_version_to_latest=True,
            user_data=self.user_data or ci_engine_user_data(queue_name),
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
            praktika_resource_tag="workflow_orchestrator",
        )
        if self.image_builder:
            self.image_builder.launch_templates.append(self.launch_template)
        self.lambda_config = copy.deepcopy(lambda_gh_trigger_config)
        self.lambda_config.name = self._lambda_name()
        self.lambda_config.role_name = self._lambda_role_name()
        self.lambda_config.secrets = {
            self._webhook_secret_name(): "GH_WEBHOOK_SECRET",
        }
        self.lambda_config.environments["SQS_QUEUE_NAME"] = queue_name
        self.webhook_secret = SecretParameter.Config(
            name=self._webhook_secret_name(),
            description="GitHub webhook secret for praktika-gh-trigger Lambda",
            generate_random=True,
        )
        self.lambda_role = IAMRole.Config(
            name=self._lambda_role_name(),
            trust_service="lambda.amazonaws.com",
            policy_arns=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
            inline_policies={
                # Lambda enqueues workflow trigger events to the main
                # workflow queue and writes cancel signals to S3 (per-run
                # cancel-request and per-PR cancel-before).
                "SQSSendMessage": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
                            "Resource": [
                                "arn:aws:sqs:*:*:*",
                            ],
                        },
                    ],
                },
                "S3CancelSignal": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:PutObject"],
                            "Resource": [
                                "arn:aws:s3:::praktika-artifacts-*/runs/*/cancel-request",
                                "arn:aws:s3:::praktika-artifacts-*/pr/*/cancel-before",
                            ],
                        },
                    ],
                },
            },
        )
        self.queue = SQSQueue.Config(
            name=queue_name,
            visibility_timeout=600,
            message_retention=86400,
        )
        self.autoscaling_group = AutoScalingGroup.Config(
            name=asg_name,
            vpc_name=self.vpc_name,
            availability_zones=[],
            min_size=0,
            max_size=self.max_size,
            desired_capacity=self.size,
            launch_template_name=self._launch_template_name(),
            launch_template_version="$Default" if self.image_builder else "$Latest",
            tags=runtime_tags,
            praktika_resource_tag="workflow_orchestrator",
        )
