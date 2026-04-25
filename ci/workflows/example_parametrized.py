from typing import List

from praktika import Job, Workflow


class JobNames:
    JOB_PARAMETRIZED_1 = "JOB_PARAMETRIZED_1"
    JOB_PARAMETRIZED_2 = "JOB_PARAMETRIZED_2"


class WorkflowNames:
    PULL_REQUEST = "Example Parametrized Jobs"


workflow_pr = Workflow.Config(
    engine="GHActions",
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        # example: parametrize job syntax
        *Job.Config(
            name=JobNames.JOB_PARAMETRIZED_2,
            runs_on=["ubuntu-latest"],
            command="python3 ./ci/tests/example_4/parametrized_job_no_artifact.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ).parametrize(
            Job.ParamSet(
                parameter={"name": [1, 2, "ABC"], "name_2": 123},
                runs_on=["ubuntu-latest"],
                timeout=10,
            ),
            Job.ParamSet(
                parameter={"name": [2, 3]},
                runs_on=["ubuntu-latest"],
                timeout=15,
            ),
            Job.ParamSet(
                parameter={"name": "praktika"},
                runs_on=["ubuntu-latest"],
                timeout=20,
            ),
            Job.ParamSet(
                parameter=123,
                runs_on=["ubuntu-latest"],
                timeout=25,
            ),
            Job.ParamSet(
                parameter="I'm a string",
                runs_on=["ubuntu-latest"],
                timeout=4,
            ),
        )
    ],
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
