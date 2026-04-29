from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Artifact, Job, Secret, Workflow


class JobNames:
    JOB_PARAMETRIZED = "Parametrized_job"
    JOB_1 = "Just Job"
    JOB_2 = "Just Job 2"
    JOB = "Just Another Job"


greet_artifact = Artifact.Config(
    name="greet", type=Artifact.Type.S3, path="./artifact.txt"
)


class WorkflowNames:
    PULL_REQUEST = "Example All IN"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_1,
            runs_on=[RunnerLabels.SMALL_FIXED],
            # run first
            requires=[],
            provides=[],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_2,
            runs_on=[RunnerLabels.SMALL_FIXED],
            # run first
            requires=[],
            provides=[greet_artifact.name],
            command=f"echo Hello > {greet_artifact.path}; python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            # enable ci cache for this job
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
        ),
        # example: parametrize job syntax
        *Job.Config(
            name=JobNames.JOB_PARAMETRIZED,
            # runs after:
            requires=[JobNames.JOB_1, greet_artifact.name],
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            # digest_config=Job.CacheDigestConfig(
            #     include_paths=["./ci/tests/example_3/script_for_parametrized_job.py"],
            # ),
        ).parametrize(
            Job.ParamSet(
                parameter={
                    "key_1": [1, 2, "ABC"],
                    "key_2": None,
                },
            ),
            Job.ParamSet(
                parameter={"key_1": [2, 3]},
            ),
        ),
    ],
    secrets=[
        # example: provide required secrets and store CI DB secrets names in user settings:
        #   CI_DB_URL="CI_DB_URL", CI_DB_PASSWORD="CI_DB_PASSWORD"
        Secret.Config(
            name="CI_DB_URL",
            type=Secret.Type.GH_SECRET,
        ),
        Secret.Config(
            name="CI_DB_PASSWORD",
            type=Secret.Type.GH_SECRET,
        ),
    ],
    artifacts=[greet_artifact],
    # enable HTML Report
    enable_report=True,
    # enable CI Cache
    enable_cache=True,
    # enable cumulative merge than ready GH status
    enable_merge_ready_status=True,
    # report results to ci db
    enable_cidb=True,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
