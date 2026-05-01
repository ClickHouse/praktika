"""Praktika MainCI — runs on push to main.

Builds the praktika wheel and uploads it to a fixed S3 key so the
orchestrator and runner pools can install the latest praktika via a
stable URL with `pip install --force-reinstall`. The fixed key means
any commit landed on main is picked up by every instance launched (or
re-launched) afterward without redeploying ASGs/LTs.
"""
from ci.settings.settings import RunnerLabels, S3_ARTIFACT_PATH
from praktika import Job, Workflow


_INSTALL_BUILD = (
    "python3 -m pip install --break-system-packages build "
    "|| python3 -m pip install build"
)

_WHEEL_S3_URL = f"s3://{S3_ARTIFACT_PATH}/packages/praktika-0.1-py3-none-any.whl"

# Build and overwrite the fixed key. Consumers install with
# `pip install --force-reinstall <https url for the same key>`, so the
# embedded `0.1` version is irrelevant — what matters is that the URL
# always serves the latest bytes.
_BUILD_AND_UPLOAD_WHEEL = (
    "python3 -m build --wheel --outdir dist/ && "
    f"aws s3 cp dist/praktika-0.1-py3-none-any.whl {_WHEEL_S3_URL}"
)


workflow = Workflow.Config(
    name="Praktika MainCI",
    event=Workflow.Event.PUSH,
    branches=["main"],
    jobs=[
        Job.Config(
            name="Publish wheel",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command=_BUILD_AND_UPLOAD_WHEEL,
            pre_hooks=[_INSTALL_BUILD],
        ),
    ],
    enable_report=True,
)

WORKFLOWS = [workflow]
