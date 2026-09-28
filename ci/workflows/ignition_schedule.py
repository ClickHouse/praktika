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
    # The native run matches head_ref against branches (like push), so the branch
    # the cron fires on must be listed here.
    branches=["main"],
    cron_schedules=["30 2 * * *"],
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
