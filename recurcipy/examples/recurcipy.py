from typing import List

from recurcipy import Job, Workflow
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    """
    Inclusive List of Job names
    """
    JOB_HELLO_WORLD = "Hello_World"
    JOB_HELLO_RECURCIPY = "Hello_RecurCIPY"
    JOB_UNIT_TESTS = "Unit_Tests"
    JOB_LINT = "Yaml_Lint"


class ArtifactNames(MetaClasses.WithIter):
    """
    Predefined names of artifacts
    """
    GREET = "greet"


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
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_HELLO_RECURCIPY,
            command="echo Hello World",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_UNIT_TESTS,
            command="python -m unittest discover -s ./ci/tests -p 'test_*.py'",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            requires=[JobNames.JOB_UNIT_TESTS],
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
