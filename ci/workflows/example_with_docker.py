from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Docker, Job, Secret, Workflow
from praktika.result import Result


class JobNames:
    NAME_1 = "Some Job to run in docker"


class WorkflowNames:
    NAME = "Example with Docker"


job_1 = Job.Config(
    name=JobNames.NAME_1,
    runs_on=[RunnerLabels.SMALL_FIXED],
    command="python3 ./ci/tests/example_2/some_job_script.py",
    job_requirements=Job.Requirements(
        python=True, python_requirements_txt="./ci/requirements.txt"
    ),
    digest_config=Job.CacheDigestConfig(
        # example: use glob to include files
        include_paths=["./ci/tests/example_2/some_job_script.py"],
    ),
    run_in_docker="clickhouse/praktika-test",
)

workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[job_1],
    dockers=[
        Docker.Config(
            name="clickhouse/praktika",
            path="./dockers/praktika",
            platforms=[Docker.Platforms.AMD, Docker.Platforms.ARM],
            depends_on=[],
        ),
        Docker.Config(
            name="clickhouse/praktika-test",
            path="./dockers/praktika-test",
            platforms=[Docker.Platforms.AMD, Docker.Platforms.ARM],
            depends_on=["clickhouse/praktika"],
        ),
    ],
    secrets=[
        Secret.Config(
            name="dockerhub_robot_password",
            type=Secret.Type.AWS_SSM_PARAMETER,
        ),
    ],
    enable_cache=True,
    enable_report=True,
)

# WORKFLOWS = [
#     workflow,
# ]  # type: List[Workflow.Config]


if __name__ == "__main__":
    # example: local job test inside praktika environment
    from praktika.runner import Runner

    Runner.generate_dummy_environment(workflow, job_1)
    Runner().run(workflow, job_1)

    print(Result.from_fs(job_1.name))
