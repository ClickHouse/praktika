import pytest

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
    monkeypatch.setattr(Settings, "USE_CUSTOM_GH_AUTH", False)
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
