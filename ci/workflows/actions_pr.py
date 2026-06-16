"""GHActions PR CI — lint, artifact/dependency/parametrize demos."""
from praktika import Artifact, Job, Workflow
from praktika.settings import Settings

_INSTALL_DEPS = (
    "python3 -m pip install -r ./ci/requirements.txt --break-system-packages "
    "|| python3 -m pip install -r ./ci/requirements.txt"
)

WORKFLOWS = [
    Workflow.Config(
        engine="GHActions",
        name="GHActions PR CI",
        event=Workflow.Event.PULL_REQUEST,
        base_branches=["main"],
        jobs=[
            # --- real library tests ---
            Job.Config(
                name="Yaml Lint",
                command="yamllint . --config-file=.yamllint",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            # --- demo: artifact upload / download ---
            Job.Config(
                name="Provide Artifact",
                command='echo "Hello from praktika" > ./hello.txt',
                provides=["hello"],
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            Job.Config(
                name="Consume Artifact",
                command=f"cat {Settings.INPUT_DIR}/hello.txt",
                requires=["hello"],
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            # --- demo: job dependencies (fan-in) ---
            Job.Config(
                name="Job A",
                command="echo 'Job A'",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            Job.Config(
                name="Job B",
                command="echo 'Job B'",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            Job.Config(
                name="After A and B",
                command="echo 'Both A and B finished'",
                requires=["Job A", "Job B"],
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            # --- demo: parametrized jobs ---
            *Job.Config(
                name="Parametrized",
                command="python3 ./ci/tests/example_4/parametrized_job_no_artifact.py",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ).parametrize(
                Job.ParamSet(parameter={"key": "value_1"}, runs_on=["ubuntu-latest"]),
                Job.ParamSet(parameter={"key": "value_2"}, runs_on=["ubuntu-latest"]),
            ),
        ],
        artifacts=[
            Artifact.Config(name="hello", type=Artifact.Type.GH, path="./hello.txt"),
        ],
        enable_exit_code_result=True,
    )
]
