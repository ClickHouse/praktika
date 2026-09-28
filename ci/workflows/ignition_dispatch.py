"""
GH_IGNITION demo — manually dispatched workflow.

The generated thin workflow exposes a "Run workflow" button in the GitHub Actions
UI; the ignition job signs the trigger (with dispatch inputs) and POSTs it to the
gh-trigger lambda, which enqueues the run on the native Praktika engine.
"""
from ci.settings.settings import RunnerLabels
from praktika import Job, Workflow

workflow = Workflow.Config(
    name="Ignition Manual Style",
    event=Workflow.Event.DISPATCH,
    engine=Workflow.Engine.GH_IGNITION,
    # The native run matches head_ref against branches (like push), so the branch
    # the workflow is dispatched from must be listed here.
    branches=["main"],
    jobs=[
        Job.Config(
            name="Style Check",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="ruff check .",
        ),
    ],
    inputs=[
        Workflow.Config.InputConfig(
            name="paths",
            is_required=False,
            default_value=".",
            description="Paths to run ruff against",
        ),
    ],
    enable_report=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
