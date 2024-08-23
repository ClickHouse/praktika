from typing import List
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job A"
    JOB_B = "Job B"


class WorkflowNames:
    NAME = "Example On Push Trigger"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["'**'"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            command="echo Dzień dobry wszystkim",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="echo Доброго ранку всім",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
            requires=[JobNames.JOB_A],
        ),
    ],
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
