"""
GH_IGNITION demo — scheduled (cron) workflow.

A thin GitHub Actions workflow is generated whose only job posts the Praktika
report link; GitHub fires the run on cron, the gh-trigger lambda enqueues it, and
the real job (ruff style check) runs on the native Praktika engine.
"""
from ci.settings.settings import RunnerLabels
from praktika import Job, Workflow

workflow = Workflow.Config(
    name="Ignition Nightly Style",
    event=Workflow.Event.SCHEDULE,
    engine=Workflow.Engine.GH_IGNITION,
    # Note: branches is ignored for schedule/dispatch workflows — cron only ever
    # fires on the default branch, and a manual "Run workflow" dispatch runs on
    # whatever ref is chosen (so it can be dispatched from a feature branch).
    # Hourly, for quick end-to-end testing of the ignition path.
    cron_schedules=["0 * * * *"],
    jobs=[
        Job.Config(
            name="Style Check",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="ruff check .",
        ),
    ],
    enable_report=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
