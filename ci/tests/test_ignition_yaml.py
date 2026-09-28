import yaml

from praktika import Job, Workflow
from praktika.parser import WorkflowConfigParser
from praktika.yaml_generator import IgnitionYamlGen


def _render(wf):
    parser = WorkflowConfigParser(wf).parse()
    return IgnitionYamlGen(parser).generate()


def _ignition_wf(name, event, *, crons=None, inputs=None):
    return Workflow.Config(
        name=name,
        event=event,
        engine=Workflow.Engine.GH_IGNITION,
        branches=["main"],
        cron_schedules=crons or [],
        inputs=inputs or [],
        jobs=[Job.Config(name="Work", runs_on=["arm-2xsmall"], command="true")],
    )


def test_schedule_ignition_renders_valid_yaml_with_cron():
    out = _render(_ignition_wf("Nightly", Workflow.Event.SCHEDULE, crons=["23 2 * * *"]))
    d = yaml.safe_load(out)
    on = d.get("on", d.get(True))  # bare `on:` parses as bool True key in YAML 1.1
    assert on["schedule"] == [{"cron": "23 2 * * *"}]
    assert "workflow_dispatch" in on
    assert list(d["jobs"].keys()) == ["ignite"]
    assert d["jobs"]["ignite"]["runs-on"] == ["ubuntu-latest"]
    # trigger + annotation, no user job runs on GitHub
    assert [s["name"] for s in d["jobs"]["ignite"]["steps"]] == [
        "Enqueue praktika trigger",
        "Report link",
    ]
    assert "praktika_ignition" in out
    # type is baked from the workflow event, not github.event_name, so the manual
    # dispatch button still enqueues a schedule run the orchestrator will match.
    assert '--arg type "schedule"' in out
    assert "github.event_name" not in out


def test_dispatch_ignition_bakes_dispatch_type():
    out = _render(_ignition_wf("Release", Workflow.Event.DISPATCH))
    assert '--arg type "dispatch"' in out


def test_dispatch_only_ignition_omits_empty_schedule():
    inputs = [
        Workflow.Config.InputConfig(
            name="dry_run",
            description="dry",
            is_required=False,
            default_value="false",
            is_boolean=True,
        )
    ]
    out = _render(_ignition_wf("Release", Workflow.Event.DISPATCH, inputs=inputs))
    d = yaml.safe_load(out)
    on = d.get("on", d.get(True))
    # no crons -> no `schedule:` trigger at all (GitHub rejects an empty one)
    assert "schedule" not in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"


def test_report_url_uses_workflow_name_and_runtime_ref():
    out = _render(_ignition_wf("Nightly SQLancer", Workflow.Event.SCHEDULE, crons=["1 1 * * *"]))
    # name is URL-encoded and baked in; branch + sha come from the runner context
    assert "name_0=Nightly%20SQLancer" in out
    assert "REF=${{ github.ref_name }}&sha=${{ github.sha }}" in out
