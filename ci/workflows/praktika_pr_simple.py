"""
Praktika CI — simple workflow demo.

Covers: S3 artifact upload/download, job dependencies, parametrized jobs.
No docker, no cache digests, no merge-ready status.
"""
from praktika import Artifact, Job, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings

_INSTALL_DEPS = (
    "python3 -m pip install -r ./ci/requirements.txt --break-system-packages "
    "|| python3 -m pip install -r ./ci/requirements.txt"
)

artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="Praktika CI",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name="Unit Tests",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 -m unittest discover -s ./ci/tests -p 'test_*.py'",
            pre_hooks=[_INSTALL_DEPS],
        ),
        Job.Config(
            name="Yaml Lint",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="yamllint . --config-file=.yamllint",
            pre_hooks=[_INSTALL_DEPS],
        ),
        Job.Config(
            name="Provide Artifact",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command='echo "Hello from praktika" > ./artifact.txt',
            provides=[artifact.name],
            pre_hooks=[_INSTALL_DEPS],
        ),
        Job.Config(
            name="Consume Artifact",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command=f"cat {Settings.INPUT_DIR}/artifact.txt",
            requires=[artifact.name],
            pre_hooks=[_INSTALL_DEPS],
        ),
        Job.Config(
            name="Independent Job",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            pre_hooks=[_INSTALL_DEPS],
        ),
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            requires=[artifact.name],
            pre_hooks=[_INSTALL_DEPS],
        ).parametrize(
            Job.ParamSet(parameter={"key_1": [1, 2, "ABC"]}),
            Job.ParamSet(parameter={"key_1": [2, 3]}),
        ),
    ],
    artifacts=[artifact],
    enable_report=True,
)

WORKFLOWS = [workflow]
