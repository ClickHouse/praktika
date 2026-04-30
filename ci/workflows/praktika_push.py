from ci.settings.settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job 1"
    JOB_B = "Job 2"


class WorkflowNames:
    NAME = "Example Push trigger, Report"


_INSTALL_DEPS = (
    "sudo apt-get update && sudo apt install -y python3-pip && "
    "python3 -m pip install --upgrade pip --break-system-packages && "
    "pip3 install -r ./ci/requirements.txt --break-system-packages"
)


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["**"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            pre_hooks=[_INSTALL_DEPS],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script_2.py",
            requires=[JobNames.JOB_A],
            pre_hooks=[_INSTALL_DEPS],
        ),
    ],
    enable_report=True,
)

# WORKFLOWS = [
#     workflow,
# ]  # type: List[Workflow.Config]
