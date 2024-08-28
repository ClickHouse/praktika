from typing import List

from praktika import Job, Workflow, Artifact
from praktika.settings import Settings


class JobNames:
    JOB_A = "Job A"
    JOB_B = "Job B"


class ArtifactNames:
    GREET = "greet"


class WorkflowNames:
    NAME = "Example On Push,S3,self-hosted"


class RunnerLabels:
    SMALL = "maxs-small"


artifacts = [
    Artifact.Config(
        name=ArtifactNames.GREET, type=Artifact.Type.S3, path="./hello_world.txt"
    ),
]  # type: List[Artifact.Config]


workflow_pr = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["parse_gh_env", "test**"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL],
            command='echo "Hello World" > ./hello_world.txt',
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL],
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
