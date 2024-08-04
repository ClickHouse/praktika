
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
    MASTER = "Main"


workflow_pr = Workflow.Config(
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

workflow_master = Workflow.Config(
    name=WorkflowNames.MASTER,
    event=Workflow.Event.PUSH,
    jobs=[
        Job.Config(
            name=JobNames.JOB_HELLO_WORLD,
            command="echo Hello Hello World",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ]
)

"""
recurCIPY entry-point for generating yaml configs
each item ends up in workflow yaml file
"""
WORKFLOWS = [
    workflow_pr,
    workflow_master,
]  # type: List[Workflow.Config]
