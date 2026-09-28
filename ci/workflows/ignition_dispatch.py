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
    # No branches restriction: the native run fires on whatever ref is chosen in
    # the GitHub "Run workflow" UI. Set branches=[...] to restrict to specific refs.
    jobs=[
        # Mock job: reads the dispatch input and asserts it propagated end to end
        # (GH UI -> ignition trigger -> lambda -> native job).
        Job.Config(
            name="Echo Dispatch Input",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_ignition/echo_input.py",
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
