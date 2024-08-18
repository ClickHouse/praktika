from typing import List
from recurcipy import Job, Workflow
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    JOB_A = "Job A"
    JOB_B = "Job B"


class WorkflowNames(MetaClasses.WithIter):
    NAME = "Example On Push Trigger"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["'**'"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            command="echo Dzień dobry wszystkim",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="echo Доброго ранку всім",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
            requires=[JobNames.JOB_A],
        ),
    ],
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
