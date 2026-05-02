from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from ..settings import _Settings
from .autoscaling_group import AutoScalingGroup
from .dedicated_host import DedicatedHost
from .ec2_instance import EC2Instance
from .iam_instance_profile import IAMInstanceProfile
from .image_builder import ImageBuilder
from .lambda_function import lambda_app_config, lambda_worker_config
from .launch_template import LaunchTemplate
from .sqs_queue import SQSQueue

if TYPE_CHECKING:
    from .autoscaling_group import AutoScalingGroup
    from .report_page import ReportPage
    from .storage import Storage
    from .vpc import VPC
    from .dedicated_host import DedicatedHost
    from .ec2_instance import EC2Instance
    from .iam_instance_profile import IAMInstanceProfile
    from .iam_role import IAMRole
    from .image_builder import ImageBuilder
    from .lambda_function import Lambda
    from .launch_template import LaunchTemplate
    from .native.cidb_cluster import CIDBCluster
    from .native.orchestrator_pool import OrchestratorPool
    from .native.runner_pool import RunnerPool
    from .secret_parameter import SecretParameter
    from .sqs_queue import SQSQueue


class CloudInfrastructure:
    SLACK_APP_LAMBDAS = [lambda_app_config, lambda_worker_config]

    @dataclass
    class Config:
        name: str
        lambda_functions: List["Lambda.Config"] = field(default_factory=list)
        iam_instance_profiles: List["IAMInstanceProfile.Config"] = field(
            default_factory=list
        )
        dedicated_hosts: List["DedicatedHost.Config"] = field(default_factory=list)
        ec2_instances: List["EC2Instance.Config"] = field(default_factory=list)
        image_builders: List["ImageBuilder.Config"] = field(default_factory=list)
        launch_templates: List["LaunchTemplate.Config"] = field(default_factory=list)
        autoscaling_groups: List["AutoScalingGroup.Config"] = field(
            default_factory=list
        )
        sqs_queues: List["SQSQueue.Config"] = field(default_factory=list)
        iam_roles: List["IAMRole.Config"] = field(default_factory=list)
        secret_parameters: List["SecretParameter.Config"] = field(default_factory=list)
        storages: List["Storage.Config"] = field(default_factory=list)
        report_pages: List["ReportPage.Config"] = field(default_factory=list)
        vpcs: List["VPC.Config"] = field(default_factory=list)
        runner_pools: List["RunnerPool"] = field(default_factory=list)
        orchestrator_pool: Optional["OrchestratorPool"] = None
        cidb_cluster: Optional["CIDBCluster"] = None
        _settings: Optional[_Settings] = None

        def __post_init__(self):
            seen_role_names: set = {r.name for r in self.iam_roles}
            seen_profile_names: set = {p.name for p in self.iam_instance_profiles}

            def _add_role(role):
                if role.name not in seen_role_names:
                    self.iam_roles.append(role)
                    seen_role_names.add(role.name)

            def _add_profile(profile):
                if profile.name not in seen_profile_names:
                    self.iam_instance_profiles.append(profile)
                    seen_profile_names.add(profile.name)

            if self.orchestrator_pool:
                self.secret_parameters.append(self.orchestrator_pool.webhook_secret)
                _add_role(self.orchestrator_pool.ec2_role)
                _add_role(self.orchestrator_pool.lambda_role)
                _add_profile(self.orchestrator_pool.instance_profile)
                self.lambda_functions.append(self.orchestrator_pool.lambda_config)
                self.sqs_queues.append(self.orchestrator_pool.queue)
                self.launch_templates.append(self.orchestrator_pool.launch_template)
                self.autoscaling_groups.append(self.orchestrator_pool.autoscaling_group)
            for pool in self.runner_pools:
                _add_role(pool.ec2_role)
                _add_profile(pool.instance_profile)
                self.launch_templates.append(pool.launch_template)
                self.autoscaling_groups.append(pool.autoscaling_group)
                self.sqs_queues.append(pool.queue)

            if self.cidb_cluster:
                # CIDB instance launches happen via CIDBCluster.deploy() (it
                # also authorizes SG ingress); only the supporting IAM/secret
                # are registered here so they roll out in the standard order.
                _add_role(self.cidb_cluster.ec2_role)
                _add_profile(self.cidb_cluster.instance_profile)
                self.secret_parameters.append(self.cidb_cluster.admin_password_secret)

        def _verify_account(self):
            if not self._settings or not self._settings.AWS_ACCOUNT_ID:
                raise ValueError(
                    "Settings.AWS_ACCOUNT_ID is not set. "
                    "Define it in your ci/settings/*.py to prevent accidental deploys to the wrong account."
                )
            from botocore.exceptions import (
                BotoCoreError,
                ClientError,
                NoCredentialsError,
                ProfileNotFound,
            )

            from ._utils import aws_client

            profile = self._settings.AWS_PROFILE or "<default>"
            try:
                sts = aws_client("sts", self._settings.AWS_REGION, "account-check")
                actual = sts.get_caller_identity()["Account"]
            except ProfileNotFound as e:
                raise SystemExit(
                    f"AWS profile [{profile}] not found: {e}. "
                    f"Configure it in ~/.aws/config or update Settings.AWS_PROFILE."
                )
            except NoCredentialsError:
                raise SystemExit(
                    f"No AWS credentials available for profile [{profile}]. "
                    f"Run: aws sso login --profile {profile}"
                )
            except (ClientError, BotoCoreError) as e:
                msg = str(e)
                if (
                    "UnauthorizedSSOTokenError" in type(e).__name__
                    or "expired" in msg.lower()
                    or "Session token not found or invalid" in msg
                    or "InvalidGrantException" in msg
                ):
                    raise SystemExit(
                        f"AWS SSO session for profile [{profile}] is expired or invalid. "
                        f"Run: aws sso login --profile {profile}"
                    )
                raise SystemExit(
                    f"AWS auth check failed for profile [{profile}]: {e}"
                )
            if actual != self._settings.AWS_ACCOUNT_ID:
                raise RuntimeError(
                    f"AWS account mismatch: configured={self._settings.AWS_ACCOUNT_ID}, "
                    f"actual={actual}. Aborting to prevent accidental changes to the wrong account."
                )
            print(f"AWS account verified: {actual} (profile: {profile})")

        def deploy(
            self,
            all=False,
            only: Optional[List[str]] = None,
            is_test: bool = False,
        ):
            """
            Deploy Lambda functions.

            Args:
                all: If False, only deploy code (skip settings validation and IAM policies).
                     If True, deploy everything (validate settings, deploy code, attach IAM policies).
                only: If set, deploy only selected component types by name.
            """
            self._verify_account()

            only_set = {
                s.strip().lower()
                for s in (only or [])
                if isinstance(s, str) and s.strip()
            }

            def _wants(type_name: str, *aliases: str) -> bool:
                if not only_set:
                    return True
                keys = {type_name.lower(), *{a.lower() for a in aliases if a}}
                return bool(keys & only_set)

            if (
                not self.lambda_functions
                and not self.secret_parameters
                and not self.iam_roles
                and not self.iam_instance_profiles
                and not self.dedicated_hosts
                and not self.ec2_instances
                and not self.image_builders
                and not self.launch_templates
                and not self.autoscaling_groups
                and not self.sqs_queues
            ):
                print("No infrastructure components to deploy")
                return

            # Full deployment mode: validate settings and configure environments
            if all:
                if not self._settings:
                    raise ValueError(
                        "Settings not configured. Please set _settings before deploying."
                    )

                required_settings = {
                    "EVENT_FEED_S3_PATH": self._settings.EVENT_FEED_S3_PATH,
                    "AWS_REGION": self._settings.AWS_REGION,
                }

                missing_settings = [
                    name for name, value in required_settings.items() if not value
                ]
                if missing_settings:
                    raise ValueError(
                        f"Missing required settings for Lambda deployment: {', '.join(missing_settings)}"
                    )

            # Deploy all Dedicated Hosts
            if _wants("DedicatedHost", "DedicatedHosts"):
                for host_config in self.dedicated_hosts:
                    # Only fall back to the global region setting when no explicit AZs are
                    # configured; otherwise _resolved_region() derives the correct region
                    # from the AZ name and a single AWS_REGION would break multi-region setups.
                    if (
                        self._settings
                        and self._settings.AWS_REGION
                        and not host_config.availability_zones
                    ):
                        host_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying Dedicated Hosts: {host_config.name}")
                    print("=" * 60)
                    host_config.deploy()

            # Deploy IAM roles before anything that depends on them
            if _wants("IAMRole", "IAMRoles"):
                from .iam_role import IAMRole as _IAMRole
                for role_config in self.iam_roles:
                    role_config.region = self._settings.AWS_REGION
                    print("\n" + "=" * 60)
                    print(f"Deploying IAM Role: {role_config.name}")
                    print("=" * 60)
                    role_config.deploy()

            # Deploy IAM Instance Profiles (role must exist first)
            if _wants("IAMInstanceProfile", "IAMInstanceProfiles"):
                for ip_config in self.iam_instance_profiles:
                    ip_config.region = self._settings.AWS_REGION
                    print("\n" + "=" * 60)
                    print(f"Deploying IAM Instance Profile: {ip_config.name}")
                    print("=" * 60)
                    ip_config.deploy()

            # Deploy EC2 Instances
            if _wants("EC2Instance", "EC2Instances", "Instance", "Instances"):
                for instance_config in self.ec2_instances:
                    instance_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying EC2 Instance: {instance_config.name}")
                    print("=" * 60)
                    instance_config.deploy()

            # Deploy Image Builder pipelines
            if _wants("ImageBuilder", "ImageBuilders"):
                for ib_config in self.image_builders:
                    ib_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying Image Builder: {ib_config.name}")
                    print("=" * 60)
                    ib_config.deploy()

            # Deploy VPCs (before ASGs that reference them by name)
            if _wants("VPC", "VPCs"):
                for vpc_config in self.vpcs:
                    vpc_config.region = self._settings.AWS_REGION
                    print("\n" + "=" * 60)
                    print(f"Deploying VPC: {vpc_config.name}")
                    print("=" * 60)
                    vpc_config.deploy()

            # Deploy all Launch Templates
            if _wants("LaunchTemplate", "LaunchTemplates"):
                for lt_config in self.launch_templates:
                    lt_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying Launch Template: {lt_config.name}")
                    print("=" * 60)
                    lt_config.deploy()

            # Deploy all ASGs
            if _wants("AutoScalingGroup", "AutoScalingGroups", "ASG", "ASGs"):
                for asg_config in self.autoscaling_groups:
                    asg_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying Auto Scaling Group: {asg_config.name}")
                    print("=" * 60)
                    asg_config.deploy()

            # Deploy secret parameters (before Lambdas that reference them)
            if _wants("SecretParameter", "SecretParameters", "Secret", "Secrets"):
                for secret_config in self.secret_parameters:
                    secret_config.region = self._settings.AWS_REGION
                    print("\n" + "=" * 60)
                    print(f"Deploying Secret Parameter: {secret_config.name}")
                    print("=" * 60)
                    secret_config.deploy()

            # Deploy CI DB cluster: authorizes SG ingress for ClickHouse ports
            # and launches each replica EC2. Runs after SecretParameter so the
            # admin password is in SSM before user_data fetches it.
            if (
                _wants("CIDBCluster", "CIDB", "CIDBClusters", "CI_DB")
                and self.cidb_cluster
            ):
                self.cidb_cluster.region = self._settings.AWS_REGION
                print("\n" + "=" * 60)
                print(f"Deploying CIDB Cluster (size={self.cidb_cluster.size})")
                print("=" * 60)
                self.cidb_cluster.deploy()

            # Deploy S3 buckets
            if _wants("Storage", "Storages", "S3", "Bucket", "Buckets"):
                for storage_config in self.storages:
                    storage_config.region = self._settings.AWS_REGION
                    print("\n" + "=" * 60)
                    print(f"Deploying Storage: {storage_config.name}")
                    print("=" * 60)
                    storage_config.deploy()

            # Upload HTML report pages (after Storage so the bucket exists)
            if _wants("ReportPage", "ReportPages", "Html", "HTML"):
                for rp in self.report_pages:
                    print("\n" + "=" * 60)
                    print(f"Deploying ReportPage: {rp.path}")
                    print("=" * 60)
                    rp.deploy(is_test=is_test)

            # Deploy SQS queues (before Lambdas so queue URLs are available)
            if _wants("SQS", "SQSQueue", "SQSQueues"):
                for sqs_config in self.sqs_queues:
                    sqs_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying SQS Queue: {sqs_config.name}")
                    print("=" * 60)
                    sqs_config.deploy()

            # Deploy all Lambdas (code only or with configuration)
            if _wants("Lambda", "Lambdas", "LambdaFunction", "LambdaFunctions"):
                for lambda_config in self.lambda_functions:
                    # Always set region if available (needed even for code-only deploys)
                    lambda_config.region = self._settings.AWS_REGION

                    # Only set environment variables in full deployment mode
                    if all and self._settings:
                        if self._settings.EVENT_FEED_S3_PATH:
                            # Inject project-specific settings into Lambda environment
                            # EVENT_FEED_S3_PATH is required by Slack Lambdas for event feed storage
                            lambda_config.environments["EVENT_FEED_S3_PATH"] = (
                                self._settings.EVENT_FEED_S3_PATH
                            )

                    print("\n" + "=" * 60)
                    print(f"Deploying Lambda: {lambda_config.name}")
                    print("=" * 60)
                    lambda_config.deploy()

            # Only attach IAM policies in full deployment mode
            if all and _wants("Lambda", "Lambdas", "LambdaFunction", "LambdaFunctions"):
                print("\n" + "=" * 60)
                print("Attaching IAM policies...")
                print("=" * 60)

                for lambda_config in self.lambda_functions:
                    role_arn = lambda_config.ext.get("role_arn")
                    if not role_arn:
                        print(
                            f"Warning: No role_arn found for {lambda_config.name}, skipping policy attachment"
                        )
                        continue

                    # Attach policies based on Lambda name patterns
                    if "worker" in lambda_config.name:
                        # Worker Lambda needs S3 read/write and CloudWatch access
                        lambda_config._attach_s3_readwrite_policy(role_arn)
                        lambda_config._attach_cloudwatch_logs_policy(role_arn)
                    elif "app" in lambda_config.name:
                        # App Lambda needs to invoke worker
                        worker_name = next(
                            (
                                lc.name
                                for lc in self.lambda_functions
                                if "worker" in lc.name
                            ),
                            None,
                        )
                        if worker_name:
                            lambda_config._attach_worker_invoke_policy(
                                role_arn, worker_name
                            )

                print("\n" + "=" * 60)
                print("Lambda deployment completed!")
                print("=" * 60)

        def shutdown(
            self,
            force: bool = True,
            only: Optional[List[str]] = None,
        ):
            """
            Delete/terminate infrastructure components with per-component confirmation.

            Args:
                force: If True, forcefully terminate instances without stopping first.
                only: If set, shut down only selected component types by name.
            """
            self._verify_account()

            from ..interactive import UserPrompt

            only_set = {
                s.strip().lower()
                for s in (only or [])
                if isinstance(s, str) and s.strip()
            }

            def _wants(type_name: str, *aliases: str) -> bool:
                if not only_set:
                    return True
                keys = {type_name.lower(), *{a.lower() for a in aliases if a}}
                return bool(keys & only_set)

            def _confirm_and_run(label: str, fn):
                print("\n" + "=" * 60)
                print(f"Shutdown: {label}")
                print("=" * 60)
                if UserPrompt.confirm(f"Delete '{label}'?"):
                    fn()
                else:
                    print("Skipped.")

            any_work = any([
                self.lambda_functions,
                self.secret_parameters,
                self.iam_roles,
                self.iam_instance_profiles,
                self.sqs_queues,
                self.launch_templates,
                self.autoscaling_groups,
                self.ec2_instances,
                self.dedicated_hosts,
                self.vpcs,
                self.storages,
            ])
            if not any_work:
                print("No resources configured to shutdown")
                return

            # Tear down compute first so ENIs/instances are released before VPC
            if _wants("AutoScalingGroup", "AutoScalingGroups", "ASG", "ASGs"):
                for c in self.autoscaling_groups:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"AutoScalingGroup {c.name}", c.delete)

            if _wants("EC2Instance", "EC2Instances", "Instance", "Instances"):
                for c in self.ec2_instances:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"EC2Instance {c.name}", lambda cfg=c: cfg.shutdown(force=force))

            if (
                _wants("CIDBCluster", "CIDB", "CIDBClusters", "CI_DB")
                and self.cidb_cluster
            ):
                # Note: data EBS volumes are created with DeleteOnTermination=False
                # so terminating the instances leaves orphan volumes intact.
                for c in self.cidb_cluster.instances:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(
                        f"CIDB Instance {c.name}",
                        lambda cfg=c: cfg.shutdown(force=force),
                    )

            if _wants("LaunchTemplate", "LaunchTemplates"):
                for c in self.launch_templates:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"LaunchTemplate {c.name}", c.delete)

            if _wants("SQS", "SQSQueue", "SQSQueues"):
                for c in self.sqs_queues:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"SQSQueue {c.name}", lambda cfg=c: cfg.shutdown())

            if _wants("Lambda", "Lambdas", "LambdaFunction", "LambdaFunctions"):
                for c in self.lambda_functions:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"Lambda {c.name}", c.delete)

            if _wants("SecretParameter", "SecretParameters", "Secret", "Secrets"):
                for c in self.secret_parameters:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"SecretParameter {c.name}", c.delete)

            if _wants("IAMInstanceProfile", "IAMInstanceProfiles"):
                for c in self.iam_instance_profiles:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"IAMInstanceProfile {c.name}", c.delete)

            if _wants("IAMRole", "IAMRoles"):
                for c in self.iam_roles:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"IAMRole {c.name}", c.delete)

            if _wants("DedicatedHost", "DedicatedHosts"):
                for c in self.dedicated_hosts:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"DedicatedHost {c.name}", lambda cfg=c: cfg.shutdown(force=force))

            if _wants("Storage", "Storages", "S3", "Bucket", "Buckets"):
                for c in self.storages:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"Storage {c.name}", c.delete)

            if _wants("VPC", "VPCs"):
                for c in self.vpcs:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"VPC {c.name}", c.delete)

            print("\n" + "=" * 60)
            print("Shutdown completed!")
            print("=" * 60)

        def restart_instances(self):
            """Trigger an instance refresh on all configured ASGs."""
            self._verify_account()
            if not self.autoscaling_groups:
                print("No ASGs configured")
                return
            for asg_config in self.autoscaling_groups:
                asg_config.region = self._settings.AWS_REGION
                print("\n" + "=" * 60)
                print(f"Restarting instances in ASG: {asg_config.name}")
                print("=" * 60)
                asg_config.restart()
