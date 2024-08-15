from typing import List
from recurcipy import Job, Workflow
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    """
    Inclusive List of Job names
    """
    JOB_A = "Job A starting at the beginning"
    JOB_B = "Job B starting at the beginning"
    JOB_C = "Job C starting after Job A and B is done"
    JOB_D = "Job D starting after Job C is done"


class WorkflowNames(MetaClasses.WithIter):
    """
    Workflow names
    """
    PULL_REQUEST = "Job Dependencies Example"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            command="echo Dzień dobry wszystkim",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="echo Доброго ранку всім",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_C,
            command="echo Добро јутро свима",
            requires=[JobNames.JOB_A, JobNames.JOB_B],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_D,
            command="echo Jó reggelt mindenkinek",
            requires=[JobNames.JOB_C],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ],
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
