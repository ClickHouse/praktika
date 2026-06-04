from praktika import Job, Workflow
from praktika.mangle import _update_workflow_with_native_jobs
from praktika.orchestrator import find_workflows_for_event
from praktika.settings import Settings


def _make_pr_workflow(name: str, *, orchestrator_filter: str = ""):
    return Workflow.Config(
        name=name,
        event=Workflow.Event.PULL_REQUEST,
        base_branches=["main"],
        orchestrator_filter=orchestrator_filter,
        jobs=[
            Job.Config(
                name="User Job",
                runs_on=["arm-2xsmall"],
                command="true",
            )
        ],
    )


def test_default_orchestrator_skips_base_workflows(monkeypatch):
    default_workflow = _make_pr_workflow("default")
    base_workflow = _make_pr_workflow("base", orchestrator_filter="base")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("SQS_QUEUE_NAME", "workflow-orchestrator")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [default_workflow, base_workflow],
    )

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["default"]


def test_base_orchestrator_skips_default_workflows(monkeypatch):
    default_workflow = _make_pr_workflow("default")
    base_workflow = _make_pr_workflow("base", orchestrator_filter="base")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("SQS_QUEUE_NAME", "workflow-orchestrator-base")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [default_workflow, base_workflow],
    )

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["base"]


def test_native_jobs_can_follow_base_runner_override():
    workflow = Workflow.Config(
        name="base workflow",
        event=Workflow.Event.PULL_REQUEST,
        base_branches=["main"],
        enable_report=True,
        post_hooks=["echo done"],
        native_job_runs_on=["arm-2xsmall-base"],
        jobs=[
            Job.Config(
                name="User Job",
                runs_on=["arm-2xsmall-base"],
                command="true",
            )
        ],
    )

    _update_workflow_with_native_jobs(workflow)

    assert workflow.jobs[0].name == Settings.CI_CONFIG_JOB_NAME
    assert workflow.jobs[0].runs_on == ["arm-2xsmall-base"]
    assert workflow.jobs[-1].name == Settings.FINISH_WORKFLOW_JOB_NAME
    assert workflow.jobs[-1].runs_on == ["arm-2xsmall-base"]
