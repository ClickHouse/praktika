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

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [default_workflow, base_workflow],
    )

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["default"]


def test_workflow_name_filter_selects_one_matching_workflow(monkeypatch):
    pr_fast = _make_pr_workflow("PR Fast")
    pr_full = _make_pr_workflow("PR Full")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [pr_fast, pr_full],
    )

    matched = find_workflows_for_event(event, workflow_name="PR Full")

    assert [wf.name for wf in matched] == ["PR Full"]


def test_workflow_name_filter_ignores_orchestrator_pool_filter(monkeypatch):
    base_workflow = _make_pr_workflow("Praktika CI", orchestrator_filter="base")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [base_workflow],
    )

    matched = find_workflows_for_event(event, workflow_name="Praktika CI")

    assert [wf.name for wf in matched] == ["Praktika CI"]


def test_base_orchestrator_skips_default_workflows(monkeypatch):
    default_workflow = _make_pr_workflow("default")
    base_workflow = _make_pr_workflow("base", orchestrator_filter="base")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator-base")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda: [default_workflow, base_workflow],
    )

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["base"]


def _make_ignition_workflow(name, event, *, engine=Workflow.Engine.GH_IGNITION):
    return Workflow.Config(
        name=name,
        event=event,
        engine=engine,
        branches=["main"],
        cron_schedules=["23 2 * * *"] if event == Workflow.Event.SCHEDULE else [],
        jobs=[Job.Config(name="User Job", runs_on=["arm-2xsmall"], command="true")],
    )


def test_schedule_event_routes_to_ignition_workflow(monkeypatch):
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE)
    # workflow_name is carried in the message, not passed as a kwarg (the SQS
    # consumer forwards the raw event only).
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda: [wf])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Nightly"]


def test_dispatch_event_routes_to_ignition_workflow(monkeypatch):
    wf = _make_ignition_workflow("Release", Workflow.Event.DISPATCH)
    event = {"type": "dispatch", "head_ref": "main", "workflow_name": "Release"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda: [wf])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Release"]


def test_schedule_message_name_filters_to_one_workflow(monkeypatch):
    a = _make_ignition_workflow("Nightly A", Workflow.Event.SCHEDULE)
    b = _make_ignition_workflow("Nightly B", Workflow.Event.SCHEDULE)
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly B"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda: [a, b])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Nightly B"]


def test_gh_actions_schedule_workflow_is_skipped(monkeypatch):
    # A schedule workflow still on the GH_ACTIONS engine runs its cron via
    # generated YAML, so the orchestrator must ignore it.
    wf = _make_ignition_workflow(
        "GH Nightly", Workflow.Event.SCHEDULE, engine=Workflow.Engine.GH_ACTIONS
    )
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "GH Nightly"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda: [wf])

    matched = find_workflows_for_event(event)

    assert matched == []


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
