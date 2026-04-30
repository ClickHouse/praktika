"""GHActions Push CI — unit tests and lint on push to main."""
from praktika import Job, Workflow

_REQ = Job.Requirements(python_requirements_txt="./ci/requirements.txt")

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
                job_requirements=_REQ,
                runs_on=["ubuntu-latest"],
            ),
            Job.Config(
                name="Yaml Lint",
                command="yamllint . --config-file=.yamllint",
                job_requirements=_REQ,
                runs_on=["ubuntu-latest"],
            ),
        ],
    )
]
