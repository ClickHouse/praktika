import base64
from types import SimpleNamespace

import pytest

from ci.infrastructure.projects import (
    _IMAGE_BUILDERS_BY_NAME,
    _PRAKTIKA_BASE_VERSION,
    _PRAKTIKA_LATEST_WHL_NAME,
    _PRAKTIKA_CONTROLLER_BASE_VERSION,
    _PRAKTIKA_CONTROLLER_LATEST_WHL_NAME,
    _RUNNER_ALLOWED_SECRETS,
    _RUNNER_ALLOWED_S3_PREFIXES,
    _RUNNER_ALLOWED_SSM_PARAMETERS,
    _RUNNER_ALLOW_ALL_S3_PREFIXES,
    _RUNNER_ALLOW_ALL_SECRETS,
    _RUNNER_ALLOW_ALL_SSM_PARAMETERS,
    _RUNNER_ALLOW_SSM_DEBUG,
    _orchestrator_pool,
    _orchestrator_pool_base,
    _runner_pools,
)
from praktika.mangle import _get_infra_config
from praktika.settings import Settings
from praktika.infrastructure import Components, ImageBuilder, Storage, VPC
from praktika.infrastructure.autoscaling_group import AutoScalingGroup
from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure.iam_instance_profile import IAMInstanceProfile
from praktika.infrastructure.iam_role import IAMRole
from praktika.infrastructure.lambda_function import Lambda
from praktika.infrastructure.launch_template import LaunchTemplate
from praktika.infrastructure.native.cidb_cluster import CIDBCluster
from praktika.infrastructure.native.github_token_minter import GitHubTokenMinter
from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool
from praktika.infrastructure.native.pool_autoscaler import PoolAutoscaler
from praktika.infrastructure.native.runner_pool import RunnerPool
from praktika.infrastructure.secret_parameter import SecretParameter
from praktika.infrastructure.sqs_queue import SQSQueue
from praktika.validator import Validator
from praktika.version import current_praktika_version, version_key
from ci.settings.settings import RunnerLabels


_PRAKTIKA_BASE_WHEEL = f"/praktika-{_PRAKTIKA_BASE_VERSION}-py3-none-any.whl"
# Latest praktika is installed from a fixed, version-less S3 key (see
# projects.py); user-data references the placeholder filename, not the version.
_PRAKTIKA_LATEST_WHEEL = f"/latest/{_PRAKTIKA_LATEST_WHL_NAME}"
_PRAKTIKA_CONTROLLER_BASE_WHEEL = (
    f"praktika_controller-{_PRAKTIKA_CONTROLLER_BASE_VERSION}-py3-none-any.whl"
)
# Latest controller is installed from the same fixed, version-less S3 key as
# praktika (see projects.py); user-data references the placeholder filename.
_PRAKTIKA_CONTROLLER_LATEST_WHEEL = f"/latest/{_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME}"


def _decode_embedded_file(command: str) -> str:
    payload = command.split("'")[3]
    return base64.b64decode(payload).decode("utf-8")


def _runner_access_statements(pool):
    return pool.ec2_role.inline_policies["RunnerAccess"]["Statement"]


def _statement_by_sid(pool, sid: str):
    return next(
        stmt for stmt in _runner_access_statements(pool) if stmt.get("Sid") == sid
    )


def test_get_infra_config_requires_project_when_multiple(tmp_path, monkeypatch):
    config_path = tmp_path / "cloud.py"
    config_path.write_text(
        "\n".join(
            [
                "from praktika.infrastructure.cloud import CloudInfrastructure",
                "PROJECTS = [",
                "    CloudInfrastructure.Config(name='alpha'),",
                "    CloudInfrastructure.Config(name='beta'),",
                "]",
            ]
        )
    )
    monkeypatch.setattr(Settings, "CLOUD_INFRASTRUCTURE_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError, match="Use --project NAME"):
        _get_infra_config()

    assert _get_infra_config("beta").name == "beta"


def test_get_infra_config_reports_version_mismatch_for_newer_fields(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "cloud.py"
    config_path.write_text(
        "\n".join(
            [
                "class RunnerPool:",
                "    def __init__(self):",
                "        pass",
                "RunnerPool(allowed_ssm_parameters=[])",
                "PROJECTS = []",
            ]
        )
    )
    monkeypatch.setattr(Settings, "CLOUD_INFRASTRUCTURE_CONFIG_PATH", str(config_path))

    with pytest.raises(RuntimeError) as exc:
        _get_infra_config()

    message = str(exc.value)
    assert "mismatch between the Praktika version and the infrastructure config" in message
    assert f"Config file: {config_path}" in message
    assert f"Running Praktika version: {current_praktika_version()}" in message
    assert "unexpected keyword argument 'allowed_ssm_parameters'" in message
    assert "newer infrastructure fields" in message
    assert "Praktika package version that matches this config" in message


def test_deploy_rejects_config_that_requires_newer_praktika(monkeypatch):
    current_version = current_praktika_version()
    cloud = CloudInfrastructure.Config(
        name="future",
        min_praktika_version="999.0.0",
    )
    monkeypatch.setattr(
        cloud,
        "_verify_account",
        lambda: (_ for _ in ()).throw(
            AssertionError("account check should not run on version mismatch")
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cloud.deploy()

    message = str(exc.value)
    assert "requires a newer Praktika runtime" in message
    assert "Config min_praktika_version: 999.0.0" in message
    assert f"Running Praktika version: {current_version}" in message
    assert "python3 -m praktika infrastructure --deploy" in message


def test_current_infrastructure_config_imports_all_component_groups(monkeypatch):
    monkeypatch.setattr(
        Settings,
        "CLOUD_INFRASTRUCTURE_CONFIG_PATH",
        "ci/infrastructure/projects.py",
    )

    cloud = _get_infra_config("praktika")

    assert version_key(cloud.min_praktika_version) <= version_key(
        current_praktika_version()
    )
    assert cloud.vpcs
    assert cloud.storages
    assert cloud.report_pages
    assert cloud.image_builders
    assert cloud.runner_pools
    assert cloud.github_token_minters
    assert cloud.orchestrator_pools
    assert cloud.cidb_cluster
    assert cloud.lambda_functions
    assert cloud.iam_roles
    assert cloud.iam_instance_profiles
    assert cloud.secret_parameters
    assert cloud.launch_templates
    assert cloud.autoscaling_groups
    assert cloud.sqs_queues
    assert cloud.pool_autoscalers
    assert any(pool.capacity_reserve == 2 for pool in cloud.orchestrator_pools)
    image_builders = {builder.name: builder for builder in cloud.image_builders}
    assert {
        name: builder.instance_profile_name for name, builder in image_builders.items()
    } == {
        "praktika-ci-arm64-image": "praktika-imagebuilder-profile",
        "praktika-ci-x86_64-image": "praktika-imagebuilder-profile",
        "praktika-ci-ubuntu-x86_64-image": "praktika-imagebuilder-profile",
    }
    imagebuilder_roles = {
        role.name: role
        for role in cloud.iam_roles
        if role.name.endswith("-imagebuilder-role")
    }
    assert set(imagebuilder_roles) == {"praktika-imagebuilder-role"}
    assert imagebuilder_roles["praktika-imagebuilder-role"].policy_arns == [
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
        "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
        "arn:aws:iam::aws:policy/EC2InstanceProfileForImageBuilder",
    ]
    imagebuilder_profiles = {
        profile.name: profile.role_name
        for profile in cloud.iam_instance_profiles
        if profile.name.endswith("-imagebuilder-profile")
    }
    assert imagebuilder_profiles == {
        "praktika-imagebuilder-profile": "praktika-imagebuilder-role"
    }
    assert {builder.vpc_name for builder in image_builders.values()} == {"praktika-vpc"}
    assert {
        tuple(builder.security_group_names) for builder in image_builders.values()
    } == {("praktika-vpc-sg",)}
    assert cloud.cidb_cluster.vpc_name == "praktika-vpc"
    assert cloud.cidb_cluster.security_group_names == ["praktika-vpc-sg"]


def test_infrastructure_deploy_validation_accepts_matching_s3_settings(monkeypatch):
    bucket = "silk-artifacts-eu-north-1"
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", f"{bucket}/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {bucket: f"{bucket}.s3.amazonaws.com"},
    )

    Validator.validate_infrastructure_deploy(cloud)


def test_infrastructure_deploy_validation_rejects_missing_s3_endpoint(
    monkeypatch, capsys
):
    bucket = "silk-artifacts-eu-north-1"
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", bucket)
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", f"{bucket}/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {"silk-artifacts": "silk-artifacts.s3.amazonaws.com"},
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    assert (
        f"S3_BUCKET_TO_HTTP_ENDPOINT must include bucket [{bucket}]"
        in capsys.readouterr().out
    )


def test_infrastructure_deploy_validation_rejects_storage_bucket_mismatch(
    monkeypatch, capsys
):
    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
    )

    monkeypatch.setattr(Settings, "S3_ARTIFACT_BUCKET", "silk-artifacts")
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", "silk-artifacts")
    monkeypatch.setattr(Settings, "CACHE_S3_PATH", "silk-artifacts/ci_cache")
    monkeypatch.setattr(
        Settings,
        "S3_BUCKET_TO_HTTP_ENDPOINT",
        {"silk-artifacts": "silk-artifacts.s3.amazonaws.com"},
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert "Setting S3_ARTIFACT_BUCKET bucket [silk-artifacts]" in out
    assert "silk-artifacts-eu-north-1" in out


def test_infrastructure_deploy_validation_rejects_prefixed_resource_names(capsys):
    cloud = CloudInfrastructure.Config(
        name="silk",
        vpcs=[
            VPC.Config(
                name="silk-ci",
                subnets=[VPC.Subnet(availability_zone="eu-north-1a")],
            )
        ],
    )

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert (
        "Infrastructure vpcs item name [silk-ci] already includes project prefix [silk]"
        in out
    )


def test_infrastructure_deploy_validation_accepts_base_venv_name(monkeypatch):
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-image",
                prebuilt_venvs=[ImageBuilder.PrebuiltVenv(name="praktika-runtime")],
            )
        ],
    )

    monkeypatch.setattr(Settings, "PRAKTIKA_BASE_VENV", "praktika-runtime")

    Validator.validate_infrastructure_deploy(cloud)


def test_infrastructure_deploy_validation_rejects_missing_base_venv(
    monkeypatch, capsys
):
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-image",
                prebuilt_venvs=[ImageBuilder.PrebuiltVenv(name="other-runtime")],
            )
        ],
    )

    monkeypatch.setattr(Settings, "PRAKTIKA_BASE_VENV", "praktika-runtime")

    with pytest.raises(SystemExit):
        Validator.validate_infrastructure_deploy(cloud)

    out = capsys.readouterr().out
    assert "Setting PRAKTIKA_BASE_VENV [praktika-runtime]" in out
    assert "other-runtime" in out


def test_cloud_config_prefixes_embedded_pool_resources():
    cloud = CloudInfrastructure.Config(
        name="sandbox",
        image_builders=[],
        vpcs=[
            VPC.Config(
                name="praktika-ci",
                subnets=[
                    VPC.Subnet(availability_zone="eu-north-1a"),
                ],
            )
        ],
        runner_pools=[
            RunnerPool(
                name="arm-2xsmall",
                instance_type="t4g.small",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            )
        ],
        orchestrator_pool=OrchestratorPool(
            instance_type="t4g.small",
            scaling=OrchestratorPool.Scaling.Auto,
            size=0,
            max_size=1,
        ),
        orchestrator_pools=[
            OrchestratorPool(
                name="workflow-orchestrator-base",
                instance_type="t4g.small",
                scaling=OrchestratorPool.Scaling.Auto,
                size=0,
                max_size=1,
            )
        ],
    )

    runner = cloud.runner_pools[0]
    orchestrator = cloud.orchestrator_pool
    base_orchestrator = next(
        pool
        for pool in cloud.orchestrator_pools
        if pool.name == "workflow-orchestrator-base"
    )

    assert runner.queue.name == "sandbox-arm-2xsmall"
    assert runner.ec2_role.name == "sandbox-arm-2xsmall-role"
    assert runner.instance_profile.name == "sandbox-arm-2xsmall-profile"
    assert runner.instance_profile.role_name == "sandbox-arm-2xsmall-role"
    assert runner.launch_template.name == "sandbox-arm-2xsmall-lt"
    assert runner.launch_template.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.security_group_names == ["sandbox-praktika-ci-sg"]
    assert runner.autoscaling_group.name == "sandbox-arm-2xsmall"
    assert runner.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert runner.launch_template.tags["praktika_role"] == "job_runner"
    assert runner.launch_template.tags["praktika_queue"] == runner.queue.name
    assert runner.launch_template.tags["praktika_project_slug"] == "sandbox"
    assert (
        "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"
        not in runner.ec2_role.policy_arns
    )
    assert (
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        not in runner.ec2_role.policy_arns
    )
    assert all(
        "ssm:GetParameter"
        not in (stmt.get("Action") if isinstance(stmt.get("Action"), list) else [])
        for stmt in _runner_access_statements(runner)
    )
    assert all(
        stmt.get("Sid") != "SecretsManagerRead"
        for stmt in runner.ec2_role.inline_policies["RunnerAccess"]["Statement"]
    )
    assert "praktika-configure-cloudwatch-agent" in runner.launch_template.user_data
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in runner.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller" in runner.launch_template.user_data
    )

    assert orchestrator is not None
    assert orchestrator.queue.name == "sandbox-workflow-orchestrator"
    assert orchestrator.ec2_role.name == "sandbox-workflow-orchestrator-role"
    assert orchestrator.instance_profile.name == "sandbox-workflow-orchestrator-profile"
    assert (
        orchestrator.instance_profile.role_name == "sandbox-workflow-orchestrator-role"
    )
    assert orchestrator.launch_template.name == "sandbox-workflow-orchestrator-lt"
    assert orchestrator.launch_template.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.launch_template.security_group_names == [
        "sandbox-praktika-ci-sg"
    ]
    assert orchestrator.autoscaling_group.name == "sandbox-workflow-orchestrator"
    assert orchestrator.autoscaling_group.vpc_name == "sandbox-praktika-ci"
    assert orchestrator.lambda_config.name == "sandbox-workflow-orchestrator"
    assert orchestrator.lambda_config.role_name == "sandbox-gh-webhook-role"
    assert orchestrator.lambda_config.environments["ALLOWED_PUSH_BRANCHES"] == "main"
    assert orchestrator.webhook_secret.name == "sandbox-gh-webhook-secret"
    assert orchestrator.launch_template.tags["praktika_role"] == "workflow_orchestrator"
    assert orchestrator.launch_template.tags["praktika_project_slug"] == "sandbox"
    assert all(
        stmt.get("Sid") != "SecretsManagerRead"
        for stmt in orchestrator.ec2_role.inline_policies["WorkflowOrchestratorAccess"][
            "Statement"
        ]
    )
    assert (
        "praktika-configure-cloudwatch-agent" in orchestrator.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in orchestrator.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in orchestrator.launch_template.user_data
    )
    assert (
        base_orchestrator.lambda_config.environments["SQS_QUEUE_NAME"]
        == "sandbox-workflow-orchestrator-base"
    )
    assert (
        base_orchestrator.lambda_config.environments["ALLOWED_PUSH_BRANCHES"] == "main"
    )
    assert (
        base_orchestrator.autoscaling_group.tags["praktika_queue"]
        == "sandbox-workflow-orchestrator-base"
    )


def test_runner_pools_get_distinct_roles_and_profiles():
    cloud = CloudInfrastructure.Config(
        name="sandbox",
        image_builders=[],
        runner_pools=[
            RunnerPool(
                name="arm-2xsmall",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            ),
            RunnerPool(
                name="arm-2xsmall-base",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
            ),
        ],
    )

    roles = {pool.ec2_role.name for pool in cloud.runner_pools}
    profiles = {pool.instance_profile.name for pool in cloud.runner_pools}

    assert roles == {
        "sandbox-arm-2xsmall-role",
        "sandbox-arm-2xsmall-base-role",
    }
    assert profiles == {
        "sandbox-arm-2xsmall-profile",
        "sandbox-arm-2xsmall-base-profile",
    }


def test_runner_pool_default_role_has_no_broad_ssm_secret_or_s3_reads():
    pool = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
    )

    assert (
        "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"
        not in pool.ec2_role.policy_arns
    )
    assert (
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        not in pool.ec2_role.policy_arns
    )
    actions = [
        action
        for stmt in _runner_access_statements(pool)
        for action in (
            stmt.get("Action") if isinstance(stmt.get("Action"), list) else []
        )
    ]
    assert "ssm:GetParameter" not in actions
    assert "ssm:GetParameters" not in actions
    assert "secretsmanager:GetSecretValue" not in actions
    assert "s3:GetObject" not in actions
    assert "s3:PutObject" not in actions
    sids = {stmt.get("Sid") for stmt in _runner_access_statements(pool)}
    assert "SSMManagedInstanceCore" not in sids
    assert "SSMMessages" not in sids
    assert "EC2Messages" not in sids


def test_runner_pool_can_opt_in_to_ssm_debug_permissions():
    pool = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        allow_ssm_debug=True,
    )

    ssm_core = _statement_by_sid(pool, "SSMManagedInstanceCore")
    assert "ssm:UpdateInstanceInformation" in ssm_core["Action"]
    assert "ssm:GetParameter" not in ssm_core["Action"]
    assert "ssm:GetParameters" not in ssm_core["Action"]
    assert _statement_by_sid(pool, "SSMMessages")["Action"] == [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    ]
    assert _statement_by_sid(pool, "EC2Messages")["Action"] == [
        "ec2messages:AcknowledgeMessage",
        "ec2messages:DeleteMessage",
        "ec2messages:FailMessage",
        "ec2messages:GetEndpoint",
        "ec2messages:GetMessages",
        "ec2messages:SendReply",
    ]


def test_runner_pool_with_image_builder_keeps_runner_role_without_ssm_agent_permissions():
    pool = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        image_builder=ImageBuilder.Config(name="runner-image"),
    )

    assert (
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
        not in pool.ec2_role.policy_arns
    )
    sids = {stmt.get("Sid") for stmt in _runner_access_statements(pool)}
    assert "SSMManagedInstanceCore" not in sids
    assert "SSMMessages" not in sids
    assert "EC2Messages" not in sids


def test_runner_pool_can_allow_specific_ssm_parameters_secrets_and_s3_prefixes():
    pool = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        allowed_ssm_parameters=[
            "cidb-connection",
            "/nested/docker-registry",
            "arn:aws:ssm:eu-north-1:123456789012:parameter/external",
        ],
        allowed_secrets=[
            "github-app",
            "arn:aws:secretsmanager:eu-north-1:123456789012:secret:external-AbCd",
        ],
        allowed_s3_prefixes=[
            "artifact-bucket",
            "cache-bucket/ci_cache",
            "s3://reports-bucket/reports",
            "arn:aws:s3:::external-bucket/custom/*",
        ],
    )

    ssm_read = _statement_by_sid(pool, "AllowedSSMParametersRead")
    assert ssm_read["Action"] == ["ssm:GetParameter", "ssm:GetParameters"]
    assert ssm_read["Resource"] == [
        "arn:aws:ssm:*:*:parameter/cidb-connection",
        "arn:aws:ssm:*:*:parameter/nested/docker-registry",
        "arn:aws:ssm:eu-north-1:123456789012:parameter/external",
    ]

    secrets_read = _statement_by_sid(pool, "AllowedSecretsManagerSecretsRead")
    assert secrets_read["Action"] == [
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
    ]
    assert secrets_read["Resource"] == [
        "arn:aws:secretsmanager:*:*:secret:github-app*",
        "arn:aws:secretsmanager:eu-north-1:123456789012:secret:external-AbCd",
    ]

    s3_read_write = _statement_by_sid(pool, "AllowedS3ReadWrite")
    assert s3_read_write["Resource"] == [
        "arn:aws:s3:::artifact-bucket",
        "arn:aws:s3:::artifact-bucket/*",
        "arn:aws:s3:::cache-bucket",
        "arn:aws:s3:::cache-bucket/ci_cache",
        "arn:aws:s3:::cache-bucket/ci_cache/*",
        "arn:aws:s3:::reports-bucket",
        "arn:aws:s3:::reports-bucket/reports",
        "arn:aws:s3:::reports-bucket/reports/*",
        "arn:aws:s3:::external-bucket/custom/*",
    ]


def test_runner_pool_allow_lists_are_project_namespaced():
    cloud = CloudInfrastructure.Config(
        name="sandbox",
        image_builders=[],
        runner_pools=[
            RunnerPool(
                name="runner",
                instance_type="t4g.small",
                vpc_name="praktika-ci",
                scaling=RunnerPool.Scaling.Auto,
                size=0,
                max_size=1,
                allowed_ssm_parameters=[
                    "cidb-connection",
                    "/nested/docker-registry",
                    "arn:aws:ssm:eu-north-1:123456789012:parameter/external",
                ],
                allowed_secrets=[
                    "github-app",
                    "arn:aws:secretsmanager:eu-north-1:123456789012:secret:external-AbCd",
                ],
                allowed_s3_prefixes=[
                    "artifact-bucket",
                    "cache-bucket/ci_cache",
                    "s3://reports-bucket/reports",
                    "arn:aws:s3:::external-bucket/custom/*",
                ],
            )
        ],
    )

    pool = cloud.runner_pools[0]
    assert pool.allowed_ssm_parameters == [
        "sandbox-cidb-connection",
        "/sandbox-nested/docker-registry",
        "arn:aws:ssm:eu-north-1:123456789012:parameter/external",
    ]
    assert pool.allowed_secrets == [
        "sandbox-github-app",
        "arn:aws:secretsmanager:eu-north-1:123456789012:secret:external-AbCd",
    ]
    assert pool.allowed_s3_prefixes == [
        "sandbox-artifact-bucket",
        "sandbox-cache-bucket/ci_cache",
        "s3://sandbox-reports-bucket/reports",
        "arn:aws:s3:::external-bucket/custom/*",
    ]
    assert _statement_by_sid(pool, "AllowedSSMParametersRead")["Resource"] == [
        "arn:aws:ssm:*:*:parameter/sandbox-cidb-connection",
        "arn:aws:ssm:*:*:parameter/sandbox-nested/docker-registry",
        "arn:aws:ssm:eu-north-1:123456789012:parameter/external",
    ]
    assert _statement_by_sid(pool, "AllowedSecretsManagerSecretsRead")[
        "Resource"
    ] == [
        "arn:aws:secretsmanager:*:*:secret:sandbox-github-app*",
        "arn:aws:secretsmanager:eu-north-1:123456789012:secret:external-AbCd",
    ]
    assert _statement_by_sid(pool, "AllowedS3ReadWrite")["Resource"] == [
        "arn:aws:s3:::sandbox-artifact-bucket",
        "arn:aws:s3:::sandbox-artifact-bucket/*",
        "arn:aws:s3:::sandbox-cache-bucket",
        "arn:aws:s3:::sandbox-cache-bucket/ci_cache",
        "arn:aws:s3:::sandbox-cache-bucket/ci_cache/*",
        "arn:aws:s3:::sandbox-reports-bucket",
        "arn:aws:s3:::sandbox-reports-bucket/reports",
        "arn:aws:s3:::sandbox-reports-bucket/reports/*",
        "arn:aws:s3:::external-bucket/custom/*",
    ]


def test_runner_pool_can_allow_all_runner_external_resources():
    pool = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        allow_all_ssm_parameters=True,
        allow_all_secrets=True,
        allow_all_s3_prefixes=True,
    )

    assert _statement_by_sid(pool, "AllowedSSMParametersRead")["Resource"] == [
        "arn:aws:ssm:*:*:parameter/*"
    ]
    assert _statement_by_sid(pool, "AllowedSecretsManagerSecretsRead")[
        "Resource"
    ] == ["arn:aws:secretsmanager:*:*:secret:*"]
    assert _statement_by_sid(pool, "AllowedS3ReadWrite")["Resource"] == [
        "arn:aws:s3:::*",
        "arn:aws:s3:::*/*",
    ]


def test_runner_pool_accepts_custom_role_and_profile_configs():
    role = IAMRole.Config(
        name="custom-runner-role",
        trust_service="ec2.amazonaws.com",
    )
    profile = IAMInstanceProfile.Config(
        name="custom-runner-profile",
        role_name="custom-runner-role",
    )

    pool = RunnerPool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        scaling=RunnerPool.Scaling.Auto,
        size=0,
        max_size=1,
        ec2_role=role,
        instance_profile=profile,
    )

    assert pool.ec2_role is role
    assert pool.instance_profile is profile
    assert pool.launch_template.iam_instance_profile_name == "custom-runner-profile"


def test_cloud_config_prefixes_all_top_level_resource_types():
    cloud = CloudInfrastructure.Config(
        name="prefix-check",
        vpcs=[
            VPC.Config(
                name="ci",
                subnets=[VPC.Subnet(availability_zone="eu-north-1a")],
            )
        ],
        storages=[Storage.Config(name="artifacts", retention_days=7)],
        iam_roles=[
            IAMRole.Config(name="worker-role", trust_service="lambda.amazonaws.com")
        ],
        iam_instance_profiles=[
            IAMInstanceProfile.Config(name="worker-profile", role_name="worker-role")
        ],
        secret_parameters=[
            SecretParameter.Config(name="app-secret"),
            SecretParameter.Config(name="/path-secret"),
        ],
        sqs_queues=[SQSQueue.Config(name="jobs")],
        launch_templates=[
            LaunchTemplate.Config(
                name="runner-lt",
                image_id="ami-1234567890abcdef0",
                instance_type="t4g.small",
                vpc_name="ci",
                iam_instance_profile_name="worker-profile",
                security_group_names=["ci-sg"],
            )
        ],
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="runner",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="runner-lt",
            )
        ],
        lambda_functions=[
            Lambda.Config(
                name="worker",
                path=__file__,
                handler="handler.main",
                role_name="worker-role",
                secrets={"app-secret": "APP_SECRET", "/path-secret": "PATH_SECRET"},
            )
        ],
        image_builders=[
            ImageBuilder.Config(
                name="builder",
                instance_profile_name="worker-profile",
                vpc_name="ci",
                security_group_names=["ci-sg"],
                inline_components=[
                    {
                        "name": "builder-setup",
                        "platform": "Linux",
                        "commands": [
                            "test -x /opt/praktika/base-venvs/runtime/bin/python"
                        ],
                    }
                ],
                prebuilt_venvs=[
                    ImageBuilder.PrebuiltVenv(name="runtime", packages=["requests"])
                ],
            )
        ],
    )

    vpc = cloud.vpcs[0]
    storage = cloud.storages[0]
    role = cloud.iam_roles[0]
    profile = cloud.iam_instance_profiles[0]
    secret = cloud.secret_parameters[0]
    path_secret = cloud.secret_parameters[1]
    queue = cloud.sqs_queues[0]
    lt = cloud.launch_templates[0]
    asg = cloud.autoscaling_groups[0]
    lambda_cfg = cloud.lambda_functions[0]
    builder = cloud.image_builders[0]

    assert vpc.name == "prefix-check-ci"
    assert storage.name == "prefix-check-artifacts"
    assert role.name == "prefix-check-worker-role"
    assert profile.name == "prefix-check-worker-profile"
    assert profile.role_name == "prefix-check-worker-role"
    assert secret.name == "prefix-check-app-secret"
    assert path_secret.name == "/prefix-check-path-secret"
    assert queue.name == "prefix-check-jobs"
    assert lt.name == "prefix-check-runner-lt"
    assert lt.vpc_name == "prefix-check-ci"
    assert lt.iam_instance_profile_name == "prefix-check-worker-profile"
    assert lt.security_group_names == ["prefix-check-ci-sg"]
    assert asg.name == "prefix-check-runner"
    assert asg.vpc_name == "prefix-check-ci"
    assert asg.launch_template_name == "prefix-check-runner-lt"
    assert lambda_cfg.name == "prefix-check-worker"
    assert lambda_cfg.role_name == "prefix-check-worker-role"
    assert lambda_cfg.secrets == {
        "prefix-check-app-secret": "APP_SECRET",
        "/prefix-check-path-secret": "PATH_SECRET",
    }
    assert builder.name == "prefix-check-builder"
    assert builder.image_recipe_name == "prefix-check-builder-recipe"
    assert (
        builder.infrastructure_configuration_name
        == "prefix-check-builder-imagebuilder-infra"
    )
    assert (
        builder.distribution_configuration_name
        == "prefix-check-builder-imagebuilder-dist"
    )
    assert builder.image_pipeline_name == "prefix-check-builder-imagebuilder-pipeline"
    assert builder.ami_name == "prefix-check-builder-{{ imagebuilder:buildDate }}"
    assert builder.instance_profile_name == "prefix-check-worker-profile"
    assert builder.vpc_name == "prefix-check-ci"
    assert builder.security_group_names == ["prefix-check-ci-sg"]
    assert builder.inline_components[0]["name"] == "prefix-check-builder-setup"
    assert builder.inline_components[0]["commands"] == [
        "test -x /opt/praktika/base-venvs/runtime/bin/python"
    ]
    assert builder.prebuilt_venvs[0].name == "runtime"


def test_cloud_project_namespace_keeps_image_builder_venv_paths_project_local():
    cloud = CloudInfrastructure.Config(
        name="silk",
        image_builders=[
            ImageBuilder.Config(
                name="ci-ubuntu-x86_64-image",
                inline_components=[
                    {
                        "name": "praktika-controller-ubuntu-image-test",
                        "platform": "Linux",
                        "phase": "test",
                        "commands": [
                            "test -x /opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python",
                            "/opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python -m pip show praktika",
                        ],
                    }
                ],
                prebuilt_venvs=[
                    ImageBuilder.PrebuiltVenv(name="praktika-runtime-0.1.2")
                ],
            )
        ],
    )

    builder = cloud.image_builders[0]

    assert builder.prebuilt_venvs[0].name == "praktika-runtime-0.1.2"
    assert builder.inline_components[0]["commands"] == [
        "test -x /opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python",
        "/opt/praktika/base-venvs/praktika-runtime-0.1.2/bin/python -m pip show praktika",
    ]


def test_cloud_project_namespace_does_not_rewrite_controller_local_paths():
    builder = Components.create_ubuntu_image_builder_config(
        name="ci-arm64-image",
        version="1.0.0",
        controller_package=(
            "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/"
            f"packages{_PRAKTIKA_CONTROLLER_LATEST_WHEEL}"
        ),
        prebuilt_venvs=[
            ImageBuilder.PrebuiltVenv(name="praktika-runtime-0.1.2")
        ],
        instance_types=["t4g.small"],
    )

    cloud = CloudInfrastructure.Config(
        name="silk",
        storages=[Storage.Config(name="artifacts-eu-north-1", retention_days=30)],
        image_builders=[builder],
    )
    builder = cloud.image_builders[0]
    runtime_component = next(
        component
        for component in builder.inline_components
        if component["description"]
        == "Install Praktika controller runtime dependencies into the image"
    )
    controller_component = next(
        component
        for component in builder.inline_components
        if component["description"] == "Bake the Praktika controller service into the image"
    )
    runtime_commands = "\n".join(runtime_component["commands"])
    commands = "\n".join(controller_component["commands"])
    unit = _decode_embedded_file(
        next(
            cmd
            for cmd in controller_component["commands"]
            if "/etc/systemd/system/praktika-controller.service" in cmd
        )
    )

    assert runtime_component["name"] == "silk-praktika-controller-ubuntu-runtime"
    assert "https://praktika-artifacts-eu-north-1.s3.amazonaws.com" in runtime_commands
    assert "praktika-silk-artifacts-eu-north-1" not in runtime_commands
    assert controller_component["name"] == "silk-praktika-controller"
    assert "/usr/local/bin/praktika-controller-start" in commands
    assert "/etc/systemd/system/praktika-controller.service" in commands
    assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
    assert "silk-praktika-controller-start" not in commands
    assert "silk-praktika-controller.service" not in commands


def test_cloud_deploy_runs_lambdas_before_image_backed_compute(monkeypatch):
    calls = []

    cloud = CloudInfrastructure.Config(
        name="deploy-order",
        lambda_functions=[
            Lambda.Config(
                name="webhook",
                path=__file__,
                handler="handler.main",
                role_name="lambda-role",
            )
        ],
        image_builders=[
            ImageBuilder.Config(
                name="builder",
            )
        ],
        launch_templates=[
            LaunchTemplate.Config(
                name="runner-lt",
                image_id="ami-1234567890abcdef0",
                instance_type="t4g.small",
            )
        ],
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="runner-asg",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="runner-lt",
            )
        ],
    )
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1", EVENT_FEED_S3_PATH="")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    for role in cloud.iam_roles:
        monkeypatch.setattr(
            role,
            "deploy",
            lambda role=role: calls.append(f"role:{role.name}"),
        )
    for profile in cloud.iam_instance_profiles:
        monkeypatch.setattr(
            profile,
            "deploy",
            lambda profile=profile: calls.append(f"profile:{profile.name}"),
        )
    monkeypatch.setattr(
        cloud.lambda_functions[0],
        "deploy",
        lambda: calls.append(f"lambda:{cloud.lambda_functions[0].name}"),
    )
    monkeypatch.setattr(
        cloud.image_builders[0],
        "deploy",
        lambda: calls.append(f"imagebuilder:{cloud.image_builders[0].name}"),
    )
    monkeypatch.setattr(
        cloud.launch_templates[0],
        "deploy",
        lambda: calls.append(f"lt:{cloud.launch_templates[0].name}"),
    )
    monkeypatch.setattr(
        cloud.autoscaling_groups[0],
        "deploy",
        lambda: calls.append(f"asg:{cloud.autoscaling_groups[0].name}"),
    )

    cloud.deploy()

    assert calls == [
        "role:deploy-order-imagebuilder-role",
        "profile:deploy-order-imagebuilder-profile",
        "lambda:deploy-order-webhook",
        "imagebuilder:deploy-order-builder",
        "lt:deploy-order-runner-lt",
        "asg:deploy-order-runner-asg",
    ]


def test_cloud_deploy_prints_deferred_asg_warning_at_end(monkeypatch, capsys):
    cloud = CloudInfrastructure.Config(
        name="praktika",
        autoscaling_groups=[
            AutoScalingGroup.Config(
                name="workflow-orchestrator",
                vpc_name="ci",
                min_size=0,
                max_size=1,
                desired_capacity=0,
                launch_template_name="workflow-orchestrator-lt",
            )
        ],
    )
    cloud._settings = SimpleNamespace(AWS_REGION="eu-north-1", EVENT_FEED_S3_PATH="")
    monkeypatch.setattr(cloud, "_verify_account", lambda: None)

    def _defer_asg():
        cloud.autoscaling_groups[0].ext.update(
            {
                "deferred_missing_launch_template": True,
                "deployment_warning": (
                    "Launch Template is not available yet for ASG "
                    "'praktika-workflow-orchestrator'; skipping until the launch template exists"
                ),
            }
        )

    monkeypatch.setattr(cloud.autoscaling_groups[0], "deploy", _defer_asg)

    cloud.deploy(only=["ASG"])

    output = capsys.readouterr().out.rstrip()
    assert "WARNING: Infrastructure deployment completed with warnings" in output
    assert (
        "WARNING: Launch Template is not available yet for ASG "
        "'praktika-workflow-orchestrator'; skipping until the launch template exists"
    ) in output
    assert output.endswith(
        "WARNING: Rerun is required after the missing launch template exists."
    )


def test_controller_image_builders_are_declared():
    for name, ami_name, instance_type in [
        ("ci-arm64-image", "ci-arm64-{{ imagebuilder:buildDate }}", "t4g.small"),
        ("ci-x86_64-image", "ci-x86_64-{{ imagebuilder:buildDate }}", "t3.small"),
    ]:
        builder = _IMAGE_BUILDERS_BY_NAME[name]

        assert builder.ami_launch_permission == {}
        assert builder.ami_name == ami_name
        assert builder.instance_types == [instance_type]
        assert [component["name"] for component in builder.inline_components] == [
            "praktika-controller-setup",
            "praktika-controller-runtime",
            "praktika-controller",
        ]
        runtime_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-controller-runtime"
        )
        setup_commands = "\n".join(builder.inline_components[0]["commands"])
        runtime_commands = "\n".join(runtime_component["commands"])
        assert "python3.12 python3.12-pip" in setup_commands
        assert "dnf install -y --allowerasing" in setup_commands
        assert "awscli-exe-linux-$(uname -m).zip" in setup_commands
        assert not any(
            "dnf install" in command and "awscli" in command
            for command in builder.inline_components[0]["commands"]
        )
        assert "python3.12 -m pip install" in runtime_commands
        assert any(
            "amazon-cloudwatch-agent" in cmd
            for cmd in builder.inline_components[0]["commands"]
        )
        assert any(
            _PRAKTIKA_CONTROLLER_BASE_WHEEL in cmd
            for cmd in runtime_component["commands"]
        )
        assert builder.prebuilt_venvs[0].name == "praktika-runtime"
        packages = builder.prebuilt_venvs[0].packages
        assert "pytest>=7.0.0" in packages
        # Praktika's runtime deps (boto3/PyJWT/cryptography/requests) come from
        # the `infrastructure` extra rather than being enumerated.
        assert any(
            pkg.startswith("praktika[infrastructure] @ ") for pkg in packages
        )
        assert (
            _IMAGE_BUILDERS_BY_NAME[name]
            .prebuilt_venvs[0]
            .packages[-1]
            .endswith(_PRAKTIKA_BASE_WHEEL)
        )
        agent_component = next(
            component
            for component in builder.inline_components
            if component["name"] == "praktika-controller"
        )
        launcher = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "praktika-controller-start" in cmd and "printf" in cmd
            )
        )
        unit = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/etc/systemd/system/praktika-controller.service" in cmd
                and "printf" in cmd
            )
        )
        cloudwatch_configure = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/usr/local/bin/praktika-configure-cloudwatch-agent" in cmd
                and "printf" in cmd
            )
        )
        assert "praktika_role" in launcher
        assert "praktika_queue" in launcher
        assert "praktika_project_slug" in launcher
        assert "export PRAKTIKA_PROJECT_SLUG" in launcher
        assert "SQS_QUEUE_NAME" not in launcher
        assert "exec /usr/local/bin/praktika-controller" in launcher
        assert "ExecStart=/usr/local/bin/praktika-controller-start" in unit
        assert "StandardOutput=append:/var/log/praktika-controller.log" in unit
        assert "StandardError=append:/var/log/praktika-controller.log" in unit
        assert "EnvironmentFile=-/etc/praktika/praktika-controller.env" not in unit
        assert (
            "latest/meta-data/tags/instance/praktika_project_slug"
            in cloudwatch_configure
        )
        assert '"file_path": "/var/log/praktika-controller.log"' in cloudwatch_configure
        assert (
            '"log_group_name": "/${PRAKTIKA_PROJECT_SLUG}/praktika-controller"'
            in cloudwatch_configure
        )


def test_ubuntu_runner_pool_uses_ubuntu_image_builder():
    builder = _IMAGE_BUILDERS_BY_NAME["ci-ubuntu-x86_64-image"]
    pool = next(pool for pool in _runner_pools if pool.name == "amd-2xsmall-ubuntu")
    setup_commands = "\n".join(builder.inline_components[0]["commands"])
    runtime_commands = builder.inline_components[1]["commands"]

    assert pool.image_builder is builder
    assert builder.ami_name == "ci-ubuntu-x86_64-{{ imagebuilder:buildDate }}"
    assert builder.image_tests_enabled is True
    assert builder.image_tests_timeout_minutes == 60
    assert builder.instance_types == ["t3.small"]
    assert [component["name"] for component in builder.inline_components] == [
        "praktika-controller-ubuntu-setup",
        "praktika-controller-ubuntu-runtime",
        "praktika-controller",
        "praktika-controller-ubuntu-image-test",
        "praktika-project-image-test",
    ]
    test_component = builder.inline_components[3]
    test_commands = "\n".join(test_component["commands"])
    assert test_component["phase"] == "test"
    assert "python3.12 -m pip show praktika-controller" in test_commands
    assert "docker buildx version" in test_commands
    assert "amazon-cloudwatch-agent-ctl" in test_commands
    assert "/opt/praktika/base-venvs/praktika-runtime/bin/python" in test_commands
    custom_test_component = builder.inline_components[4]
    assert custom_test_component["phase"] == "test"
    assert custom_test_component["commands"] == [
        "test -d /opt/praktika/work",
        "test -w /opt/praktika/work",
    ]
    assert builder.parent_image_resolver is not None
    assert "apt-get install --yes --no-install-recommends" in setup_commands
    assert "amazoncloudwatch-agent/ubuntu/${deb_arch}" in setup_commands
    assert "gpg --verify /tmp/amazon-cloudwatch-agent.deb.sig" in setup_commands
    assert "awscli-exe-linux-$(uname -m).zip" in setup_commands
    assert "download.docker.com/linux/ubuntu" in setup_commands
    assert "docker-ce docker-buildx-plugin docker-ce-cli containerd.io" in setup_commands
    assert "registry-mirrors" not in setup_commands
    assert "insecure-registries" not in setup_commands
    assert "dnf install" not in setup_commands
    assert any("--break-system-packages" in command for command in runtime_commands)
    assert any("--ignore-installed" in command for command in runtime_commands)
    assert not any("--force-reinstall" in command for command in runtime_commands)
    assert [lt.name for lt in builder.launch_templates] == [
        "amd-2xsmall-ubuntu-lt"
    ]


def test_project_image_builders_register_expected_launch_templates():
    assert [
        lt.name for lt in _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"].launch_templates
    ] == [
        "arm-2xsmall-lt",
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-lt",
        "workflow-orchestrator-base-lt",
    ]
    assert [
        lt.name for lt in _IMAGE_BUILDERS_BY_NAME["ci-x86_64-image"].launch_templates
    ] == ["amd-2xsmall-lt"]
    assert [
        lt.name
        for lt in _IMAGE_BUILDERS_BY_NAME[
            "ci-ubuntu-x86_64-image"
        ].launch_templates
    ] == ["amd-2xsmall-ubuntu-lt"]


def test_all_image_builders_stay_private():
    for name in [
        "ci-arm64-image",
        "ci-x86_64-image",
        "ci-ubuntu-x86_64-image",
    ]:
        assert _IMAGE_BUILDERS_BY_NAME[name].ami_launch_permission == {}


def test_project_image_builders_rely_on_settings_region_defaults():
    for builder in _IMAGE_BUILDERS_BY_NAME.values():
        assert builder.region == ""
        assert builder.regions == []


def test_project_github_token_minter_uses_defaults_and_project_repo_scope():
    cloud = _get_infra_config("praktika")
    gh_token_minter = cloud.github_token_minters[0]
    assert gh_token_minter.name == "praktika-gh-token"
    assert gh_token_minter.role_name == "praktika-gh-token-role"
    assert gh_token_minter.secret_name == "praktika-gh-app-echt"
    assert gh_token_minter.repositories == ["praktika"]
    assert gh_token_minter.permissions["contents"] == "write"

    runner = next(pool for pool in cloud.runner_pools if pool.name == "arm-2xsmall")
    orchestrator = cloud.orchestrator_pool
    runner_invoke = runner.ec2_role.inline_policies["GitHubTokenMinterInvoke"][
        "Statement"
    ]
    orchestrator_invoke = orchestrator.ec2_role.inline_policies[
        "GitHubTokenMinterInvoke"
    ]["Statement"]

    assert any("lambda:InvokeFunction" in stmt["Action"] for stmt in runner_invoke)
    assert any(
        "lambda:InvokeFunction" in stmt["Action"] for stmt in orchestrator_invoke
    )
    assert runner.launch_template.tags["praktika_project_slug"] == "praktika"
    assert orchestrator.launch_template.tags["praktika_project_slug"] == "praktika"


def test_project_runner_pools_allow_only_required_ssm_parameters():
    cloud = _get_infra_config("praktika")

    for pool in cloud.runner_pools:
        assert (
            "arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess"
            not in pool.ec2_role.policy_arns
        )
        assert (
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
            not in pool.ec2_role.policy_arns
        )
        assert pool.allowed_ssm_parameters == list(_RUNNER_ALLOWED_SSM_PARAMETERS)
        assert pool.allowed_secrets == []
        assert pool.allowed_secrets == list(_RUNNER_ALLOWED_SECRETS)
        assert pool.allowed_s3_prefixes == [
            f"praktika-{_RUNNER_ALLOWED_S3_PREFIXES[0]}"
        ]
        assert pool.allow_all_ssm_parameters is _RUNNER_ALLOW_ALL_SSM_PARAMETERS
        assert pool.allow_all_secrets is _RUNNER_ALLOW_ALL_SECRETS
        assert pool.allow_all_s3_prefixes is _RUNNER_ALLOW_ALL_S3_PREFIXES
        assert pool.allow_ssm_debug is _RUNNER_ALLOW_SSM_DEBUG
        runner_access = pool.ec2_role.inline_policies["RunnerAccess"]["Statement"]
        assert all(stmt.get("Sid") != "SSMManagedInstanceCore" for stmt in runner_access)
        assert all(stmt.get("Sid") != "SSMMessages" for stmt in runner_access)
        assert all(stmt.get("Sid") != "EC2Messages" for stmt in runner_access)
        assert _statement_by_sid(pool, "AllowedSSMParametersRead") == {
            "Sid": "AllowedSSMParametersRead",
            "Effect": "Allow",
            "Action": ["ssm:GetParameter", "ssm:GetParameters"],
            "Resource": [
                f"arn:aws:ssm:*:*:parameter/{_RUNNER_ALLOWED_SSM_PARAMETERS[0]}"
            ],
        }
        assert all(
            stmt.get("Sid") != "AllowedSecretsManagerSecretsRead"
            for stmt in runner_access
        )
        assert _statement_by_sid(pool, "AllowedS3ReadWrite")["Resource"] == [
            f"arn:aws:s3:::praktika-{_RUNNER_ALLOWED_S3_PREFIXES[0]}",
            f"arn:aws:s3:::praktika-{_RUNNER_ALLOWED_S3_PREFIXES[0]}/*",
        ]


def test_base_runner_pool_uses_base_image_without_bootstrap_user_data():
    pool = next(pool for pool in _runner_pools if pool.name == "arm-2xsmall-base")

    assert pool.image_builder is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    assert "praktika-configure-cloudwatch-agent" in pool.launch_template.user_data
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config" in pool.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller" in pool.launch_template.user_data
    )
    assert "pip install --force-reinstall" not in pool.launch_template.user_data
    assert pool.queue.name == "arm-2xsmall-base"


def test_non_base_runner_pools_patch_praktika_into_shared_base_venv():
    for pool_name in [
        "arm-2xsmall",
        "amd-2xsmall",
        "amd-2xsmall-ubuntu",
    ]:
        pool = next(pool for pool in _runner_pools if pool.name == pool_name)

        assert (
            pool.image_builder.prebuilt_venvs[0]
            .packages[-1]
            .endswith(_PRAKTIKA_BASE_WHEEL)
        )
        assert (
            "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
            in pool.launch_template.user_data
        )
        assert "praktika-configure-cloudwatch-agent" in pool.launch_template.user_data
        assert (
            "amazon-cloudwatch-agent-ctl -a fetch-config"
            in pool.launch_template.user_data
        )
        assert _PRAKTIKA_LATEST_WHEEL.lstrip("/") in pool.launch_template.user_data
        assert (
            _PRAKTIKA_CONTROLLER_LATEST_WHEEL in pool.launch_template.user_data
        )
        assert (
            "systemctl enable --now praktika-controller"
            in pool.launch_template.user_data
        )

    ubuntu = next(pool for pool in _runner_pools if pool.name == "amd-2xsmall-ubuntu")
    ubuntu_user_data = ubuntu.launch_template.user_data
    assert (
        ubuntu_user_data.index("praktika-configure-cloudwatch-agent")
        < ubuntu_user_data.index(_PRAKTIKA_CONTROLLER_LATEST_WHEEL)
    )
    assert (
        "python3.12 -m pip install --ignore-installed"
        in ubuntu.launch_template.user_data
    )
    assert (
        "python3.12 -m pip install --force-reinstall"
        not in ubuntu.launch_template.user_data
    )


def test_shared_arm64_images_are_used_by_runner_and_orchestrator_pools():
    builder = _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]

    assert builder.ami_launch_permission == {}
    assert builder.ami_name == "ci-arm64-{{ imagebuilder:buildDate }}"
    assert builder.instance_types == ["t4g.small"]
    assert [lt.name for lt in builder.launch_templates] == [
        "arm-2xsmall-lt",
        "arm-2xsmall-base-lt",
        "workflow-orchestrator-lt",
        "workflow-orchestrator-base-lt",
    ]


def test_projects_orchestrator_pools_include_default_and_base_image_variants():
    assert _orchestrator_pool.name == "workflow-orchestrator"
    assert _orchestrator_pool.queue.name == "workflow-orchestrator"
    assert _orchestrator_pool.lambda_config.name == "workflow-orchestrator"
    assert _orchestrator_pool.image_builder is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    assert (
        _orchestrator_pool.image_builder.prebuilt_venvs[0]
        .packages[-1]
        .endswith(_PRAKTIKA_BASE_WHEEL)
    )
    assert (
        "praktika-configure-cloudwatch-agent"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "/opt/praktika/base-venvs/praktika-runtime/bin/python -m pip install --force-reinstall"
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        _PRAKTIKA_LATEST_WHEEL.lstrip("/")
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        _PRAKTIKA_CONTROLLER_LATEST_WHEEL
        in _orchestrator_pool.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in _orchestrator_pool.launch_template.user_data
    )
    assert _orchestrator_pool.launch_template.name == "workflow-orchestrator-lt"
    assert _orchestrator_pool.autoscaling_group.name == "workflow-orchestrator"
    assert (
        _orchestrator_pool.launch_template.tags["praktika_role"]
        == "workflow_orchestrator"
    )

    assert _orchestrator_pool_base.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.queue.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.name == "workflow-orchestrator-base"
    assert _orchestrator_pool_base.lambda_config.environments["SQS_QUEUE_NAME"] == (
        "workflow-orchestrator-base"
    )
    assert (
        _orchestrator_pool_base.image_builder
        is _IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]
    )
    assert (
        "praktika-configure-cloudwatch-agent"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "amazon-cloudwatch-agent-ctl -a fetch-config"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "systemctl enable --now praktika-controller"
        in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        "pip install --force-reinstall"
        not in _orchestrator_pool_base.launch_template.user_data
    )
    assert (
        _orchestrator_pool_base.launch_template.name == "workflow-orchestrator-base-lt"
    )
    assert (
        _orchestrator_pool_base.autoscaling_group.name == "workflow-orchestrator-base"
    )
    assert (
        _orchestrator_pool_base.launch_template.tags["praktika_role"]
        == "workflow_orchestrator"
    )
    assert (
        _orchestrator_pool_base.image_builder.prebuilt_venvs[0]
        .packages[-1]
        .endswith(_PRAKTIKA_BASE_WHEEL)
    )
    assert _orchestrator_pool_base.autoscaling_group.tags["praktika_queue"] == (
        "workflow-orchestrator-base"
    )
    assert _orchestrator_pool_base.capacity_reserve == 2
    assert (
        _orchestrator_pool_base.autoscaling_group.tags["praktika_capacity_reserve"]
        == "2"
    )

    assert _orchestrator_pool.lambda_role.name == "gh-webhook-role"
    assert _orchestrator_pool_base.lambda_role.name == "gh-webhook-role"
    assert _orchestrator_pool.webhook_secret.name == "gh-webhook-secret"
    assert _orchestrator_pool_base.webhook_secret.name == "gh-webhook-secret"


def test_orchestrator_pools_share_default_lambda_role_and_hmac_secret():
    from praktika.infrastructure.native.orchestrator_pool import OrchestratorPool

    pool_a = OrchestratorPool(
        name="orch-a",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
    )
    pool_b = OrchestratorPool(
        name="orch-b",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
    )

    assert pool_a.queue.name == pool_a.name == pool_a.lambda_config.name
    assert pool_b.queue.name == pool_b.name == pool_b.lambda_config.name
    assert pool_a.lambda_role.name == "gh-webhook-role"
    assert pool_b.lambda_role.name == "gh-webhook-role"
    assert pool_a.webhook_secret.name == "gh-webhook-secret"
    assert pool_b.webhook_secret.name == "gh-webhook-secret"


def test_orchestrator_pool_can_configure_allowed_push_branches_from_ext():
    pool = OrchestratorPool(
        name="orch",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        ext={"allowed_push_branches": ["develop", "release/1.0"]},
    )

    assert pool.lambda_config.environments["ALLOWED_PUSH_BRANCHES"] == (
        "develop,release/1.0"
    )


def test_orchestrator_pool_appends_ext_iam_statements_to_role_policy():
    stmt = {
        "Sid": "BedrockMantleInference",
        "Effect": "Allow",
        "Action": ["bedrock-mantle:CreateInference"],
        "Resource": "*",
    }
    with_stmt = OrchestratorPool(
        name="orch",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
        ext={"iam_statements": [stmt]},
    )
    without_stmt = OrchestratorPool(
        name="orch",
        instance_type="t4g.small",
        vpc_name="praktika-ci",
        size=1,
        max_size=1,
    )

    assert (
        stmt
        in with_stmt.ec2_role.inline_policies["WorkflowOrchestratorAccess"]["Statement"]
    )
    assert all(
        s.get("Sid") != "BedrockMantleInference"
        for s in without_stmt.ec2_role.inline_policies["WorkflowOrchestratorAccess"][
            "Statement"
        ]
    )


def test_projects_grant_bedrock_to_both_orchestrator_pools():
    from ci.infrastructure.projects import (
        _orchestrator_pool,
        _orchestrator_pool_base,
    )

    def _sids(pool):
        return [
            s.get("Sid")
            for s in pool.ec2_role.inline_policies["WorkflowOrchestratorAccess"][
                "Statement"
            ]
        ]

    assert "BedrockMantleInference" in _sids(_orchestrator_pool)
    assert "BedrockMantleInference" in _sids(_orchestrator_pool_base)


def test_native_configs_accept_ext_maps():
    runner = RunnerPool(
        name="runner",
        instance_type="t4g.small",
        scaling=RunnerPool.Scaling.Disabled,
        size=1,
        max_size=1,
        ext={"owner": "ci"},
    )
    token_minter = GitHubTokenMinter(ext={"owner": "ci"})
    autoscaler_pool = PoolAutoscaler.Pool(name="runner", ext={"owner": "ci"})
    autoscaler = PoolAutoscaler(pools=[autoscaler_pool], ext={"owner": "ci"})
    cidb = CIDBCluster(ext={"owner": "ci"})
    cloud = CloudInfrastructure.Config(name="project", ext={"owner": "ci"})

    assert runner.ext == {"owner": "ci"}
    assert token_minter.ext == {"owner": "ci"}
    assert autoscaler_pool.ext == {"owner": "ci"}
    assert autoscaler.ext == {"owner": "ci"}
    assert cidb.ext == {"owner": "ci"}
    assert cloud.ext == {"owner": "ci"}
    assert '"ext"' not in autoscaler.lambda_config.environments["POOLS_CONFIG_JSON"]
