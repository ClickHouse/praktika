import copy
import re
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
    from .native.github_token_minter import GitHubTokenMinter
    from .native.pool_autoscaler import PoolAutoscaler
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
        github_token_minters: List["GitHubTokenMinter"] = field(default_factory=list)
        pool_autoscalers: List["PoolAutoscaler"] = field(default_factory=list)
        pool_autoscaler_interval_seconds: int = 60
        orchestrator_pool: Optional["OrchestratorPool"] = None
        orchestrator_pools: List["OrchestratorPool"] = field(default_factory=list)
        cidb_cluster: Optional["CIDBCluster"] = None
        _settings: Optional[_Settings] = None

        def _clone_owned_configs(self):
            # Cloud configs are often composed from module-level shared objects
            # in ci/infrastructure/projects.py. Namespace application mutates names deeply,
            # so we must detach from those shared objects first; otherwise one
            # project would rewrite another project's config in place.
            cloned = copy.deepcopy(
                {
                    "lambda_functions": self.lambda_functions,
                    "iam_instance_profiles": self.iam_instance_profiles,
                    "dedicated_hosts": self.dedicated_hosts,
                    "ec2_instances": self.ec2_instances,
                    "image_builders": self.image_builders,
                    "launch_templates": self.launch_templates,
                    "autoscaling_groups": self.autoscaling_groups,
                    "sqs_queues": self.sqs_queues,
                    "iam_roles": self.iam_roles,
                    "secret_parameters": self.secret_parameters,
                    "storages": self.storages,
                    "report_pages": self.report_pages,
                    "vpcs": self.vpcs,
                    "runner_pools": self.runner_pools,
                    "github_token_minters": self.github_token_minters,
                    "pool_autoscalers": self.pool_autoscalers,
                    "orchestrator_pool": self.orchestrator_pool,
                    "orchestrator_pools": self.orchestrator_pools,
                    "cidb_cluster": self.cidb_cluster,
                }
            )
            for key, value in cloned.items():
                setattr(self, key, value)

        def _project_prefix(self) -> str:
            # Project names are user-facing labels. Resource names need a
            # stable AWS-safe slug so all generated names line up across
            # queues, ASGs, IAM roles, launch templates, etc.
            prefix = re.sub(r"[^a-z0-9]+", "-", (self.name or "").strip().lower())
            prefix = re.sub(r"-{2,}", "-", prefix).strip("-")
            if not prefix:
                raise ValueError("CloudInfrastructure.Config.name must normalize to a non-empty project prefix")
            return prefix

        def _prefixed(self, value: str) -> str:
            # Idempotent helper: callers can safely run namespace application
            # multiple times without double-prefixing names.
            if not value:
                return value
            prefix = f"{self._project_prefix()}-"
            if value.startswith(prefix):
                return value
            return f"{prefix}{value}"

        def _prefixed_secret_name(self, value: str) -> str:
            # SSM/Secrets-style names may be bare names or "/path"-like names.
            # Keep the leading slash semantics intact while still applying the
            # project namespace to the actual secret name.
            if not value:
                return value
            if value.startswith("/"):
                return "/" + self._prefixed(value.lstrip("/"))
            return self._prefixed(value)

        def _replace_recursive(self, value, replacements):
            # Some configs embed resource names inside user-data scripts,
            # inline IAM policy ARNs, lambda env JSON, etc. After renaming the
            # typed fields above, sweep those nested blobs so references stay
            # consistent with the generated AWS names.
            if isinstance(value, str):
                result = value
                project_prefix = f"{self._project_prefix()}-"
                for old, new in sorted(
                    replacements.items(), key=lambda item: len(item[0]), reverse=True
                ):
                    if not old:
                        continue
                    # Avoid prefixing a resource reference that has already
                    # been rewritten once during this namespace pass.
                    pattern = rf"(?<!{re.escape(project_prefix)}){re.escape(old)}"
                    result = re.sub(pattern, new, result)
                return result
            if isinstance(value, list):
                return [self._replace_recursive(item, replacements) for item in value]
            if isinstance(value, dict):
                return {
                    self._replace_recursive(key, replacements): self._replace_recursive(val, replacements)
                    for key, val in value.items()
                }
            return value

        def _record_rename(self, replacements, old: str, new: str):
            if old and new and old != new:
                replacements[old] = new

        def _apply_project_namespace(self):
            # Centralize namespacing here instead of forcing ci/infrastructure/projects.py
            # to hand-prefix every queue, LT, IAM role, lambda, bucket, etc.
            #
            # This keeps project configs declarative and makes "one config vs
            # many projects" the only user-visible difference. The method
            # mutates every owned component plus any embedded child resources
            # created by higher-level native components such as RunnerPool,
            # OrchestratorPool, PoolAutoscaler, GitHubTokenMinter, and CIDB.
            self._clone_owned_configs()

            replacements = {}

            for config in self.vpcs:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)

            for config in self.storages:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)

            for config in self.report_pages:
                if not getattr(config, "bucket_name", "") and self.storages:
                    config.bucket_name = self.storages[0].name
                elif getattr(config, "bucket_name", ""):
                    old_bucket = config.bucket_name
                    config.bucket_name = self._prefixed(config.bucket_name)
                    self._record_rename(replacements, old_bucket, config.bucket_name)

            for config in self.iam_roles:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)

            for config in self.iam_instance_profiles:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                old_role = config.role_name
                config.role_name = self._prefixed(config.role_name)
                self._record_rename(replacements, old_role, config.role_name)

            for config in self.secret_parameters:
                old = config.name
                config.name = self._prefixed_secret_name(config.name)
                self._record_rename(replacements, old, config.name)
                self._record_rename(replacements, old.lstrip("/"), config.name.lstrip("/"))

            for config in self.sqs_queues:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)

            for config in self.launch_templates:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                if config.vpc_name:
                    old_vpc = config.vpc_name
                    config.vpc_name = self._prefixed(config.vpc_name)
                    self._record_rename(replacements, old_vpc, config.vpc_name)
                if config.iam_instance_profile_name:
                    old_profile = config.iam_instance_profile_name
                    config.iam_instance_profile_name = self._prefixed(config.iam_instance_profile_name)
                    self._record_rename(replacements, old_profile, config.iam_instance_profile_name)
                config.security_group_names = [self._prefixed(name) for name in config.security_group_names]
                if config.image_builder_pipeline_name:
                    old_pipeline = config.image_builder_pipeline_name
                    config.image_builder_pipeline_name = self._prefixed(config.image_builder_pipeline_name)
                    self._record_rename(replacements, old_pipeline, config.image_builder_pipeline_name)

            for config in self.autoscaling_groups:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                if config.vpc_name:
                    old_vpc = config.vpc_name
                    config.vpc_name = self._prefixed(config.vpc_name)
                    self._record_rename(replacements, old_vpc, config.vpc_name)
                if config.launch_template_name:
                    old_lt = config.launch_template_name
                    config.launch_template_name = self._prefixed(config.launch_template_name)
                    self._record_rename(replacements, old_lt, config.launch_template_name)

            for config in self.lambda_functions:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                if config.role_name:
                    old_role = config.role_name
                    config.role_name = self._prefixed(config.role_name)
                    self._record_rename(replacements, old_role, config.role_name)
                config.secrets = {
                    self._prefixed_secret_name(secret_name): env_name
                    for secret_name, env_name in config.secrets.items()
                }

            for config in self.image_builders:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                for attr in (
                    "image_recipe_name",
                    "infrastructure_configuration_name",
                    "instance_profile_name",
                    "distribution_configuration_name",
                    "image_pipeline_name",
                ):
                    value = getattr(config, attr, "")
                    if value:
                        new_value = self._prefixed(value)
                        setattr(config, attr, new_value)
                        self._record_rename(replacements, value, new_value)
                if config.vpc_name:
                    old_vpc = config.vpc_name
                    config.vpc_name = self._prefixed(config.vpc_name)
                    self._record_rename(replacements, old_vpc, config.vpc_name)
                config.security_group_names = [self._prefixed(name) for name in config.security_group_names]
                if config.ami_name:
                    old_ami_name = config.ami_name
                    config.ami_name = self._prefixed(config.ami_name)
                    self._record_rename(replacements, old_ami_name, config.ami_name)
                for component in config.inline_components:
                    if component.get("name"):
                        old_name = component["name"]
                        component["name"] = self._prefixed(component["name"])
                        self._record_rename(replacements, old_name, component["name"])
                for venv in config.prebuilt_venvs:
                    old_name = venv.name
                    venv.name = self._prefixed(venv.name)
                    self._record_rename(replacements, old_name, venv.name)
                    if venv.path:
                        old_path = venv.path
                        venv.path = venv.path.replace(old_name, venv.name)
                        self._record_rename(replacements, old_path, venv.path)

            for config in self.dedicated_hosts:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)

            for config in self.ec2_instances:
                old = config.name
                config.name = self._prefixed(config.name)
                self._record_rename(replacements, old, config.name)
                if getattr(config, "iam_instance_profile_name", ""):
                    old_profile = config.iam_instance_profile_name
                    config.iam_instance_profile_name = self._prefixed(config.iam_instance_profile_name)
                    self._record_rename(replacements, old_profile, config.iam_instance_profile_name)

            for pool in self.runner_pools:
                if pool.vpc_name:
                    old_vpc = pool.vpc_name
                    pool.vpc_name = self._prefixed(pool.vpc_name)
                    self._record_rename(replacements, old_vpc, pool.vpc_name)
                if pool.iam_instance_profile_name:
                    old_profile = pool.iam_instance_profile_name
                    pool.iam_instance_profile_name = self._prefixed(pool.iam_instance_profile_name)
                    self._record_rename(replacements, old_profile, pool.iam_instance_profile_name)
                if pool.ec2_role_name:
                    old_role = pool.ec2_role_name
                    pool.ec2_role_name = self._prefixed(pool.ec2_role_name)
                    self._record_rename(replacements, old_role, pool.ec2_role_name)
                pool.security_group_names = [self._prefixed(name) for name in pool.security_group_names]
                old_role_name = pool.ec2_role.name
                pool.ec2_role.name = self._prefixed(pool.ec2_role.name)
                self._record_rename(replacements, old_role_name, pool.ec2_role.name)
                old_profile_name = pool.instance_profile.name
                pool.instance_profile.name = self._prefixed(pool.instance_profile.name)
                self._record_rename(replacements, old_profile_name, pool.instance_profile.name)
                old_profile_role = pool.instance_profile.role_name
                pool.instance_profile.role_name = self._prefixed(pool.instance_profile.role_name)
                self._record_rename(replacements, old_profile_role, pool.instance_profile.role_name)
                old_lt = pool.launch_template.name
                pool.launch_template.name = self._prefixed(pool.launch_template.name)
                self._record_rename(replacements, old_lt, pool.launch_template.name)
                if getattr(pool.launch_template, "vpc_name", ""):
                    old_lt_vpc = pool.launch_template.vpc_name
                    pool.launch_template.vpc_name = self._prefixed(
                        pool.launch_template.vpc_name
                    )
                    self._record_rename(
                        replacements, old_lt_vpc, pool.launch_template.vpc_name
                    )
                if getattr(pool.launch_template, "iam_instance_profile_name", ""):
                    old_lt_profile = pool.launch_template.iam_instance_profile_name
                    pool.launch_template.iam_instance_profile_name = self._prefixed(
                        pool.launch_template.iam_instance_profile_name
                    )
                    self._record_rename(
                        replacements,
                        old_lt_profile,
                        pool.launch_template.iam_instance_profile_name,
                    )
                pool.launch_template.security_group_names = [
                    self._prefixed(name)
                    for name in pool.launch_template.security_group_names
                ]
                old_queue = pool.queue.name
                pool.queue.name = self._prefixed(pool.queue.name)
                self._record_rename(replacements, old_queue, pool.queue.name)
                old_asg = pool.autoscaling_group.name
                pool.autoscaling_group.name = self._prefixed(pool.autoscaling_group.name)
                self._record_rename(replacements, old_asg, pool.autoscaling_group.name)
                if getattr(pool.autoscaling_group, "vpc_name", ""):
                    old_asg_vpc = pool.autoscaling_group.vpc_name
                    pool.autoscaling_group.vpc_name = self._prefixed(
                        pool.autoscaling_group.vpc_name
                    )
                    self._record_rename(
                        replacements, old_asg_vpc, pool.autoscaling_group.vpc_name
                    )
                old_lt_ref = pool.autoscaling_group.launch_template_name
                pool.autoscaling_group.launch_template_name = self._prefixed(pool.autoscaling_group.launch_template_name)
                self._record_rename(replacements, old_lt_ref, pool.autoscaling_group.launch_template_name)

            for pool in self.orchestrator_pools:
                if pool.vpc_name:
                    old_vpc = pool.vpc_name
                    pool.vpc_name = self._prefixed(pool.vpc_name)
                    self._record_rename(replacements, old_vpc, pool.vpc_name)
                if pool.iam_instance_profile_name:
                    old_profile = pool.iam_instance_profile_name
                    pool.iam_instance_profile_name = self._prefixed(pool.iam_instance_profile_name)
                    self._record_rename(replacements, old_profile, pool.iam_instance_profile_name)
                if pool.ec2_role_name:
                    old_role = pool.ec2_role_name
                    pool.ec2_role_name = self._prefixed(pool.ec2_role_name)
                    self._record_rename(replacements, old_role, pool.ec2_role_name)
                pool.security_group_names = [self._prefixed(name) for name in pool.security_group_names]
                old_role_name = pool.ec2_role.name
                pool.ec2_role.name = self._prefixed(pool.ec2_role.name)
                self._record_rename(replacements, old_role_name, pool.ec2_role.name)
                old_profile_name = pool.instance_profile.name
                pool.instance_profile.name = self._prefixed(pool.instance_profile.name)
                self._record_rename(replacements, old_profile_name, pool.instance_profile.name)
                old_profile_role = pool.instance_profile.role_name
                pool.instance_profile.role_name = self._prefixed(pool.instance_profile.role_name)
                self._record_rename(replacements, old_profile_role, pool.instance_profile.role_name)
                old_lt = pool.launch_template.name
                pool.launch_template.name = self._prefixed(pool.launch_template.name)
                self._record_rename(replacements, old_lt, pool.launch_template.name)
                if getattr(pool.launch_template, "vpc_name", ""):
                    old_lt_vpc = pool.launch_template.vpc_name
                    pool.launch_template.vpc_name = self._prefixed(
                        pool.launch_template.vpc_name
                    )
                    self._record_rename(
                        replacements, old_lt_vpc, pool.launch_template.vpc_name
                    )
                if getattr(pool.launch_template, "iam_instance_profile_name", ""):
                    old_lt_profile = pool.launch_template.iam_instance_profile_name
                    pool.launch_template.iam_instance_profile_name = self._prefixed(
                        pool.launch_template.iam_instance_profile_name
                    )
                    self._record_rename(
                        replacements,
                        old_lt_profile,
                        pool.launch_template.iam_instance_profile_name,
                    )
                pool.launch_template.security_group_names = [
                    self._prefixed(name)
                    for name in pool.launch_template.security_group_names
                ]
                old_queue = pool.queue.name
                pool.queue.name = self._prefixed(pool.queue.name)
                self._record_rename(replacements, old_queue, pool.queue.name)
                old_asg = pool.autoscaling_group.name
                pool.autoscaling_group.name = self._prefixed(pool.autoscaling_group.name)
                self._record_rename(replacements, old_asg, pool.autoscaling_group.name)
                if getattr(pool.autoscaling_group, "vpc_name", ""):
                    old_asg_vpc = pool.autoscaling_group.vpc_name
                    pool.autoscaling_group.vpc_name = self._prefixed(
                        pool.autoscaling_group.vpc_name
                    )
                    self._record_rename(
                        replacements, old_asg_vpc, pool.autoscaling_group.vpc_name
                    )
                old_lt_ref = pool.autoscaling_group.launch_template_name
                pool.autoscaling_group.launch_template_name = self._prefixed(pool.autoscaling_group.launch_template_name)
                self._record_rename(replacements, old_lt_ref, pool.autoscaling_group.launch_template_name)
                old_lambda_name = pool.lambda_config.name
                pool.lambda_config.name = self._prefixed(pool.lambda_config.name)
                self._record_rename(replacements, old_lambda_name, pool.lambda_config.name)
                old_lambda_role = pool.lambda_config.role_name
                pool.lambda_config.role_name = self._prefixed(pool.lambda_config.role_name)
                self._record_rename(replacements, old_lambda_role, pool.lambda_config.role_name)
                old_webhook_secret = pool.webhook_secret.name
                pool.webhook_secret.name = self._prefixed_secret_name(pool.webhook_secret.name)
                self._record_rename(replacements, old_webhook_secret, pool.webhook_secret.name)
                self._record_rename(replacements, old_webhook_secret.lstrip("/"), pool.webhook_secret.name.lstrip("/"))
                old_pool_lambda_role = pool.lambda_role.name
                pool.lambda_role.name = self._prefixed(pool.lambda_role.name)
                self._record_rename(replacements, old_pool_lambda_role, pool.lambda_role.name)

            for autoscaler in self.pool_autoscalers:
                old_name = autoscaler.name
                autoscaler.name = self._prefixed(autoscaler.name)
                self._record_rename(replacements, old_name, autoscaler.name)
                old_role = autoscaler.lambda_role_name
                autoscaler.lambda_role_name = self._prefixed(autoscaler.lambda_role_name)
                self._record_rename(replacements, old_role, autoscaler.lambda_role_name)
                old_lambda_name = autoscaler.lambda_config.name
                autoscaler.lambda_config.name = self._prefixed(autoscaler.lambda_config.name)
                self._record_rename(replacements, old_lambda_name, autoscaler.lambda_config.name)
                old_lambda_role = autoscaler.lambda_config.role_name
                autoscaler.lambda_config.role_name = self._prefixed(autoscaler.lambda_config.role_name)
                self._record_rename(replacements, old_lambda_role, autoscaler.lambda_config.role_name)
                old_embedded_role = autoscaler.lambda_role.name
                autoscaler.lambda_role.name = self._prefixed(autoscaler.lambda_role.name)
                self._record_rename(replacements, old_embedded_role, autoscaler.lambda_role.name)

            for token_minter in self.github_token_minters:
                old_name = token_minter.name
                token_minter.name = self._prefixed(token_minter.name)
                self._record_rename(replacements, old_name, token_minter.name)
                old_role = token_minter.role_name
                token_minter.role_name = self._prefixed(token_minter.role_name)
                self._record_rename(replacements, old_role, token_minter.role_name)
                old_secret = token_minter.secret_name
                token_minter.secret_name = self._prefixed_secret_name(token_minter.secret_name)
                self._record_rename(replacements, old_secret, token_minter.secret_name)
                self._record_rename(replacements, old_secret.lstrip("/"), token_minter.secret_name.lstrip("/"))
                old_lambda_name = token_minter.lambda_config.name
                token_minter.lambda_config.name = self._prefixed(token_minter.lambda_config.name)
                self._record_rename(replacements, old_lambda_name, token_minter.lambda_config.name)
                old_lambda_role = token_minter.lambda_config.role_name
                token_minter.lambda_config.role_name = self._prefixed(token_minter.lambda_config.role_name)
                self._record_rename(replacements, old_lambda_role, token_minter.lambda_config.role_name)
                old_embedded_role = token_minter.lambda_role.name
                token_minter.lambda_role.name = self._prefixed(token_minter.lambda_role.name)
                self._record_rename(replacements, old_embedded_role, token_minter.lambda_role.name)

            if self.cidb_cluster:
                cluster = self.cidb_cluster
                if cluster.vpc_name:
                    old_vpc = cluster.vpc_name
                    cluster.vpc_name = self._prefixed(cluster.vpc_name)
                    self._record_rename(replacements, old_vpc, cluster.vpc_name)
                if cluster.iam_instance_profile_name:
                    old_profile = cluster.iam_instance_profile_name
                    cluster.iam_instance_profile_name = self._prefixed(cluster.iam_instance_profile_name)
                    self._record_rename(replacements, old_profile, cluster.iam_instance_profile_name)
                if cluster.ec2_role_name:
                    old_role = cluster.ec2_role_name
                    cluster.ec2_role_name = self._prefixed(cluster.ec2_role_name)
                    self._record_rename(replacements, old_role, cluster.ec2_role_name)
                if cluster.admin_password_secret_name:
                    old_secret = cluster.admin_password_secret_name
                    cluster.admin_password_secret_name = self._prefixed_secret_name(cluster.admin_password_secret_name)
                    self._record_rename(replacements, old_secret, cluster.admin_password_secret_name)
                    self._record_rename(replacements, old_secret.lstrip("/"), cluster.admin_password_secret_name.lstrip("/"))
                cluster.security_group_names = [self._prefixed(name) for name in cluster.security_group_names]
                old_role_name = cluster.ec2_role.name
                cluster.ec2_role.name = self._prefixed(cluster.ec2_role.name)
                self._record_rename(replacements, old_role_name, cluster.ec2_role.name)
                old_profile_name = cluster.instance_profile.name
                cluster.instance_profile.name = self._prefixed(cluster.instance_profile.name)
                self._record_rename(replacements, old_profile_name, cluster.instance_profile.name)
                old_profile_role = cluster.instance_profile.role_name
                cluster.instance_profile.role_name = self._prefixed(cluster.instance_profile.role_name)
                self._record_rename(replacements, old_profile_role, cluster.instance_profile.role_name)
                old_secret_name = cluster.admin_password_secret.name
                cluster.admin_password_secret.name = self._prefixed_secret_name(cluster.admin_password_secret.name)
                self._record_rename(replacements, old_secret_name, cluster.admin_password_secret.name)
                self._record_rename(replacements, old_secret_name.lstrip("/"), cluster.admin_password_secret.name.lstrip("/"))
                for instance in cluster.instances:
                    old_instance_name = instance.name
                    instance.name = self._prefixed(instance.name)
                    self._record_rename(replacements, old_instance_name, instance.name)
                    if getattr(instance, "iam_instance_profile_name", ""):
                        old_instance_profile = instance.iam_instance_profile_name
                        instance.iam_instance_profile_name = self._prefixed(instance.iam_instance_profile_name)
                        self._record_rename(replacements, old_instance_profile, instance.iam_instance_profile_name)

            for config in self.iam_roles:
                config.inline_policies = self._replace_recursive(config.inline_policies, replacements)
            for config in self.lambda_functions:
                config.environments = self._replace_recursive(config.environments, replacements)
                config.secrets = self._replace_recursive(config.secrets, replacements)
                config.inline_policies = self._replace_recursive(config.inline_policies, replacements)
            for config in self.launch_templates:
                config.tags = self._replace_recursive(config.tags, replacements)
                config.user_data = self._replace_recursive(config.user_data, replacements)
            for config in self.autoscaling_groups:
                config.tags = self._replace_recursive(config.tags, replacements)
            for config in self.ec2_instances:
                config.user_data = self._replace_recursive(getattr(config, "user_data", ""), replacements)

            for pool in self.runner_pools:
                pool.ec2_role.inline_policies = self._replace_recursive(pool.ec2_role.inline_policies, replacements)
                pool.launch_template.tags = self._replace_recursive(pool.launch_template.tags, replacements)
                pool.launch_template.user_data = self._replace_recursive(pool.launch_template.user_data, replacements)
                pool.autoscaling_group.tags = self._replace_recursive(pool.autoscaling_group.tags, replacements)

            for pool in self.orchestrator_pools:
                pool.ec2_role.inline_policies = self._replace_recursive(pool.ec2_role.inline_policies, replacements)
                pool.lambda_role.inline_policies = self._replace_recursive(pool.lambda_role.inline_policies, replacements)
                pool.lambda_config.environments = self._replace_recursive(pool.lambda_config.environments, replacements)
                pool.lambda_config.secrets = self._replace_recursive(pool.lambda_config.secrets, replacements)
                pool.launch_template.tags = self._replace_recursive(pool.launch_template.tags, replacements)
                pool.launch_template.user_data = self._replace_recursive(pool.launch_template.user_data, replacements)
                pool.autoscaling_group.tags = self._replace_recursive(pool.autoscaling_group.tags, replacements)

            for autoscaler in self.pool_autoscalers:
                autoscaler.lambda_role.inline_policies = self._replace_recursive(autoscaler.lambda_role.inline_policies, replacements)
                autoscaler.lambda_config.environments = self._replace_recursive(autoscaler.lambda_config.environments, replacements)

            for token_minter in self.github_token_minters:
                token_minter.lambda_role.inline_policies = self._replace_recursive(token_minter.lambda_role.inline_policies, replacements)
                token_minter.lambda_config.environments = self._replace_recursive(token_minter.lambda_config.environments, replacements)

            if self.cidb_cluster:
                cluster = self.cidb_cluster
                cluster.ec2_role.inline_policies = self._replace_recursive(cluster.ec2_role.inline_policies, replacements)
                for instance in cluster.instances:
                    instance.user_data = self._replace_recursive(getattr(instance, "user_data", ""), replacements)

        def __post_init__(self):
            if self.orchestrator_pool:
                if not self.orchestrator_pools:
                    self.orchestrator_pools = [self.orchestrator_pool]
                elif all(pool.name != self.orchestrator_pool.name for pool in self.orchestrator_pools):
                    self.orchestrator_pools.insert(0, self.orchestrator_pool)
            self.orchestrator_pool = (
                self.orchestrator_pools[0] if self.orchestrator_pools else None
            )

            # 1. Namespace all resources for this project.
            # 2. Materialize implicit child components from the high-level
            #    native components (pools, token minters, CIDB, autoscaler).
            #
            # Ordering matters: the implicit children must be created after
            # namespacing so their names and cross-references land in the same
            # project namespace as the rest of the config.
            self._apply_project_namespace()
            self.orchestrator_pool = (
                self.orchestrator_pools[0] if self.orchestrator_pools else None
            )
            seen_role_names: set = {r.name for r in self.iam_roles}
            seen_profile_names: set = {p.name for p in self.iam_instance_profiles}
            seen_secret_names: set = {s.name for s in self.secret_parameters}

            def _add_role(role):
                if role.name not in seen_role_names:
                    self.iam_roles.append(role)
                    seen_role_names.add(role.name)

            def _add_profile(profile):
                if profile.name not in seen_profile_names:
                    self.iam_instance_profiles.append(profile)
                    seen_profile_names.add(profile.name)

            def _add_secret(secret):
                if secret.name not in seen_secret_names:
                    self.secret_parameters.append(secret)
                    seen_secret_names.add(secret.name)

            implicit_autoscaler_sources = []
            for pool in self.orchestrator_pools:
                _add_secret(pool.webhook_secret)
                _add_role(pool.ec2_role)
                _add_role(pool.lambda_role)
                _add_profile(pool.instance_profile)
                self.lambda_functions.append(pool.lambda_config)
                self.sqs_queues.append(pool.queue)
                self.launch_templates.append(pool.launch_template)
                self.autoscaling_groups.append(pool.autoscaling_group)
                implicit_autoscaler_sources.append(pool)
            for pool in self.runner_pools:
                _add_role(pool.ec2_role)
                _add_profile(pool.instance_profile)
                self.launch_templates.append(pool.launch_template)
                self.autoscaling_groups.append(pool.autoscaling_group)
                self.sqs_queues.append(pool.queue)
                implicit_autoscaler_sources.append(pool)
            from .native.pool_autoscaler import PoolAutoscaler as _PoolAutoscaler
            implicit_runner_autoscaler = _PoolAutoscaler.from_pools(
                implicit_autoscaler_sources,
                interval_seconds=self.pool_autoscaler_interval_seconds,
            )
            if implicit_runner_autoscaler:
                implicit_runner_autoscaler.name = self._prefixed(
                    implicit_runner_autoscaler.name
                )
                implicit_runner_autoscaler.lambda_role_name = self._prefixed(
                    implicit_runner_autoscaler.lambda_role_name
                )
                implicit_runner_autoscaler.lambda_config.name = self._prefixed(
                    implicit_runner_autoscaler.lambda_config.name
                )
                implicit_runner_autoscaler.lambda_config.role_name = self._prefixed(
                    implicit_runner_autoscaler.lambda_config.role_name
                )
                implicit_runner_autoscaler.lambda_role.name = self._prefixed(
                    implicit_runner_autoscaler.lambda_role.name
                )
            if implicit_runner_autoscaler and not any(
                autoscaler.name == implicit_runner_autoscaler.name
                for autoscaler in self.pool_autoscalers
            ):
                self.pool_autoscalers.append(implicit_runner_autoscaler)
            for token_minter in self.github_token_minters:
                _add_role(token_minter.lambda_role)
                self.lambda_functions.append(token_minter.lambda_config)
                for pool in self.orchestrator_pools:
                    token_minter.grant_invoke(pool.ec2_role)
                for pool in self.runner_pools:
                    token_minter.grant_invoke(pool.ec2_role)
            for autoscaler in self.pool_autoscalers:
                _add_role(autoscaler.lambda_role)
                self.lambda_functions.append(autoscaler.lambda_config)

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

            # Deploy Image Builder pipelines after LaunchTemplates so
            # distribution settings can reference LT ids for automatic
            # Image Builder-managed AMI propagation.
            if _wants("ImageBuilder", "ImageBuilders"):
                for ib_config in self.image_builders:
                    ib_config.region = self._settings.AWS_REGION

                    print("\n" + "=" * 60)
                    print(f"Deploying Image Builder: {ib_config.name}")
                    print("=" * 60)
                    ib_config.deploy()

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

        def destroy_runtime(
            self,
            force: bool = True,
            only: Optional[List[str]] = None,
        ):
            """
            Delete the execution-plane resources that can be safely recreated.
            Preserve shared/data-plane resources and scarce capacity allocations.

            Args:
                force: If True, forcefully terminate instances without stopping first.
                only: If set, destroy only selected runtime component types by name.
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

            runtime_lambdas = []
            runtime_lambda_roles = []
            for autoscaler in self.pool_autoscalers:
                runtime_lambdas.append(autoscaler.lambda_config)
                runtime_lambda_roles.append(autoscaler.lambda_role)
            for token_minter in self.github_token_minters:
                runtime_lambdas.append(token_minter.lambda_config)
                runtime_lambda_roles.append(token_minter.lambda_role)

            any_work = any([
                self.sqs_queues,
                self.launch_templates,
                self.autoscaling_groups,
                self.ec2_instances,
                runtime_lambdas,
                runtime_lambda_roles,
            ])
            if not any_work:
                print("No runtime resources configured to destroy")
                return

            # Tear down compute first so ENIs/instances are released before
            # launch templates and queues disappear.
            if _wants("AutoScalingGroup", "AutoScalingGroups", "ASG", "ASGs"):
                for c in self.autoscaling_groups:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"AutoScalingGroup {c.name}", c.delete)

            if _wants("EC2Instance", "EC2Instances", "Instance", "Instances"):
                for c in self.ec2_instances:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"EC2Instance {c.name}", lambda cfg=c: cfg.shutdown(force=force))

            if _wants("LaunchTemplate", "LaunchTemplates"):
                for c in self.launch_templates:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"LaunchTemplate {c.name}", c.delete)

            if _wants("SQS", "SQSQueue", "SQSQueues"):
                for c in self.sqs_queues:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"SQSQueue {c.name}", lambda cfg=c: cfg.shutdown())

            if _wants(
                "RuntimeLambda",
                "RuntimeLambdas",
                "Lambda",
                "Lambdas",
                "LambdaFunction",
                "LambdaFunctions",
            ):
                for c in runtime_lambdas:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"Lambda {c.name}", c.delete)

            if _wants("RuntimeIAMRole", "RuntimeIAMRoles", "IAMRole", "IAMRoles"):
                for c in runtime_lambda_roles:
                    c.region = self._settings.AWS_REGION
                    _confirm_and_run(f"IAMRole {c.name}", c.delete)

            print("\n" + "=" * 60)
            print("Runtime destroy completed!")
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
