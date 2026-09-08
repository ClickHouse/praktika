from praktika.infrastructure.cloud import CloudInfrastructure
from praktika.infrastructure import Components, ImageBuilder, Storage, VPC
from ci.settings.settings import SECRET_CI_DB_CONNECTION, SECRET_DOCKER_REGISTRY


_PRAKTIKA_PACKAGE_BASE_URL = (
    "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages"
)
_PRAKTIKA_BASE_VERSION = "0.1.9"
# The baked AMI venv pins an exact Praktika version so image builds are
# reproducible and a version bump forces a fresh AMI (see _image_builders).
_PRAKTIKA_BASE_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/praktika-{_PRAKTIKA_BASE_VERSION}-py3-none-any.whl"
)
_PRAKTIKA_CONTROLLER_BASE_VERSION = "0.1.3"
_PRAKTIKA_CONTROLLER_BASE_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/"
    f"praktika_controller-{_PRAKTIKA_CONTROLLER_BASE_VERSION}-py3-none-any.whl"
)
# The latest controller wheel uses the same fixed, version-less "latest" S3
# location as praktika. MainCI publish scripts also mirror it to a major.minor
# compat version.
_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME = "praktika_controller-0.0.0-py3-none-any.whl"
_PRAKTIKA_CONTROLLER_WHL = (
    f"{_PRAKTIKA_PACKAGE_BASE_URL}/latest/{_PRAKTIKA_CONTROLLER_LATEST_WHL_NAME}"
)
_RUNTIME_BASE_VENV = "praktika-runtime"


def _runtime_prebuilt_venvs():
    # The `infrastructure` extra pulls Praktika's runtime deps
    # (boto3/PyJWT/cryptography/requests) automatically; pytest and the Bedrock
    # AI SDK are optional extras the runner/orchestrator need, so list them
    # explicitly. The orchestrator's AI advisor (AI_PROVIDER="bedrock-anthropic") imports
    # `anthropic[bedrock]` lazily at decide() time; baking it into this shared
    # venv keeps it present on every AMI — including the base pool, which has no
    # boot-time user_data to pip-install into (harmless on job runners).
    return [
        ImageBuilder.PrebuiltVenv(
            name=_RUNTIME_BASE_VENV,
            packages=[
                "pytest>=7.0.0",
                "pytest-reportlog>=0.4.0",
                "anthropic[bedrock]",
                "ruff==0.15.21",
                f"praktika[infrastructure] @ {_PRAKTIKA_BASE_WHL}",
            ],
            description=(
                "Shared Python base venv: Praktika (+infrastructure extra), "
                "pytest, ruff, and the Bedrock AI SDK"
            ),
        ),
    ]


def _custom_image_tests():
    return [
        Components.create_image_test_component(
            name="praktika-project-image-test",
            commands=[
                "test -d /opt/praktika/work",
                "test -w /opt/praktika/work",
            ],
        ),
    ]


def _image_builders():
    # All recipes share the praktika-controller build component
    # (_praktika_controller_component), whose Image Builder version is taken from
    # the recipe version. Component versions are IMMUTABLE: re-registering an
    # existing name+version silently keeps the old content (see
    # ImageBuilder._ensure_inline_components). So the recipes MUST share one
    # version and bump together -- otherwise a "new" version for one recipe can
    # collide with a praktika-controller version already created by another,
    # shipping a stale build component against fresh test assertions (which is
    # exactly what broke the ubuntu build when awslinux/ubuntu versions skewed).
    #
    # Bump on any change to the baked venv contents (_runtime_prebuilt_venvs) or
    # the baked controller component (_praktika_controller_component).
    recipe_version = "1.0.18"

    return [
        Components.create_awslinux_image_builder_config(
            name="ci-arm64-image",
            version=recipe_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t4g.small"],
        ),
        Components.create_awslinux_image_builder_config(
            name="ci-x86_64-image",
            version=recipe_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            instance_types=["t3.small"],
        ),
        Components.create_ubuntu_image_builder_config(
            name="ci-ubuntu-x86_64-image",
            version=recipe_version,
            controller_package=_PRAKTIKA_CONTROLLER_BASE_WHL,
            prebuilt_venvs=_runtime_prebuilt_venvs(),
            components=_custom_image_tests(),
            instance_types=["t3.small"],
        ),
    ]


_IMAGE_BUILDERS = _image_builders()
_IMAGE_BUILDERS_BY_NAME = {builder.name: builder for builder in _IMAGE_BUILDERS}
_RUNNER_ALLOWED_SSM_PARAMETERS = [
    SECRET_CI_DB_CONNECTION,
    SECRET_DOCKER_REGISTRY,
]
_RUNNER_ALLOWED_SECRETS = []
_RUNNER_ALLOWED_S3_PREFIXES = ["artifacts-eu-north-1"]
_RUNNER_ALLOW_ALL_SSM_PARAMETERS = False
_RUNNER_ALLOW_ALL_SECRETS = False
_RUNNER_ALLOW_ALL_S3_PREFIXES = False
_RUNNER_ALLOW_SSM_DEBUG = False

def _runner_user_data(controller_update_cmd: str) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -xeuo pipefail",
            "",
            controller_update_cmd,
            "# Add any host customization you need above this line.",
            "/usr/local/bin/praktika-configure-cloudwatch-agent",
            "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
            # Praktika itself is installed at run time from the pool's
            # `ext["runtime_source"]` (the run's checkout, `.`), not baked into
            # the base venv here. See docs/installing-praktika.md.
            "systemctl enable --now praktika-controller",
            "",
        ]
    )


# OSS trust boundary for the repo-snapshot tiers. Snapshots live under
# praktika-artifacts-eu-north-1/repo-snapshots/v1/PRs/... (written by fork /
# pull_request runs, which route to the pr-* pools via the PR workflow's
# runs_on_label_prefix="pr-") and .../repo-snapshots/v1/REFs/... (written by push
# runs on non-pr pools). PRs/ is the untrusted tier, REFs/ the trusted one. The
# runner S3 grant is bucket-wide for convenience (fine on a private project with
# no untrusted actor), so on this OSS project we carve the tiers back out per pool
# with explicit Deny statements (Deny always overrides Allow). Information-flow
# rule: reads may go down-trust but never up, and writes never go up.
#
#   untrusted (pr-*) pool: may READ REFs (reuse), must NOT WRITE REFs (no poisoning)
#   trusted (non-pr) pool: must NOT READ or WRITE PRs (no tainted input into trusted)
#
# The tiers are empty until ENABLE_S3_REPO_SNAPSHOT is set, so these denies are
# inert for existing CI. Bare bucket names are namespaced to
# praktika-artifacts-eu-north-1 by the deploy-time policy sweep.
_UNTRUSTED_DENY_WRITE_TRUSTED_STATEMENT = {
    "Sid": "DenyUntrustedWriteToTrustedSnapshots",
    "Effect": "Deny",
    "Action": [
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
    ],
    "Resource": "arn:aws:s3:::artifacts-eu-north-1/repo-snapshots/v1/REFs/*",
}
_TRUSTED_DENY_ACCESS_UNTRUSTED_STATEMENT = {
    "Sid": "DenyTrustedAccessToUntrustedSnapshots",
    "Effect": "Deny",
    "Action": [
        "s3:GetObject",
        "s3:GetObjectTagging",
        "s3:PutObject",
        "s3:PutObjectTagging",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
    ],
    "Resource": "arn:aws:s3:::artifacts-eu-north-1/repo-snapshots/v1/PRs/*",
}


def _runner_pool(
    name: str,
    instance_type: str,
    image_builder: str,
    max_size: int = 10,
    user_data: str = "",
    ext=None,
    untrusted: bool = False,
    runtime_source: str = ".",
):
    ext = dict(ext) if ext is not None else {}
    # Append (don't replace) so pools that already carry ext statements (e.g. the
    # bedrock pool) keep them.
    trust_deny = (
        _UNTRUSTED_DENY_WRITE_TRUSTED_STATEMENT
        if untrusted
        else _TRUSTED_DENY_ACCESS_UNTRUSTED_STATEMENT
    )
    ext["iam_statements"] = list(ext.get("iam_statements", [])) + [trust_deny]
    # Praktika is developed in this repo, so by default these pools install
    # Praktika at run time from each run's own checkout (`runtime_source="."`)
    # instead of the AMI-baked version — a PR's Praktika changes are tested by
    # that PR's own CI, and diverged branches each run their own Praktika. The
    # `-base` pool opts out (runtime_source="") to stay pinned to the AMI as a
    # stable reference. See docs/installing-praktika.md.
    if runtime_source:
        ext["runtime_source"] = runtime_source
    return Components.RunnerPool(
        name=name,
        instance_type=instance_type,
        scaling=Components.RunnerPool.Scaling.Auto,
        size=0,
        max_size=max_size,
        image_builder=_IMAGE_BUILDERS_BY_NAME[image_builder],
        allowed_ssm_parameters=list(_RUNNER_ALLOWED_SSM_PARAMETERS),
        allowed_secrets=list(_RUNNER_ALLOWED_SECRETS),
        allowed_s3_prefixes=list(_RUNNER_ALLOWED_S3_PREFIXES),
        allow_all_ssm_parameters=_RUNNER_ALLOW_ALL_SSM_PARAMETERS,
        allow_all_secrets=_RUNNER_ALLOW_ALL_SECRETS,
        allow_all_s3_prefixes=_RUNNER_ALLOW_ALL_S3_PREFIXES,
        allow_ssm_debug=_RUNNER_ALLOW_SSM_DEBUG,
        user_data=user_data,
        ext=ext,
    )


# The Code Review job (`praktika review`) calls an OpenAI model on Bedrock via
# the Converse API, which requires bedrock:InvokeModel. Only the dedicated
# code-review runner pool below carries this grant (scoped to Bedrock
# foundation-model / inference-profile resources) -- general job runners stay
# Bedrock-less, so an arbitrary job cannot reach the model API.
_CODE_REVIEW_BEDROCK_IAM_STATEMENT = {
    "Sid": "BedrockRuntimeInference",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:*:inference-profile/*",
    ],
}

_runner_pools = [
    _runner_pool(
        name="arm-2xsmall",
        instance_type="t4g.small",
        image_builder="ci-arm64-image",
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    _runner_pool(
        name="arm-2xsmall-base",
        instance_type="t4g.small",
        image_builder="ci-arm64-image",
        # Runs praktika_pr_simple, a pull_request workflow (untrusted), despite the
        # non-"pr-" name — so it must be untrusted: allowed the PRs/ snapshot tier,
        # denied writes to REFs/.
        untrusted=True,
        # Stays pinned to the AMI-baked Praktika (no runtime install) as a stable
        # reference, unlike the other pools which run the checkout's Praktika.
        runtime_source="",
    ),
    _runner_pool(
        name="amd-2xsmall",
        instance_type="t3.small",
        image_builder="ci-x86_64-image",
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    _runner_pool(
        name="amd-2xsmall-ubuntu",
        instance_type="t3.small",
        image_builder="ci-ubuntu-x86_64-image",
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --ignore-installed {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    _runner_pool(
        name="pr-arm-2xsmall",
        instance_type="t4g.small",
        image_builder="ci-arm64-image",
        untrusted=True,
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    _runner_pool(
        name="pr-amd-2xsmall",
        instance_type="t3.small",
        image_builder="ci-x86_64-image",
        untrusted=True,
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    _runner_pool(
        name="pr-amd-2xsmall-ubuntu",
        instance_type="t3.small",
        image_builder="ci-ubuntu-x86_64-image",
        untrusted=True,
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --ignore-installed {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
    # Dedicated pool for the AI Code Review job. Identical to arm-2xsmall, plus a
    # scoped bedrock:InvokeModel grant via ext["iam_statements"] so only this
    # pool's role can call the Bedrock model API.
    _runner_pool(
        name="pr-arm-2xsmall-bedrock",
        instance_type="t4g.small",
        image_builder="ci-arm64-image",
        untrusted=True,
        ext={"iam_statements": [_CODE_REVIEW_BEDROCK_IAM_STATEMENT]},
        user_data=_runner_user_data(
            "# Update the controller if changed (to test new version w/o image rebuild)\n"
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages"
        ),
    ),
]

# The AI advisor (AI_PROVIDER="bedrock") reaches Claude through Bedrock Runtime,
# which the orchestrator's instance role must be allowed to invoke. Both
# orchestrator pools share one role and get the grant (the base pool doesn't run
# the advisor today, but the permission is harmless there).
_ORCHESTRATOR_BEDROCK_IAM_STATEMENT = {
    "Sid": "BedrockRuntimeInference",
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel"],
    "Resource": "*",
}

_orchestrator_pool = Components.OrchestratorPool(
    name="workflow-orchestrator",
    instance_type="t4g.small",
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    capacity_reserve=0,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
    ext={
        "iam_statements": [_ORCHESTRATOR_BEDROCK_IAM_STATEMENT],
        # No external_pr_autoapprove_paths: with runtime_source="." this
        # orchestrator installs and runs the checkout's Praktika under its
        # trusted IAM role, so autoapproving external-PR pushes (even scoped to
        # `praktika/*`) would let an untrusted head supply the engine. External
        # PRs go through manual approval instead.
        # Ship kernel/OOM/systemd-kill evidence to /praktika/praktika-system so
        # a silently killed controller (e.g. OOM) leaves a trace. See
        # docs/logging.md.
        "system_logs": True,
        # Run the orchestrator on the checkout's Praktika (not the AMI-baked
        # version), so PR/branch changes to the engine are exercised in
        # orchestration too. The `-base` orchestrator stays pinned. See
        # docs/installing-praktika.md.
        "runtime_source": ".",
    },
    user_data="\n".join(
        [
            "#!/usr/bin/env bash",
            "set -xeuo pipefail",
            "",
            "# Update the controller if changed (to test new version w/o image rebuild)",
            f"python3.12 -m pip install --force-reinstall {_PRAKTIKA_CONTROLLER_WHL} --break-system-packages",
            "# Add any host customization you need above this line.",
            "/usr/local/bin/praktika-configure-cloudwatch-agent",
            "/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -c file:/etc/praktika/amazon-cloudwatch-agent.json -s",
            # Praktika itself is installed at run time from ext["runtime_source"]
            # ("."), not baked into the base venv here.
            "systemctl enable --now praktika-controller",
            "",
        ]
    ),
)

_orchestrator_pool_base = Components.OrchestratorPool(
    name="workflow-orchestrator-base",
    instance_type="t4g.small",
    scaling=Components.OrchestratorPool.Scaling.Auto,
    size=0,
    max_size=10,
    capacity_reserve=2,
    image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"],
    ext={
        "iam_statements": [_ORCHESTRATOR_BEDROCK_IAM_STATEMENT],
        # See docs/logging.md; captures OOM/kill traces for the controller.
        "system_logs": True,
    },
)

_cidb_cluster = Components.CIDBCluster(
    instance_type="t4g.large",
    size=1,
)

PROJECTS = [
    CloudInfrastructure.Config(
        name="praktika",
        min_praktika_version="0.1.6",
        vpcs=[
            VPC.Config(
                subnets=[
                    VPC.Subnet(availability_zone="eu-north-1a"),
                ],
            )
        ],
        storages=[
            Storage.Config(name="artifacts-eu-north-1", retention_days=90, public=True),
        ],
        report_pages=[
            Components.report_page_config,
        ],
        image_builders=_IMAGE_BUILDERS,
        github_token_minters=[Components.GitHubTokenMinter(secret_name="gh-app-echt")],
        orchestrator_pools=[_orchestrator_pool, _orchestrator_pool_base],
        runner_pools=_runner_pools,
        cidb_cluster=_cidb_cluster,
    ),
]
