"""Praktika MainCI — runs on push to main.

Builds the praktika and praktika_controller wheels and uploads them to fixed
S3 keys so the orchestrator and runner pools can install the latest release
artifacts via stable URLs. The fixed keys mean any commit landed on main is
picked up by every instance launched (or re-launched) afterward without
redeploying ASGs/LTs.
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
        Job.Config(
            name="Publish controller wheel",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="bash ./ci/scripts/publish_controller_wheel.sh",
            pre_hooks=[_INSTALL_BUILD],
        ),
    ],
    enable_report=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
