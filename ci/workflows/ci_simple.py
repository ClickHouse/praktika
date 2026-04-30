"""
Praktika CI — simple workflow demo.

Covers: S3 artifact upload/download, job dependencies, parametrized jobs.
No docker, no cache digests, no merge-ready status.
"""
from praktika import Artifact, Job, Workflow
from ci.settings.settings import RunnerLabels
from praktika.settings import Settings

_REQ = Job.Requirements(python=True, python_requirements_txt="./ci/requirements.txt")

artifact = Artifact.Config(name="greet", type=Artifact.Type.S3, path="./artifact.txt")

workflow = Workflow.Config(
    name="Praktika CI",
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name="Provide Artifact",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command='echo "Hello from praktika" > ./artifact.txt',
            provides=[artifact.name],
            job_requirements=_REQ,
        ),
        Job.Config(
            name="Consume Artifact",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command=f"cat {Settings.INPUT_DIR}/artifact.txt",
            requires=[artifact.name],
            job_requirements=_REQ,
        ),
        Job.Config(
            name="Independent Job",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=_REQ,
        ),
        *Job.Config(
            name="Parametrized",
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            job_requirements=_REQ,
        ).parametrize(
            Job.ParamSet(parameter={"key_1": [1, 2, "ABC"]}),
            Job.ParamSet(parameter={"key_1": [2, 3]}),
        ),
    ],
    artifacts=[artifact],
    enable_report=True,
)

WORKFLOWS = [workflow]
