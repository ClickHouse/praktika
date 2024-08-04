from typing import List

from recurcipy import Job, Workflow
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    """
    Inclusive List of Job names
    """
    JOB_HELLO_WORLD = "Hello World"
    JOB_LINT = "Yaml Lint"


class WorkflowNames(MetaClasses.WithIter):
    """
    Workflow names
    """
    PULL_REQUEST = "Pull Request"


w1 = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_HELLO_WORLD,
            command="echo Hello World",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ]
)

# this is only variable recurcipy cares about
WORKFLOWS = [
    w1,
]  # type: List[Workflow.Config]
