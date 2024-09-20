from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow, Artifact
from praktika.settings import Settings


class JobNames:
    JOB_UPLOADING_ARTIFACT = "Provide Artifact"
    JOB_REQUIRING_ARTIFACT = "Consume Artifact"


class ArtifactNames:
    GREET = "greet"


class WorkflowNames:
    PULL_REQUEST = "Example S3 Artifact"


artifacts = [
    Artifact.Config(
        name=ArtifactNames.GREET, type=Artifact.Type.S3, path="./artifact.txt"
    ),
]  # type: List[Artifact.Config]


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_UPLOADING_ARTIFACT,
            runs_on=[RunnerLabels.SMALL],
            command='echo "Hello World" > ./artifact.txt',
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_REQUIRING_ARTIFACT,
            runs_on=[RunnerLabels.SMALL],
            command=f"cat {Settings.INPUT_DIR}/artifact.txt",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
    ],
    artifacts=artifacts,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
