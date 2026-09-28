from praktika import Job, Workflow
from praktika.settings import Settings
from praktika.validator import Validator


def _run_validator_for_workflow(monkeypatch, workflow):
    def _fake_get_workflows(*args, **kwargs):
        file_names = kwargs.get("_file_names_out")
        if isinstance(file_names, list):
            file_names.append("test_workflow")
        return [workflow]

    monkeypatch.setattr(Settings, "CLOUD_INFRASTRUCTURE_CONFIG_PATH", "")
    monkeypatch.setattr(Settings, "ENABLED_WORKFLOWS", None)
    monkeypatch.setattr(Settings, "DISABLED_WORKFLOWS", None)
    monkeypatch.setattr(Settings, "VALIDATE_FILE_PATHS", False)
    monkeypatch.setattr("praktika.validator._get_workflows", _fake_get_workflows)

    Validator.validate()


def test_validator_allows_job_commit_status_for_praktika_workflow(
    monkeypatch, capsys
):
    # enable_commit_status is harmless on the Praktika engine (it uses the
    # Checks API regardless), so validation must not reject it.
    workflow = Workflow.Config(
        name="native",
        event=Workflow.Event.PULL_REQUEST,
        jobs=[
            Job.Config(
                name="job",
                runs_on=["runner"],
                command="echo ok",
                enable_commit_status=True,
            )
        ],
    )

    _run_validator_for_workflow(monkeypatch, workflow)

    out = capsys.readouterr().out
    assert ".enable_commit_status is redundant" not in out


def test_validator_allows_failure_commit_status_for_praktika_workflow(
    monkeypatch, capsys
):
    # enable_commit_status_on_failure is harmless on the Praktika engine, so
    # validation must not reject it.
    workflow = Workflow.Config(
        name="native",
        event=Workflow.Event.PULL_REQUEST,
        jobs=[
            Job.Config(
                name="job",
                runs_on=["runner"],
                command="echo ok",
            )
        ],
        enable_commit_status_on_failure=True,
    )

    _run_validator_for_workflow(monkeypatch, workflow)

    out = capsys.readouterr().out
    assert ".enable_commit_status_on_failure is redundant" not in out


def _ignition_schedule_workflow(branches):
    return Workflow.Config(
        name="ignition",
        event=Workflow.Event.SCHEDULE,
        engine=Workflow.Engine.GH_IGNITION,
        branches=branches,
        cron_schedules=["30 2 * * *"],
        jobs=[Job.Config(name="job", runs_on=["runner"], command="ruff check .")],
    )


def test_validator_rejects_ignition_without_branches(monkeypatch):
    import pytest

    workflow = _ignition_schedule_workflow(branches=[])
    with pytest.raises(SystemExit):
        _run_validator_for_workflow(monkeypatch, workflow)


def test_validator_allows_ignition_with_branches(monkeypatch):
    workflow = _ignition_schedule_workflow(branches=["main"])
    _run_validator_for_workflow(monkeypatch, workflow)  # no SystemExit


def test_validator_rejects_ignition_for_unsupported_event(monkeypatch):
    import pytest

    workflow = Workflow.Config(
        name="ignition-pr",
        event=Workflow.Event.PULL_REQUEST,
        engine=Workflow.Engine.GH_IGNITION,
        base_branches=["main"],
        jobs=[Job.Config(name="job", runs_on=["runner"], command="ruff check .")],
    )
    with pytest.raises(SystemExit):
        _run_validator_for_workflow(monkeypatch, workflow)
