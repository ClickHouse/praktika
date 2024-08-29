from typing import List

from praktika import Job, Workflow
from ci.settings.my_settings import RunnerLabels
from praktika.docker import Docker
from praktika.secret import Secret


class JobNames:
    NAME_1 = "Some Job to run in docker"


class WorkflowNames:
    NAME = "Example with Docker"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.NAME_1,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python_requirements_txt="./requirements.txt"
            ),
            digest_config=Job.CacheDigestConfig(
                # example: use glob to include files
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
            run_in_docker="clickhouse/praktika-test",
        ),
    ],
    dockers=[
        Docker.Config(
            name="clickhouse/praktika",
            path="./dockers/praktika",
            arm64=True,
            amd64=True,
            depend_on=[],
        ),
        Docker.Config(
            name="clickhouse/praktika-test",
            path="./dockers/praktika-test",
            arm64=True,
            amd64=True,
            depend_on=["clickhouse/praktika"],
        ),
    ],
    secrets=[
        Secret.Config(
            name="dockerhub_robot_password",
            type=Secret.Type.AWS_SSM_VAR,
            encrypted=True,
        )
    ],
    enable_cache=True,
    enable_html=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
