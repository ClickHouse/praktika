from typing import List

from praktika import Artifact, Job, Workflow
from praktika.settings import Settings


class JobNames:
    JOB_UPLOADING_ARTIFACT = "Provide Artifact"
    JOB_REQUIRING_ARTIFACT = "Consume Artifact"
    JOB_A = "Job A starting at the beginning"
    JOB_B = "Job B starting at the beginning"
    JOB_C = "Job C starting after Job A and B is done"
    JOB_D = "Job D starting after Job C is done"


class ArtifactNames:
    GREET = "greet"


class WorkflowNames:
    PULL_REQUEST = "Example Artifact And Dependencies"


workflow_pr = Workflow.Config(
    engine="GHActions",
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_UPLOADING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command='echo "Hello World" > ./hello_world.txt',
            # example: set list of artifacts that job provides
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_REQUIRING_ARTIFACT,
            runs_on=["ubuntu-latest"],
            command=f"cat {Settings.INPUT_DIR}/hello_world.txt",
            # example: set list of artifacts that job requires, job will follow all jobs that provide required artifact
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_A,
            command="echo Dzień dobry wszystkim",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="echo Доброго ранку всім",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_C,
            command="echo Добро јутро свима",
            # example: Job names caould be also set in :requires:
            requires=[JobNames.JOB_A, JobNames.JOB_B],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
        ),
        Job.Config(
            name=JobNames.JOB_D,
            command="echo Jó reggelt mindenkinek",
            requires=[JobNames.JOB_C],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            runs_on=["ubuntu-latest"],
        ),
    ],
    # example: all artifacts must be defined in the workflow' list of artifacts
    artifacts=[
        Artifact.Config(name=ArtifactNames.GREET, type=Artifact.Type.GH, path="./hello_world.txt")
    ],
)

# WORKFLOWS = [
#     workflow_pr,
# ]  # type: List[Workflow.Config]
