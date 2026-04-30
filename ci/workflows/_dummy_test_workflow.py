"""Workflow used only by ci/tests/test_runner.py.

Disabled via Settings.DISABLED_WORKFLOWS so the live yaml/run paths skip it.
The test loads the workflow object directly and calls Runner().run() against
it.
"""
from praktika import Job, Workflow

WORKFLOWS = [
    Workflow.Config(
        name="DummyRunnerTest",
        event=Workflow.Event.PUSH,
        branches=["main"],
        jobs=[
            Job.Config(
                name="dummy",
                runs_on=["test-runner"],
                command="python3 ci/tests/_dummy_job_script.py",
            ),
        ],
        enable_report=True,
    ),
]
