"""Praktika MainCI — runs on push to main.

Builds the praktika wheel and uploads it to a fixed S3 key so the
orchestrator and runner pools can install the latest praktika via a
stable URL with `pip install --force-reinstall`. The fixed key means
any commit landed on main is picked up by every instance launched (or
re-launched) afterward without redeploying ASGs/LTs.
"""
from ci.settings.settings import RunnerLabels
from praktika import Job, Workflow


_INSTALL_BUILD = (
    "python3 -m pip install --break-system-packages build "
    "|| python3 -m pip install build"
)


workflow = Workflow.Config(
    name="Praktika MainCI",
    event=Workflow.Event.PUSH,
    branches=["main"],
    jobs=[
        Job.Config(
            name="Publish wheel",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="bash ./ci/scripts/publish_wheel.sh",
            pre_hooks=[_INSTALL_BUILD],
        ),
    ],
    enable_report=True,
)

WORKFLOWS = [workflow]
