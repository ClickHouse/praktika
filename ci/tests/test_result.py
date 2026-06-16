from pathlib import Path

from praktika.result import Result
from praktika.utils import Shell


def test_from_pytest_run_falls_back_without_reportlog(tmp_path, monkeypatch):
    commands = []
    pytest_log = tmp_path / "pytest.log"
    stdout_log = tmp_path / "pytest.stdout.log"

    def _fake_get_output(cls, command, strict=False, verbose=False, retries=1, delay=2):
        if command == "pytest --help":
            return "--junitxml"
        return ""

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
        commands.append(command)
        Path(log_file).write_text("E   AssertionError: boom\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(Shell, "get_output", classmethod(_fake_get_output))
    monkeypatch.setattr(Shell, "run", classmethod(_fake_run))

    result = Result.from_pytest_run(
        "./ci/tests/test_parser.py",
        name="Praktika Pytests",
        pytest_logfile=str(pytest_log),
        logfile=str(stdout_log),
    )

    assert result.status == Result.Status.FAIL
    assert result.results == []
    assert "--report-log" not in commands[0]
    assert result.files == [str(pytest_log), str(stdout_log)]
    assert "pytest-reportlog plugin is not installed" in result.info
    assert "AssertionError: boom" in result.info


def test_assets_live_in_ext():
    result = Result.create_from(
        name="job",
        status=Result.Status.OK,
        assets=["./asset.txt"],
    )

    assert result.ext["assets"] == ["./asset.txt"]
    assert "assets" not in Result.to_dict(result)
