from pathlib import Path
from types import SimpleNamespace

from praktika.__main__ import create_parser, main
from praktika.interactive import UserPrompt
from praktika.project_init import (
    detect_default_branch,
    detect_aws_profiles,
    has_nested_git_repositories,
    has_praktika_project_files,
    _prompt_aws_profile,
    run_init_interactive,
    should_auto_prompt_init,
    _validate_aws_profile,
)


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


def test_should_auto_prompt_init_skips_workspace_git_root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    nested_repo = tmp_path / "praktika"
    nested_repo.mkdir()
    (nested_repo / ".git").mkdir()

    monkeypatch.chdir(tmp_path)

    assert should_auto_prompt_init(tmp_path) is False


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


def test_run_init_interactive_writes_starter_project(tmp_path, monkeypatch):
    confirm_answers = iter([True, True, True])
    project_slug = tmp_path.name.replace("_", "-")
    string_answers = iter(
        [
            "main",
            "us-east-1",
            "us-east-1a",
            "123456789012",
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

    written = run_init_interactive(tmp_path)

    written_rel = {path.relative_to(tmp_path).as_posix() for path in written}
    assert written_rel == {
        "ci/settings/settings.py",
        "ci/workflows/pull_request.py",
        "ci/infrastructure/projects.py",
    }

    settings_path = tmp_path / "ci/settings/settings.py"
    workflow_path = tmp_path / "ci/workflows/pull_request.py"
    infra_path = tmp_path / "ci/infrastructure/projects.py"

    settings_text = settings_path.read_text(encoding="utf8")
    workflow_text = workflow_path.read_text(encoding="utf8")
    infra_text = infra_path.read_text(encoding="utf8")

    assert 'AWS_REGION = "us-east-1"' in settings_text
    assert f'PROJECT_NAME = "{tmp_path.name}"' in settings_text
    assert f'PROJECT_SLUG = "{project_slug}"' in settings_text
    assert 'GH_AUTH_LAMBDA_NAME = f"{PROJECT_SLUG}-gh-token"' in settings_text
    assert 'S3_ARTIFACT_PATH = f"{PROJECT_SLUG}-artifacts"' in settings_text
    assert 'SMALL_ARM = "arm-small"' in settings_text
    assert 'SMALL_AMD = "amd-small"' in settings_text
    assert 'MEDIUM_ARM = "arm-medium"' in settings_text
    assert 'MEDIUM_AMD = "amd-medium"' in settings_text
    assert 'base_branches=["main"]' in workflow_text
    assert "from ci.settings.settings import AWS_REGION, PROJECT_NAME, PROJECT_SLUG" in infra_text
    assert 'name="praktika-gh-token"' not in infra_text
    assert 'name="gh-token"' in infra_text
    assert 'secret_name="gh-app"' in infra_text
    assert 'PROJECT_NAME = "' not in infra_text
    assert 'name="praktika-ci"' not in infra_text
    assert 'CI_VPC_NAME = f"{PROJECT_SLUG}-ci"' in infra_text
    assert 'name="artifacts"' in infra_text
    assert "public=False" in infra_text
    assert 'name="arm-small"' in infra_text
    assert 'name="amd-small"' in infra_text
    assert 'name="arm-medium"' in infra_text
    assert 'instance_type="c7g.4xlarge"' in infra_text
    assert 'name="amd-medium"' in infra_text
    assert 'instance_type="c7a.4xlarge"' in infra_text
    assert "volume_size_gb=30" in infra_text

    compile(settings_text, str(settings_path), "exec")
    compile(workflow_text, str(workflow_path), "exec")
    compile(infra_text, str(infra_path), "exec")


def test_main_without_args_prints_help_and_exits():
    try:
        main([])
    except SystemExit as ex:
        assert ex.code == 1
