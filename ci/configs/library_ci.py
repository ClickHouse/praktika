from typing import List

from recurcipy import Job, Workflow, Artifact
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    JOB_UNIT_TESTS = "Unit Tests"
    JOB_LINT = "Yaml Lint"


class WorkflowNames(MetaClasses.WithIter):
    """
    Workflow names
    """

    PULL_REQUEST = "Library PR CI"
    MAIN = "Library Main CI"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_UNIT_TESTS,
            command="python -m unittest discover -s ./ci/tests -p 'test_*.py'",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
        ),
    ],
)

workflow_main = Workflow.Config(
    name=WorkflowNames.MAIN,
    event=Workflow.Event.PUSH,
    jobs=[
        Job.Config(
            name=JobNames.JOB_UNIT_TESTS,
            command="python -m unittest discover -s ./ci/tests -p 'test_*.py'",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
            runs_on=["ubuntu-latest"],
        ),
    ],
)


WORKFLOWS = [workflow_pr, workflow_main]  # type: List[Workflow.Config]
