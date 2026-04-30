"""GHActions Push CI — unit tests and lint on push to main."""
from praktika import Job, Workflow

_INSTALL_DEPS = (
    "sudo apt-get update && sudo apt install -y python3-pip && "
    "python3 -m pip install --upgrade pip --break-system-packages && "
    "pip3 install -r ./ci/requirements.txt --break-system-packages"
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
    )
]
