from typing import List

from praktika import Job, Workflow, Artifact
from praktika.settings import Settings


class JobNames:
    JOB_UPLOADING_ARTIFACT = "Provide Artifact"
    JOB_REQUIRING_ARTIFACT = "Consume Artifact"


class ArtifactNames:
    GREET = "greet"


class WorkflowNames:
    PULL_REQUEST = "Example GH Artifact"


artifacts = [
    Artifact.define_gh_artifact(name=ArtifactNames.GREET, path="./hello_world.txt"),
]  # type: List[Artifact.Config]


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_UPLOADING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command='echo "Hello World" > ./hello_world.txt',
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_REQUIRING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command=f"cat {Settings.INPUT_DIR}/hello_world.txt",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
        ),
    ],
    artifacts=artifacts,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
