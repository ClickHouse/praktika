"""
Praktika CI Advanced — full-featured workflow demo.

Covers all features: S3 artifacts, docker, cache digests, parametrized jobs,
CI DB reporting, merge-ready status, GH summary comments, secrets.
"""
from praktika import Artifact, Docker, Job, Secret, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings

_HEAD_PRAKTIKA_VERSION = "0.1.5"

artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="Praktika CI Advanced",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name="Version Check",
            runs_on=[RunnerLabels.SMALL_AMD_UBUNTU],
            command=(
                "python3 -c \"import importlib.metadata as m; "
                f"praktika=m.version('praktika'); "
                "print('praktika=', praktika); "
                f"assert praktika == '{_HEAD_PRAKTIKA_VERSION}', praktika\""
            ),
        ),
        Job.Config(
            name="Praktika Pytests",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/scripts/run_ci_pytests.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=[
                    "./ci/scripts/run_ci_pytests.py",
                    "./ci/tests",
                    "./praktika",
                    "./pyproject.toml",
                ],
            ),
        ),
        # S3 artifact with cache digest
        Job.Config(
            name="Build",
            runs_on=[RunnerLabels.SMALL_ARM],
            command='echo "Hello from praktika" > ./artifact.txt && python3 ./ci/tests/example_2/some_job_script.py',
            provides=[artifact.name],
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
        ),
        # Consumes artifact, also cached
        Job.Config(
            name="Test",
            runs_on=[RunnerLabels.SMALL_ARM],
            command=f"python3 ./ci/jobs/consume_artifact.py",
            requires=[artifact.name],
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/jobs/consume_artifact.py"],
            ),
        ),
        # Docker job
        Job.Config(
            name="Docker Job",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
            run_in_docker="clickhouse/praktika-test",
        ),
        # Parametrized with digests
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_ARM],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
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
        Secret.Config(name=Settings.SECRET_CI_DB_CONNECTION, type=Secret.Type.AWS_SSM_PARAMETER),
        Secret.Config(name=Settings.SECRET_DOCKER_REGISTRY, type=Secret.Type.AWS_SSM_PARAMETER),
    ],
    enable_report=True,
    enable_cache=True,
    enable_merge_ready_status=True,
    enable_cidb=True,
    enable_gh_summary_comment=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
