"""
Praktika CI Advanced — full-featured workflow demo.

Covers all features: S3 artifacts, docker, cache digests, parametrized jobs,
CI DB reporting, merge-ready status, GH summary comments, secrets.
"""
from praktika import Artifact, Docker, Job, Secret, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings

_REQ = Job.Requirements(python_requirements_txt="./ci/requirements.txt")

artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="Praktika CI Advanced",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        # S3 artifact with cache digest
        Job.Config(
            name="Build",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command='echo "Hello from praktika" > ./artifact.txt && python3 ./ci/tests/example_2/some_job_script.py',
            provides=[artifact.name],
            job_requirements=_REQ,
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
        ),
        # Consumes artifact, also cached
        Job.Config(
            name="Test",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command=f"cat {Settings.INPUT_DIR}/artifact.txt && python3 ./ci/tests/example_1/test_example_consume_artifact.py",
            requires=[artifact.name],
            job_requirements=_REQ,
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_1"],
                exclude_paths=["./ci/tests/example_1/test_example_produce*"],
            ),
        ),
        # Docker job
        Job.Config(
            name="Docker Job",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=_REQ,
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
            run_in_docker="clickhouse/praktika-test",
        ),
        # Parametrized with digests
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            job_requirements=_REQ,
            requires=[artifact.name],
        ).parametrize(
            Job.ParamSet(parameter={"key_1": [1, 2, "ABC"], "key_2": None}),
            Job.ParamSet(parameter={"key_1": [2, 3]}),
        ),
    ],
    artifacts=[artifact],
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
        Secret.Config(name="CI_DB_URL", type=Secret.Type.GH_SECRET),
        Secret.Config(name="CI_DB_PASSWORD", type=Secret.Type.GH_SECRET),
        Secret.Config(name="dockerhub_robot_password", type=Secret.Type.AWS_SSM_PARAMETER),
    ],
    enable_report=True,
    enable_cache=True,
    enable_merge_ready_status=True,
    enable_cidb=True,
    enable_gh_summary_comment=True,
)

WORKFLOWS = [workflow]
