from typing import List

from praktika import Job, Workflow, Artifact
from praktika.settings import Settings
from ci.settings.my_settings import RunnerLabels


class JobNames:
    JOB_A = "Job A"
    JOB_B = "Job B"


class ArtifactNames:
    GREET = "greet"


class WorkflowNames:
    NAME = "Example CI with Cache"


artifacts = [
    Artifact.Config(
        name=ArtifactNames.GREET,
        type=Artifact.Type.S3,
        path=f"{Settings.OUTPUT_DIR}/hello_world.txt",
    ),
]  # type: List[Artifact.Config]


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python -m unittest ./ci/tests/example_1/test_example_produce_artifact.py",
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            digest_config=Job.CacheDigestConfig(
                # example: use glob to include files
                include_paths=["./ci/tests/example_1/test_example_produce*py"],
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python -m unittest ./ci/tests/example_1/test_example_consume_artifact.py",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            digest_config=Job.CacheDigestConfig(
                # example: use dir to include files recursively
                include_paths=["./ci/tests/example_1"],
                # example: use glob and dir to exclude files from digest
                exclude_paths=[
                    "./ci/tests/example_1/test_example_produce*",
                ],
            ),
        ),
    ],
    artifacts=artifacts,
    enable_cache=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
