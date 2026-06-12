import base64
import sys
from pathlib import Path
from types import SimpleNamespace

from praktika.__main__ import create_parser, main
from praktika.interactive import UserPrompt
from praktika.mangle import _get_infra_config, _get_workflows
from praktika.project_init import (
    detect_aws_account_ids,
    detect_aws_profile_account_ids,
    detect_default_branch,
    detect_aws_profiles,
    has_nested_git_repositories,
    has_praktika_project_files,
    _prompt_aws_account_id,
    _prompt_aws_profile,
    run_init_interactive,
    _validate_aws_profile,
)
from praktika.settings import Settings
from praktika.version import current_praktika_version


def _decode_embedded_file(command: str) -> str:
    payload = command.split("'")[3]
    return base64.b64decode(payload).decode("utf-8")


def test_init_parser_supports_command():
    parser = create_parser()
    args = parser.parse_args(["init"])

    assert args.command == "init"


def test_has_praktika_project_files_detects_markers(tmp_path):
    assert has_praktika_project_files(tmp_path) is False

    settings = tmp_path / "ci/settings/settings.py"
    settings.parent.mkdir(parents=True)
    settings.write_text("AWS_REGION = 'us-east-1'\n", encoding="utf8")

    assert has_praktika_project_files(tmp_path) is True


def test_has_nested_git_repositories_detects_workspace_layout(tmp_path):
    nested_repo = tmp_path / "child-repo"
    nested_repo.mkdir()
    (nested_repo / ".git").mkdir()

    assert has_nested_git_repositories(tmp_path) is True


def test_detect_default_branch_prefers_origin_head(monkeypatch, tmp_path):
    calls = iter(
        [
            SimpleNamespace(returncode=0, stdout="origin/trunk\n"),
            SimpleNamespace(returncode=0, stdout="feature-branch\n"),
        ]
    )

    monkeypatch.setattr(
        "praktika.project_init.subprocess.run",
        lambda *args, **kwargs: next(calls),
    )

    assert detect_default_branch(tmp_path) == "trunk"


def test_detect_aws_profiles_reads_config_and_credentials(tmp_path, monkeypatch):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    config_path = aws_dir / "config"
    credentials_path = aws_dir / "credentials"
    config_path.write_text(
        "[default]\nregion=us-east-1\n[profile Box]\nregion=eu-north-1\n",
        encoding="utf8",
    )
    credentials_path.write_text(
        "[personal]\naws_access_key_id=test\naws_secret_access_key=test\n",
        encoding="utf8",
    )

    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_path))

    assert detect_aws_profiles() == {"default", "Box", "personal"}


def test_detect_aws_account_ids_reads_values_from_local_aws_files(
    tmp_path, monkeypatch
):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    config_path = aws_dir / "config"
    credentials_path = aws_dir / "credentials"
    config_path.write_text(
        "[default]\nsso_account_id=123456789012\n[profile Box]\naccount_id=210987654321\n",
        encoding="utf8",
    )
    credentials_path.write_text(
        "[personal]\naws_access_key_id=test\naws_secret_access_key=test\n",
        encoding="utf8",
    )

    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_path))

    assert detect_aws_account_ids() == {"123456789012", "210987654321"}


def test_detect_aws_profile_account_ids_reads_values_per_profile(tmp_path, monkeypatch):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    config_path = aws_dir / "config"
    credentials_path = aws_dir / "credentials"
    config_path.write_text(
        "[default]\nsso_account_id=123456789012\n[profile Box]\naccount_id=210987654321\n",
        encoding="utf8",
    )
    credentials_path.write_text(
        "[personal]\naccount_id=609927696493\naws_access_key_id=test\naws_secret_access_key=test\n",
        encoding="utf8",
    )

    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_path))

    assert detect_aws_profile_account_ids() == {
        "default": {"123456789012"},
        "Box": {"210987654321"},
        "personal": {"609927696493"},
    }


def test_validate_aws_profile_requires_existing_profile_when_available(monkeypatch):
    monkeypatch.setattr(
        "praktika.project_init.detect_aws_profiles",
        lambda: {"default", "Box"},
    )

    assert _validate_aws_profile("Box") is True
    assert _validate_aws_profile("missing") is False


def test_prompt_aws_profile_retries_with_available_profiles(monkeypatch, capsys):
    answers = iter(["B", "Box"])

    monkeypatch.setattr(
        "praktika.project_init.detect_aws_profiles",
        lambda: {"default", "Box"},
    )
    monkeypatch.setattr(
        UserPrompt,
        "_safe_input",
        staticmethod(lambda _: next(answers)),
    )

    assert _prompt_aws_profile(default="default") == "Box"
    out = capsys.readouterr().out
    assert "Unknown AWS profile [B]" in out
    assert "Available profiles: Box, default" in out


def test_prompt_aws_account_id_auto_uses_profile_match(monkeypatch, capsys):
    monkeypatch.setattr(
        "praktika.project_init.detect_aws_profile_account_ids",
        lambda: {"Box": {"123456789012"}},
    )

    assert _prompt_aws_account_id(profile="Box") == "123456789012"
    out = capsys.readouterr().out
    assert (
        "Using AWS account ID [123456789012] from local config for profile [Box]" in out
    )


def test_prompt_aws_account_id_retries_with_available_account_ids(monkeypatch, capsys):
    answers = iter(["1", "123456789012"])

    monkeypatch.setattr(
        "praktika.project_init.detect_aws_profile_account_ids",
        lambda: {},
    )
    monkeypatch.setattr(
        "praktika.project_init.detect_aws_account_ids",
        lambda: {"123456789012", "210987654321"},
    )
    monkeypatch.setattr(
        UserPrompt,
        "_safe_input",
        staticmethod(lambda _: next(answers)),
    )

    assert _prompt_aws_account_id() == "123456789012"
    out = capsys.readouterr().out
    assert "Invalid AWS account ID [1]" in out
    assert "Available account IDs: 123456789012, 210987654321" in out


def test_run_init_interactive_writes_starter_project(tmp_path, monkeypatch):
    confirm_answers = iter([True, False])
    project_slug = tmp_path.name.replace("_", "-")
    string_answers = iter(
        [
            "main",
            "us-east-1",
            "us-east-1a",
            "awslinux",
        ]
    )

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda _: next(confirm_answers)),
    )
    monkeypatch.setattr(
        UserPrompt,
        "get_string",
        staticmethod(lambda *args, **kwargs: next(string_answers)),
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_profile",
        lambda default="default": "default",
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_account_id",
        lambda profile="": "123456789012",
    )

    written = run_init_interactive(tmp_path)

    written_rel = {path.relative_to(tmp_path).as_posix() for path in written}
    assert written_rel == {
        "ci/settings/settings.py",
        "ci/workflows/pull_request.py",
        "ci/workflows/main_ci.py",
        "ci/infrastructure/projects.py",
    }

    settings_path = tmp_path / "ci/settings/settings.py"
    pr_workflow_path = tmp_path / "ci/workflows/pull_request.py"
    main_ci_workflow_path = tmp_path / "ci/workflows/main_ci.py"
    infra_path = tmp_path / "ci/infrastructure/projects.py"

    settings_text = settings_path.read_text(encoding="utf8")
    pr_workflow_text = pr_workflow_path.read_text(encoding="utf8")
    main_ci_workflow_text = main_ci_workflow_path.read_text(encoding="utf8")
    infra_text = infra_path.read_text(encoding="utf8")

    assert 'AWS_REGION = "us-east-1"' in settings_text
    assert f'PROJECT_NAME = "{tmp_path.name}"' in settings_text
    assert f'PROJECT_SLUG = "{project_slug}"' in settings_text
    assert 'GH_AUTH_LAMBDA_NAME = f"{PROJECT_SLUG}-gh-token"' in settings_text
    assert 'S3_ARTIFACT_BUCKET = f"{PROJECT_SLUG}-artifacts"' in settings_text
    expected_base_venv = f"praktika-runtime-{current_praktika_version()}"
    assert f'PRAKTIKA_BASE_VENV = "{expected_base_venv}"' in settings_text
    assert (
        'CLOUD_INFRASTRUCTURE_CONFIG_PATH = "./ci/infrastructure/projects.py"'
        not in settings_text
    )
    assert 'PRAKTIKA_INSTALL_SOURCE = "."' not in settings_text
    assert 'SMALL_ARM = "arm-small"' in settings_text
    assert 'SMALL_AMD = "amd-small"' in settings_text
    assert 'MEDIUM_ARM = "arm-medium"' in settings_text
    assert 'MEDIUM_AMD = "amd-medium"' in settings_text
    assert 'name="Pull Request CI"' in pr_workflow_text
    assert 'base_branches=["main"]' in pr_workflow_text
    assert "enable_gh_summary_comment=True" in pr_workflow_text
    assert "enable_gh_summary_comment=True" not in main_ci_workflow_text
    assert 'name="Main CI"' in main_ci_workflow_text
    assert "event=Workflow.Event.PUSH" in main_ci_workflow_text
    assert 'branches=["main"]' in main_ci_workflow_text
    assert (
        "from ci.settings.settings import PROJECT_NAME, PROJECT_SLUG, PRAKTIKA_BASE_VENV"
        in infra_text
    )
    assert "from praktika.infrastructure import Components, Storage, VPC" in infra_text
    assert f'min_praktika_version="{current_praktika_version()}"' in infra_text
    assert "# until published in pip" in infra_text
    assert "Components.create_praktika_venv_config(" in infra_text
    assert "Components.create_image_test_component(" in infra_text
    assert 'name="project-image-test"' in infra_text
    assert "components=custom_image_tests" in infra_text
    assert "PRAKTIKA_BASE_VENV," in infra_text
    assert "_RUNTIME_BASE_VENV" not in infra_text
    assert f'"{current_praktika_version()}"' in infra_text
    assert (
        "https://praktika-artifacts-eu-north-1.s3.amazonaws.com/packages/"
        "praktika_controller-0.1.1-py3-none-any.whl"
    ) in infra_text
    assert "AWS_REGION" not in infra_text
    assert "Components.GitHubTokenMinter(" in infra_text
    assert "repositories=[PROJECT_NAME]" in infra_text
    assert 'name="gh-token"' not in infra_text
    assert 'secret_name="gh-app"' not in infra_text
    assert 'PROJECT_NAME = "' not in infra_text
    assert 'name="praktika-ci"' not in infra_text
    assert "CI_VPC_NAME" not in infra_text
    assert "region=CI_REGION" not in infra_text
    assert "capacity_reserve=1" in infra_text
    assert "def _controller_image_component(name: str):" not in infra_text
    assert "Components.create_awslinux_image_builder_config(" in infra_text
    assert infra_text.count("Components.create_awslinux_image_builder_config(") == 2
    assert "Components.create_ubuntu_image_builder_config(" not in infra_text
    assert "project_slug=" not in infra_text
    assert "SQS_QUEUE_NAME" not in infra_text
    assert "ImageBuilder.Config(" not in infra_text
    assert 'name="ci-arm64-image"' in infra_text
    assert 'name="ci-x86_64-image"' in infra_text
    assert "ami_name=" not in infra_text
    assert "image_pipeline_name=" not in infra_text
    assert "instance_profile_name=" not in infra_text
    assert "security_group_names=" not in infra_text
    assert "vpc_name=" not in infra_text
    assert "image_builders=_IMAGE_BUILDERS" in infra_text
    assert 'image_builder=_IMAGE_BUILDERS_BY_NAME["ci-arm64-image"]' in infra_text
    assert 'image_builder=_IMAGE_BUILDERS_BY_NAME["ci-x86_64-image"]' in infra_text
    assert "/etc/systemd/system/praktika-controller.service" not in infra_text
    assert '"log_group_name"' not in infra_text
    assert '"/praktika/controller"' not in infra_text
    assert 'name="artifacts"' in infra_text
    assert "public=False" in infra_text
    assert 'name="arm-small"' in infra_text
    assert 'name="amd-small"' in infra_text
    assert 'name="arm-medium"' in infra_text
    assert 'instance_type="c7g.4xlarge"' in infra_text
    assert 'name="amd-medium"' in infra_text
    assert 'instance_type="c7a.4xlarge"' in infra_text
    assert "max_size=50" in infra_text
    assert infra_text.count("max_size=50") == 5
    assert "volume_size_gb=30" in infra_text

    compile(settings_text, str(settings_path), "exec")
    compile(pr_workflow_text, str(pr_workflow_path), "exec")
    compile(main_ci_workflow_text, str(main_ci_workflow_path), "exec")
    compile(infra_text, str(infra_path), "exec")


def test_run_init_interactive_writes_configs_praktika_can_read(tmp_path, monkeypatch):
    confirm_answers = iter([True, False])
    string_answers = iter(
        [
            "main",
            "us-east-1",
            "us-east-1a",
            "awslinux",
        ]
    )

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda _: next(confirm_answers)),
    )
    monkeypatch.setattr(
        UserPrompt,
        "get_string",
        staticmethod(lambda *args, **kwargs: next(string_answers)),
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_profile",
        lambda default="default": "default",
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_account_id",
        lambda profile="": "123456789012",
    )

    run_init_interactive(tmp_path)

    monkeypatch.syspath_prepend(str(tmp_path))
    module_names = ("ci.settings.settings", "ci.settings")
    missing_module = object()
    previous_modules = {
        module_name: sys.modules.get(module_name, missing_module)
        for module_name in module_names
    }
    for module_name in module_names:
        sys.modules.pop(module_name, None)
    monkeypatch.setattr(
        Settings,
        "WORKFLOWS_DIRECTORY",
        str(tmp_path / "ci/workflows"),
    )
    monkeypatch.setattr(
        Settings,
        "CLOUD_INFRASTRUCTURE_CONFIG_PATH",
        str(tmp_path / "ci/infrastructure/projects.py"),
    )
    monkeypatch.setattr(Settings, "ENABLED_WORKFLOWS", None)
    monkeypatch.setattr(Settings, "DISABLED_WORKFLOWS", None)

    try:
        workflows = _get_workflows(_for_validation_check=True)
        cloud = _get_infra_config()
    finally:
        for module_name in module_names:
            sys.modules.pop(module_name, None)
        for module_name, previous_module in previous_modules.items():
            if previous_module is not missing_module:
                sys.modules[module_name] = previous_module

    def _builder_arch(builder):
        return "arm64" if builder.instance_types[0].startswith("t4g.") else "x86_64"

    builders_by_arch = {
        _builder_arch(builder): builder for builder in cloud.image_builders
    }

    assert {workflow.name for workflow in workflows} == {
        "Pull Request CI",
        "Main CI",
    }
    workflows_by_name = {workflow.name: workflow for workflow in workflows}
    assert (
        workflows_by_name["Pull Request CI"].jobs[0].command
        == "python3 -c 'print(\"hello from praktika\")'"
    )
    assert (
        workflows_by_name["Main CI"].jobs[0].command
        == "python3 -c 'print(\"hello from main ci\")'"
    )
    assert cloud.name == tmp_path.name
    assert cloud.min_praktika_version == current_praktika_version()
    assert cloud.orchestrator_pool.capacity_reserve == 1
    assert cloud.orchestrator_pool.max_size == 50
    assert {pool.name: pool.max_size for pool in cloud.runner_pools} == {
        "arm-small": 50,
        "amd-small": 50,
        "arm-medium": 50,
        "amd-medium": 50,
    }
    assert set(builders_by_arch) == {"arm64", "x86_64"}
    assert all(len(builder.inline_components) == 4 for builder in cloud.image_builders)
    project_slug = tmp_path.name.lower().replace("_", "-")
    assert {
        arch: builder.instance_profile_name
        for arch, builder in builders_by_arch.items()
    } == {
        "arm64": f"{project_slug}-arm-small-profile",
        "x86_64": f"{project_slug}-amd-small-profile",
    }
    assert {arch: builder.vpc_name for arch, builder in builders_by_arch.items()} == {
        "arm64": f"{project_slug}-vpc",
        "x86_64": f"{project_slug}-vpc",
    }
    assert {
        arch: builder.security_group_names for arch, builder in builders_by_arch.items()
    } == {
        "arm64": [f"{project_slug}-vpc-sg"],
        "x86_64": [f"{project_slug}-vpc-sg"],
    }
    for builder in cloud.image_builders:
        assert [component["name"] for component in builder.inline_components] == [
            f"{project_slug}-praktika-controller-setup",
            f"{project_slug}-praktika-controller-runtime",
            f"{project_slug}-praktika-controller",
            f"{project_slug}-project-image-test",
        ]
        project_test_component = builder.inline_components[3]
        assert project_test_component["phase"] == "test"
        assert project_test_component["commands"] == [
            "test -d /opt/praktika/work",
            "test -w /opt/praktika/work",
        ]
        agent_component = next(
            component
            for component in builder.inline_components
            if component["name"] == f"{project_slug}-praktika-controller"
        )
        cloudwatch_configure = _decode_embedded_file(
            next(
                cmd
                for cmd in agent_component["commands"]
                if "/usr/local/bin/praktika-configure-cloudwatch-agent" in cmd
                and "printf" in cmd
            )
        )
        assert (
            "latest/meta-data/tags/instance/praktika_project_slug"
            in cloudwatch_configure
        )
        assert (
            '"log_group_name": "/${PRAKTIKA_PROJECT_SLUG}/praktika-controller"'
            in cloudwatch_configure
        )
        assert (
            builder.prebuilt_venvs[0].name
            == f"praktika-runtime-{current_praktika_version()}"
        )
        assert {
            "boto3",
            "PyJWT",
            "cryptography",
            "requests",
            "pytest>=7.0.0",
        }.issubset(builder.prebuilt_venvs[0].packages)
        assert (
            builder.prebuilt_venvs[0]
            .packages[-1]
            .endswith(f"/praktika-{current_praktika_version()}-py3-none-any.whl")
        )
    assert cloud.orchestrator_pool.vpc_name == f"{project_slug}-vpc"
    assert cloud.orchestrator_pool.launch_template.vpc_name == f"{project_slug}-vpc"
    assert cloud.orchestrator_pool.autoscaling_group.vpc_name == f"{project_slug}-vpc"
    assert {pool.name: pool.vpc_name for pool in cloud.runner_pools} == {
        "arm-small": f"{project_slug}-vpc",
        "amd-small": f"{project_slug}-vpc",
        "arm-medium": f"{project_slug}-vpc",
        "amd-medium": f"{project_slug}-vpc",
    }
    assert cloud.orchestrator_pool.image_builder is builders_by_arch["arm64"]
    assert (
        cloud.orchestrator_pool.launch_template.image_builder
        is builders_by_arch["arm64"]
    )
    assert {
        pool.name: _builder_arch(pool.image_builder) for pool in cloud.runner_pools
    } == {
        "arm-small": "arm64",
        "amd-small": "x86_64",
        "arm-medium": "arm64",
        "amd-medium": "x86_64",
    }
    assert {
        pool.name: _builder_arch(pool.launch_template.image_builder)
        for pool in cloud.runner_pools
    } == {
        "arm-small": "arm64",
        "amd-small": "x86_64",
        "arm-medium": "arm64",
        "amd-medium": "x86_64",
    }


def test_run_init_interactive_supports_oss_storage_and_ubuntu_images(
    tmp_path, monkeypatch
):
    confirm_answers = iter([True, True])
    project_slug = tmp_path.name.replace("_", "-")
    string_answers = iter(
        [
            "main",
            "eu-north-1",
            "eu-north-1a",
            "ubuntu",
        ]
    )

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda _: next(confirm_answers)),
    )
    monkeypatch.setattr(
        UserPrompt,
        "get_string",
        staticmethod(lambda *args, **kwargs: next(string_answers)),
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_profile",
        lambda default="default": "default",
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_account_id",
        lambda profile="": "123456789012",
    )

    run_init_interactive(tmp_path)

    settings_path = tmp_path / "ci/settings/settings.py"
    infra_path = tmp_path / "ci/infrastructure/projects.py"
    settings_text = settings_path.read_text(encoding="utf8")
    infra_text = infra_path.read_text(encoding="utf8")

    assert (
        'S3_ARTIFACT_BUCKET = f"{PROJECT_SLUG}-artifacts-{AWS_REGION}"'
        in settings_text
    )
    assert "Components.create_ubuntu_image_builder_config(" in infra_text
    assert infra_text.count("Components.create_ubuntu_image_builder_config(") == 2
    assert "Components.create_image_test_component(" in infra_text
    assert "Components.create_awslinux_image_builder_config(" not in infra_text
    assert 'name="artifacts-eu-north-1"' in infra_text
    assert "public=True" in infra_text

    compile(settings_text, str(settings_path), "exec")
    compile(infra_text, str(infra_path), "exec")

    monkeypatch.syspath_prepend(str(tmp_path))
    module_names = ("ci.settings.settings", "ci.settings")
    missing_module = object()
    previous_modules = {
        module_name: sys.modules.get(module_name, missing_module)
        for module_name in module_names
    }
    for module_name in module_names:
        sys.modules.pop(module_name, None)
    monkeypatch.setattr(
        Settings,
        "CLOUD_INFRASTRUCTURE_CONFIG_PATH",
        str(tmp_path / "ci/infrastructure/projects.py"),
    )

    try:
        cloud = _get_infra_config()
    finally:
        for module_name in module_names:
            sys.modules.pop(module_name, None)
        for module_name, previous_module in previous_modules.items():
            if previous_module is not missing_module:
                sys.modules[module_name] = previous_module

    assert cloud.storages[0].name == f"{project_slug}-artifacts-eu-north-1"
    assert cloud.storages[0].public is True
    assert {
        component["name"]
        for builder in cloud.image_builders
        for component in builder.inline_components
    } >= {
        f"{project_slug}-praktika-controller-ubuntu-setup",
        f"{project_slug}-praktika-controller-ubuntu-runtime",
        f"{project_slug}-praktika-controller-ubuntu-image-test",
        f"{project_slug}-project-image-test",
    }

    captured_component_names = []

    class _Client:
        def list_components(self, **req):
            return {"componentVersionList": []}

        def create_component(self, **req):
            captured_component_names.append(req["name"])
            return {
                "componentBuildVersionArn": (
                    f"arn:component/{req['name']}/{req['semanticVersion']}/1"
                )
            }

    for builder in cloud.image_builders:
        monkeypatch.setattr(builder, "_client", lambda: _Client())
        builder._ensure_inline_components()

    assert all("." not in name for name in captured_component_names)
    assert f"{project_slug}-project-image-test" in captured_component_names
    assert {
        (
            f"{project_slug}-ci-arm64-image-"
            f"praktika-runtime-"
            f"{current_praktika_version().replace('.', '-')}-venv"
        ),
        (
            f"{project_slug}-ci-x86_64-image-"
            f"praktika-runtime-"
            f"{current_praktika_version().replace('.', '-')}-venv"
        ),
    }.issubset(captured_component_names)


def test_run_init_interactive_auto_creates_missing_settings_and_workflow(
    tmp_path, monkeypatch
):
    prompts = []
    answers = {
        "Default branch name": "main",
        "AWS region (for example us-east-1)": "us-east-1",
        "Primary availability zone": "us-east-1a",
    }

    monkeypatch.setattr(
        UserPrompt,
        "confirm",
        staticmethod(lambda question: prompts.append(question) or False),
    )
    monkeypatch.setattr(
        UserPrompt,
        "get_string",
        staticmethod(lambda question, **kwargs: answers[question]),
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_profile",
        lambda default="default": "default",
    )
    monkeypatch.setattr(
        "praktika.project_init._prompt_aws_account_id",
        lambda profile="": "123456789012",
    )

    written = run_init_interactive(tmp_path)

    written_rel = {path.relative_to(tmp_path).as_posix() for path in written}
    assert written_rel == {
        "ci/settings/settings.py",
        "ci/workflows/pull_request.py",
        "ci/workflows/main_ci.py",
    }
    assert prompts == [
        "Create ci/infrastructure/projects.py? Required only for standalone Praktika CI (not GitHub Actions), and only if this repo should manage the infrastructure.",
        "Is this an OSS project that should use public artifact storage?",
    ]


def test_main_without_args_prints_help_and_exits():
    try:
        main([])
    except SystemExit as ex:
        assert ex.code == 1
