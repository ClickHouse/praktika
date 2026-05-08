"""GHActions Push CI — unit tests and lint on push to main."""
from praktika import Job, Workflow

_INSTALL_DEPS = (
    "python3 -m pip install -r ./ci/requirements.txt --break-system-packages "
    "|| python3 -m pip install -r ./ci/requirements.txt"
)

WORKFLOWS = [
    Workflow.Config(
        engine="GHActions",
        name="GHActions Push CI",
        event=Workflow.Event.PUSH,
        branches=["main"],
        jobs=[
            Job.Config(
                name="Unit Tests",
                command="python -m unittest discover -s ./ci/tests -p 'test_*.py'",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
            Job.Config(
                name="Yaml Lint",
                command="yamllint . --config-file=.yamllint",
                pre_hooks=[_INSTALL_DEPS],
                runs_on=["ubuntu-latest"],
            ),
        ],
        enable_exit_code_result=True,
    )
]
