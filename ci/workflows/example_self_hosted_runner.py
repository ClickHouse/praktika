from typing import List

from ci.settings.user_defined_settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job A"
    JOB_B = "Job B"


class WorkflowNames:
    PULL_REQUEST = "Example Self-hosted Runners"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            command="echo Dzień dobry wszystkim",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=[RunnerLabels.SMALL],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="echo Доброго ранку всім",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=[RunnerLabels.SMALL],
        ),
    ],
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
