"""Praktika MainCI — runs on push to main.

Builds the praktika and praktika_controller wheels and uploads them to fixed
S3 keys so the orchestrator and runner pools can install the latest release
artifacts via stable URLs. The fixed keys mean any commit landed on main is
picked up by every instance launched (or re-launched) afterward without
redeploying ASGs/LTs.
"""
from ci.settings.settings import RunnerLabels
from praktika import Artifact, Job, Workflow


_INSTALL_BUILD = (
    "python3 -m pip install --break-system-packages build "
    "|| python3 -m pip install build"
)
_INSTALL_COVERAGE_DEPS = (
    "python3 -m pip install coverage -r ./ci/requirements.txt --break-system-packages "
    "|| python3 -m pip install coverage -r ./ci/requirements.txt"
)

coverage_html = Artifact.Config(
    name="coverage-html",
    type=Artifact.Type.S3,
    path="./ci/tmp/coverage-html.tar.gz",
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
        Job.Config(
            name="Praktika Pytests",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="PRAKTIKA_ENABLE_COVERAGE=1 python3 ./ci/scripts/run_ci_pytests.py",
            pre_hooks=[_INSTALL_COVERAGE_DEPS],
            provides=[coverage_html.name],
        ),
        Job.Config(
            name="Publish Coverage Report",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/publish_coverage_pages.py",
            requires=[coverage_html.name],
            enable_gh_auth=True,
        ),
    ],
    artifacts=[coverage_html],
    enable_report=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
