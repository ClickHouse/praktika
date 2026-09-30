import pytest
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
        lambda *a, **k: [default_workflow, base_workflow],
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
        lambda *a, **k: [pr_fast, pr_full],
    )

    matched = find_workflows_for_event(event, workflow_name="PR Full")

    assert [wf.name for wf in matched] == ["PR Full"]


def test_workflow_name_filter_ignores_orchestrator_pool_filter(monkeypatch):
    base_workflow = _make_pr_workflow("Praktika CI", orchestrator_filter="base")
    event = {"type": "pull_request", "base_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr(
        "praktika.orchestrator._get_workflows",
        lambda *a, **k: [base_workflow],
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
        lambda *a, **k: [default_workflow, base_workflow],
    )

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["base"]


def _make_ignition_workflow(
    name, event, *, engine=Workflow.Engine.GH_IGNITION, branches=("main",)
):
    return Workflow.Config(
        name=name,
        event=event,
        engine=engine,
        branches=list(branches),
        cron_schedules=["23 2 * * *"] if event == Workflow.Event.SCHEDULE else [],
        jobs=[Job.Config(name="User Job", runs_on=["arm-2xsmall"], command="true")],
    )


def test_schedule_event_routes_to_ignition_workflow(monkeypatch):
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE)
    # workflow_name is carried in the message, not passed as a kwarg (the SQS
    # consumer forwards the raw event only).
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Nightly"]


def test_dispatch_event_routes_to_ignition_workflow(monkeypatch):
    wf = _make_ignition_workflow("Release", Workflow.Event.DISPATCH)
    event = {"type": "dispatch", "head_ref": "main", "workflow_name": "Release"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Release"]


def test_dispatch_without_branches_runs_on_any_ref(monkeypatch):
    # No branches restriction: a manual dispatch from any ref chosen in the GH UI
    # must route, using that ref.
    wf = _make_ignition_workflow("Release", Workflow.Event.DISPATCH, branches=[])
    event = {
        "type": "dispatch",
        "head_ref": "feature/some-branch",
        "workflow_name": "Release",
    }

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event)

    assert [wf.name for wf in matched] == ["Release"]


def test_dispatch_with_branches_restricts_ref(monkeypatch):
    wf = _make_ignition_workflow(
        "Release", Workflow.Event.DISPATCH, branches=["main"]
    )
    event = {
        "type": "dispatch",
        "head_ref": "feature/x",
        "workflow_name": "Release",
    }

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event)

    assert matched == []  # ref not in the branches restriction


def test_ignition_event_name_does_not_bypass_pool_routing(monkeypatch):
    # An event-carried name must NOT bypass orchestrator_filter: a base-pool
    # workflow named in a message reaching the default pool must not run there.
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE, branches=[])
    wf.orchestrator_filter = "base"
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")  # default
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    assert find_workflows_for_event(event) == []


def test_ignition_event_name_runs_on_its_own_pool(monkeypatch):
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE, branches=[])
    wf.orchestrator_filter = "base"
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator-base")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    assert [w.name for w in find_workflows_for_event(event)] == ["Nightly"]


def test_explicit_name_arg_still_bypasses_pool_routing(monkeypatch):
    # The trusted CLI selector keeps its bypass: a base-pool workflow requested
    # by explicit --name runs regardless of the serving pool.
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE, branches=[])
    wf.orchestrator_filter = "base"
    event = {"type": "schedule", "head_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")  # default
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event, workflow_name="Nightly")
    assert [w.name for w in matched] == ["Nightly"]


def test_schedule_without_workflow_name_is_skipped(monkeypatch):
    # Guard against fan-out: an ignition event with no name matches nothing.
    wf = _make_ignition_workflow("Nightly", Workflow.Event.SCHEDULE, branches=[])
    event = {"type": "schedule", "head_ref": "main"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

    matched = find_workflows_for_event(event)

    assert matched == []


def test_schedule_message_name_filters_to_one_workflow(monkeypatch):
    a = _make_ignition_workflow("Nightly A", Workflow.Event.SCHEDULE)
    b = _make_ignition_workflow("Nightly B", Workflow.Event.SCHEDULE)
    event = {"type": "schedule", "head_ref": "main", "workflow_name": "Nightly B"}

    monkeypatch.setenv("PRAKTIKA_CONTROLLER_QUEUE", "workflow-orchestrator")
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *args, **kw: [a, b])

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
    monkeypatch.setattr("praktika.orchestrator._get_workflows", lambda *a, **k: [wf])

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


_GOOD_WF_FILE = '''\
from praktika import Job, Workflow

WORKFLOWS = [
    Workflow.Config(
        name="Good WF",
        event=Workflow.Event.PULL_REQUEST,
        base_branches=["main"],
        jobs=[Job.Config(name="User Job", runs_on=["arm-2xsmall"], command="true")],
    )
]
'''

# References an attribute that does not exist on Workflow.Engine — mirrors a pool
# whose baked praktika predates a newer engine used by a workflow file.
_BROKEN_WF_FILE = '''\
from praktika import Workflow

_ = Workflow.Engine.DOES_NOT_EXIST
WORKFLOWS = []
'''


def _write_workflows_dir(tmp_path):
    (tmp_path / "good_wf.py").write_text(_GOOD_WF_FILE, encoding="utf-8")
    (tmp_path / "broken_wf.py").write_text(_BROKEN_WF_FILE, encoding="utf-8")
    return tmp_path


def test_get_workflows_skips_broken_file_and_records_error(tmp_path, monkeypatch):
    from praktika import mangle

    _write_workflows_dir(tmp_path)
    monkeypatch.setattr(Settings, "WORKFLOWS_DIRECTORY", str(tmp_path))

    errors = []
    res = mangle._get_workflows(name="Good WF", _load_errors_out=errors)

    # The good workflow still loads despite the broken sibling file.
    assert [wf.name for wf in res] == ["Good WF"]
    # The broken file is surfaced (filename + error) rather than silently dropped.
    assert [f for f, _ in errors] == ["broken_wf.py"]
    assert "DOES_NOT_EXIST" in errors[0][1]


def test_get_workflows_reraises_broken_file_during_validation(tmp_path, monkeypatch):
    from praktika import mangle

    _write_workflows_dir(tmp_path)
    monkeypatch.setattr(Settings, "WORKFLOWS_DIRECTORY", str(tmp_path))

    # Validation must NOT swallow a broken workflow file (it runs under the
    # checkout's own praktika, so an import error is a genuine bug to surface).
    with pytest.raises(AttributeError):
        mangle._get_workflows(_for_validation_check=True)
