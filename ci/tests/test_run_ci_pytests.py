import os
import tarfile

from ci.scripts import run_ci_pytests


class _FakeResult:
    def __init__(self):
        self.completed = False
        self.info = ""

    def complete_job(self):
        self.completed = True

    def set_error(self):
        raise AssertionError("set_error should not be called")


def test_run_ci_pytests_defaults_to_plain_pytest(monkeypatch, tmp_path):
    commands = []
    envs = []
    fake_result = _FakeResult()
    monkeypatch.delenv("PRAKTIKA_ENABLE_COVERAGE", raising=False)
    monkeypatch.setattr(
        run_ci_pytests.Result,
        "from_pytest_run",
        lambda *args, **kwargs: commands.append(kwargs["pytest_command"])
        or envs.append(kwargs["env"])
        or fake_result,
    )
    monkeypatch.setattr(
        run_ci_pytests,
        "COVERAGE_HTML_DIR",
        tmp_path / "coverage" / "html",
    )
    monkeypatch.setattr(
        run_ci_pytests,
        "COVERAGE_HTML_ARCHIVE",
        tmp_path / "coverage-html.tar.gz",
    )

    run_ci_pytests.main()

    assert commands == ["pytest"]
    assert envs[0]["PYTHONPATH"].split(os.pathsep)[:2] == [
        str(run_ci_pytests.REPO_ROOT),
        str(run_ci_pytests.BOOTSTRAP_SRC),
    ]
    assert fake_result.completed is True
    assert not (tmp_path / "coverage-html.tar.gz").exists()


def test_run_ci_pytests_generates_coverage_archive_when_enabled(monkeypatch, tmp_path):
    commands = []
    fake_result = _FakeResult()
    coverage_dir = tmp_path / "coverage" / "html"
    archive = tmp_path / "coverage-html.tar.gz"
    monkeypatch.setenv("PRAKTIKA_ENABLE_COVERAGE", "1")
    monkeypatch.setattr(
        run_ci_pytests.Result,
        "from_pytest_run",
        lambda *args, **kwargs: commands.append(kwargs["pytest_command"])
        or fake_result,
    )
    monkeypatch.setattr(run_ci_pytests, "COVERAGE_HTML_DIR", coverage_dir)
    monkeypatch.setattr(run_ci_pytests, "COVERAGE_HTML_ARCHIVE", archive)

    def _fake_check(command, verbose=False):
        assert command == f"coverage html -d {coverage_dir}"
        coverage_dir.mkdir(parents=True, exist_ok=True)
        (coverage_dir / "index.html").write_text("coverage", encoding="utf-8")
        return True

    monkeypatch.setattr(run_ci_pytests.Shell, "check", _fake_check)

    run_ci_pytests.main()

    assert commands == ["coverage run -m pytest"]
    assert fake_result.completed is True
    with tarfile.open(archive, "r:gz") as tar:
        assert "index.html" in tar.getnames()
