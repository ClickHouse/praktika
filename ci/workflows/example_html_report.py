from typing import List

from ci.settings.user_defined_settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job 1"
    JOB_B = "Job 2"


class WorkflowNames:
    NAME = "Example HTML report"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="echo Hi there!",
            job_requirements=Job.Requirements(
                python_requirements_txt="./requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="echo Hoi!",
            requires=[JobNames.JOB_A],
            job_requirements=Job.Requirements(
                python_requirements_txt="./requirements.txt"
            ),
        ),
    ],
    enable_html=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
