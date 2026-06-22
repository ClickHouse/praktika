import shutil
import tempfile

from praktika.gh import GH
from praktika.gh_auth import GHAuth
from praktika.utils import Shell


def test_repo_name_from_git_remote_url():
    assert (
        GH._repo_name_from_git_remote_url("git@github.com:ClickHouse/praktika.git")
        == "ClickHouse/praktika"
    )
    assert (
        GH._repo_name_from_git_remote_url(
            "https://github.com/ClickHouse/praktika.git"
        )
        == "ClickHouse/praktika"
    )
    assert (
        GH._repo_name_from_git_remote_url(
            "ssh://git@github.com/ClickHouse/praktika.git"
        )
        == "ClickHouse/praktika"
    )


def test_gh_pages_url_normalizes_destination():
    assert (
        GH.gh_pages_url(repo="ClickHouse/praktika", destination_dir="/coverage/pr-1/")
        == "https://clickhouse.github.io/praktika/coverage/pr-1/"
    )


def test_gh_pages_destination_rejects_parent_traversal():
    try:
        GH.gh_pages_url(repo="ClickHouse/praktika", destination_dir="../bad")
    except ValueError as ex:
        assert "Invalid GitHub Pages destination" in str(ex)
    else:
        assert False, "Expected invalid destination to raise"


def test_publish_gh_pages_copies_source_and_pushes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("coverage", encoding="utf-8")

    temp_root = tmp_path / "publish"
    temp_root.mkdir()
    commands = []
    run_commands = []

    def _fake_mkdtemp(prefix):
        assert prefix == "praktika-gh-pages-"
        return str(temp_root)

    def _fake_check(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=False,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        **kwargs,
    ):
        commands.append(command)
        if command.startswith("git worktree add"):
            (temp_root / "worktree").mkdir(exist_ok=True)
        return True

    def _fake_run(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=True,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        retry_errors="",
        **kwargs,
    ):
        run_commands.append(command)
        if "ls-remote" in command:
            return 0
        if command.endswith("diff --cached --quiet"):
            return 1
        return 0

    real_rmtree = shutil.rmtree

    def _fake_rmtree(path, *args, **kwargs):
        if str(path) == str(temp_root):
            return
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", _fake_mkdtemp)
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(Shell, "check", classmethod(_fake_check))
    monkeypatch.setattr(Shell, "run", classmethod(_fake_run))

    url = GH.publish_gh_pages(
        str(source),
        repo="ClickHouse/praktika",
        destination_dir="/coverage/pr-1/",
        commit_message="publish coverage",
        github_token="ghs_test_token",
        verbose=False,
    )

    assert url == "https://clickhouse.github.io/praktika/coverage/pr-1/"
    assert (temp_root / "worktree" / "coverage" / "pr-1" / "index.html").read_text(
        encoding="utf-8"
    ) == "coverage"
    assert any(
        command.startswith("git remote add praktika-gh-pages-")
        and command.endswith(" https://github.com/ClickHouse/praktika.git")
        for command in commands
    )
    assert any(
        command.startswith("git fetch --depth=1 praktika-gh-pages-")
        and "+refs/heads/gh-pages:" in command
        for command in commands
    )
    assert (
        f"git -C {temp_root / 'worktree'} config user.name 'praktika[bot]'"
        in commands
    )
    assert (
        f"git -C {temp_root / 'worktree'} config user.email 'praktika[bot]@users.noreply.github.com'"
        in commands
    )
    assert any(
        command.startswith(f"git -C {temp_root / 'worktree'} push praktika-gh-pages-")
        and command.endswith(" HEAD:gh-pages")
        for command in commands
    )
    assert any(
        command.startswith("git worktree remove --force") for command in run_commands
    )
    assert any(
        command.startswith("git remote remove praktika-gh-pages-")
        for command in run_commands
    )
    assert all("ghs_test_token" not in command for command in commands + run_commands)


def test_publish_gh_pages_initializes_missing_branch_with_force_rm(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("coverage", encoding="utf-8")

    temp_root = tmp_path / "publish"
    temp_root.mkdir()
    commands = []
    run_commands = []

    def _fake_mkdtemp(prefix):
        assert prefix == "praktika-gh-pages-"
        return str(temp_root)

    def _fake_check(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=False,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        **kwargs,
    ):
        commands.append(command)
        if command.startswith("git worktree add"):
            (temp_root / "worktree").mkdir(exist_ok=True)
        return True

    def _fake_run(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=True,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        retry_errors="",
        **kwargs,
    ):
        run_commands.append(command)
        if "ls-remote" in command:
            return 2
        if command.endswith("diff --cached --quiet"):
            return 1
        return 0

    real_rmtree = shutil.rmtree

    def _fake_rmtree(path, *args, **kwargs):
        if str(path) == str(temp_root):
            return
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tempfile, "mkdtemp", _fake_mkdtemp)
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(Shell, "check", classmethod(_fake_check))
    monkeypatch.setattr(Shell, "run", classmethod(_fake_run))

    url = GH.publish_gh_pages(
        str(source),
        repo="ClickHouse/praktika",
        destination_dir="/coverage/pr-1/",
        commit_message="publish coverage",
        github_token="ghs_test_token",
        verbose=False,
    )

    assert url == "https://clickhouse.github.io/praktika/coverage/pr-1/"
    assert any(command.endswith(" checkout --orphan gh-pages") for command in commands)
    assert any(
        command.endswith(" rm -rf --ignore-unmatch .") for command in commands
    )
    assert not any(command.startswith("git fetch ") for command in commands)


def test_publish_gh_pages_requests_contents_write_token(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("coverage", encoding="utf-8")

    temp_root = tmp_path / "publish"
    temp_root.mkdir()
    requested_permissions = []

    def _fake_get_installation_token(cls, required_permissions=None):
        requested_permissions.append(required_permissions)
        return "ghs_test_token"

    def _fake_mkdtemp(prefix):
        assert prefix == "praktika-gh-pages-"
        return str(temp_root)

    def _fake_check(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=False,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        **kwargs,
    ):
        if command.startswith("git worktree add"):
            (temp_root / "worktree").mkdir(exist_ok=True)
        return True

    def _fake_run(
        cls,
        command,
        log_file=None,
        strict=False,
        verbose=True,
        dry_run=False,
        stdin_str=None,
        timeout=None,
        retries=1,
        retry_errors="",
        **kwargs,
    ):
        if "ls-remote" in command:
            return 0
        if command.endswith("diff --cached --quiet"):
            return 1
        return 0

    real_rmtree = shutil.rmtree

    def _fake_rmtree(path, *args, **kwargs):
        if str(path) == str(temp_root):
            return
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        GHAuth,
        "get_installation_token",
        classmethod(_fake_get_installation_token),
    )
    monkeypatch.setattr(tempfile, "mkdtemp", _fake_mkdtemp)
    monkeypatch.setattr(shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(Shell, "check", classmethod(_fake_check))
    monkeypatch.setattr(Shell, "run", classmethod(_fake_run))

    GH.publish_gh_pages(
        str(source),
        repo="ClickHouse/praktika",
        destination_dir="/coverage/pr-1/",
        commit_message="publish coverage",
        verbose=False,
    )

    assert requested_permissions == [{"contents": "write"}]
