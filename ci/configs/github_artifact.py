from typing import List

from recurcipy import Job, Workflow, Artifact
from recurcipy.environment import Environment
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    JOB_UPLOADING_ARTIFACT = "Provide Artifact"
    JOB_REQUIRING_ARTIFACT = "Consume Artifact"


class ArtifactNames(MetaClasses.WithIter):
    GREET = "greet"


class WorkflowNames(MetaClasses.WithIter):
    PULL_REQUEST = "GitHub Artifact Example"


artifacts = [
    Artifact.define_gh_artifact(name=ArtifactNames.GREET, path="./hello_world.txt"),
]  # type: List[Artifact.Config]


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_UPLOADING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command='echo "Hello World" > ./hello_world.txt',
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
        ),
        Job.Config(
            name=JobNames.JOB_REQUIRING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command=f"cat {Environment.INPUT_DIR}/hello_world.txt",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt"),
        ),
    ],
    artifacts=artifacts,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
