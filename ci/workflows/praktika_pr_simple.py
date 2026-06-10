"""
Praktika CI - simple workflow demo.

Covers: S3 artifact upload/download, job dependencies, parametrized jobs.
No docker, no cache digests, no merge-ready status.

Uses the base runner pool so jobs execute against the Praktika version baked
into the image. The workflow is also routed through the base orchestrator and
uses base-native jobs, which keeps this pipeline exercising backward
compatibility against past Praktika releases end to end.
"""
from praktika import Artifact, Job, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings

_BASE_PRAKTIKA_VERSION = "0.0.1"

artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="Praktika CI",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    orchestrator_filter="base",
    native_job_runs_on=[RunnerLabels.SMALL_ARM_BASE],
    jobs=[
        Job.Config(
            name="Version Check",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command=(
                "python3 -c \"import importlib.metadata as m; "
                f"praktika=m.version('praktika'); "
                "print('praktika=', praktika); "
                f"assert praktika == '{_BASE_PRAKTIKA_VERSION}', praktika\""
            ),
        ),
        Job.Config(
            name="Praktika Pytests",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command="python3 ./ci/scripts/run_ci_pytests.py",
        ),
        Job.Config(
            name="Unit Tests",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command="python3 -m pytest ./ci/tests/ --ignore=./ci/tests/example_1",
        ),
        Job.Config(
            name="Yaml Lint",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command="yamllint . --config-file=.yamllint",
        ),
        Job.Config(
            name="Provide Artifact",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command='echo "Hello from praktika" > ./artifact.txt',
            provides=[artifact.name],
        ),
        Job.Config(
            name="Consume Artifact",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command=f"cat {Settings.INPUT_DIR}/artifact.txt",
            requires=[artifact.name],
        ),
        Job.Config(
            name="Independent Job",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            pre_hooks=["echo 'pre-hook: executed before the job' || true"],
            post_hooks=["echo 'post-hook: executed after the job' || true"],
        ),
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_ARM_BASE],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            requires=[artifact.name],
        ).parametrize(
            Job.ParamSet(parameter={"key_1": [1, 2, "ABC"]}),
            Job.ParamSet(parameter={"key_1": [2, 3]}),
        ),
    ],
    artifacts=[artifact],
    enable_report=True,
    enable_exit_code_result=True,
)

WORKFLOWS = [workflow]
