from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job 1"
    JOB_B = "Job 2"


class WorkflowNames:
    NAME = "Example Push trigger, Report"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["**"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script_2.py",
            requires=[JobNames.JOB_A],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
    ],
    enable_report=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
