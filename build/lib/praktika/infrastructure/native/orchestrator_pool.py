from dataclasses import dataclass, field
from typing import List

from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.secret_parameter import SecretParameter
from praktika.infrastructure.sqs_queue import SQSQueue

from .configs import (
    GH_TRIGGER_ROLE_NAME,
    GH_TRIGGER_WEBHOOK_SECRET_NAME,
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

    # TODO: ami_id, security_group_ids, vpc_name, iam_instance_profile_name are
    #  infrastructure-level constants shared across all pools — find a way to
    #  propagate them automatically (e.g. from CloudInfrastructure.Config) so
    #  callers don't have to repeat them per pool.
    vpc_name: str
    instance_type: str
    size: int
    max_size: int
    ami_id: str = ""  # resolved at deploy time via SSM if empty
    iam_instance_profile_name: str = ORCHESTRATOR_INSTANCE_PROFILE_NAME
    ec2_role_name: str = ORCHESTRATOR_ROLE_NAME
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

    def __post_init__(self):
        if not self.security_group_ids and not self.security_group_names:
            self.security_group_names = [f"{self.vpc_name}-sg"]
        assert self.size >= 1, f"size={self.size} must be >= 1"
        assert self.max_size >= self.size, (
            f"max_size={self.max_size} must be >= size={self.size}"
        )

        self.ec2_role = IAMRole.Config(
            name=self.ec2_role_name,
            trust_service="ec2.amazonaws.com",
            policy_arns=[
                "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
                "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
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
                                "sqs:SendMessage",
                                "sqs:GetQueueUrl",
                                "sqs:GetQueueAttributes",
                                "sqs:CreateQueue",
                                "sqs:DeleteQueue",
                            ],
                            "Resource": [
                                "arn:aws:sqs:*:*:praktika-workflows",
                                "arn:aws:sqs:*:*:praktika-*",
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
        self.launch_template = LaunchTemplate.Config(
            name="praktika-workflow-orchestrator-lt",
            image_id=self.ami_id,
            instance_type=self.instance_type,
            security_group_ids=self.security_group_ids,
            security_group_names=self.security_group_names,
            iam_instance_profile_name=self.iam_instance_profile_name,
            set_default_version_to_latest=True,
            user_data=ci_engine_user_data(),
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
            praktika_resource_tag="workflow_orchestrator",
        )
        self.lambda_config = lambda_gh_trigger_config
        self.webhook_secret = SecretParameter.Config(
            name=GH_TRIGGER_WEBHOOK_SECRET_NAME,
            description="GitHub webhook secret for praktika-gh-trigger Lambda",
            generate_random=True,
        )
        self.lambda_role = IAMRole.Config(
            name=GH_TRIGGER_ROLE_NAME,
            trust_service="lambda.amazonaws.com",
            policy_arns=["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
            inline_policies={
                "SQSSendMessage": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
                            "Resource": [
                                "arn:aws:sqs:*:*:praktika-workflows",
                                "arn:aws:sqs:*:*:wf-*",
                            ],
                        },
                        {
                            "Effect": "Allow",
                            "Action": ["sqs:ListQueues"],
                            "Resource": "*",
                        },
                    ],
                },
            },
        )
        self.queue = SQSQueue.Config(
            name="praktika-workflows",
            visibility_timeout=600,
            message_retention=86400,
        )
        self.autoscaling_group = AutoScalingGroup.Config(
            name="praktika-workflow-orchestrator-asg",
            vpc_name=self.vpc_name,
            availability_zones=[],
            min_size=0,
            max_size=self.max_size,
            desired_capacity=self.size,
            launch_template_name="praktika-workflow-orchestrator-lt",
            launch_template_version="$Latest",
            praktika_resource_tag="workflow_orchestrator",
        )
