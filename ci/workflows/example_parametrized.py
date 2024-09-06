from typing import List

from praktika import Job, Workflow


class JobNames:
    JOB_PARAMETRIZED_1 = "JOB_PARAMETRIZED_1"
    JOB_PARAMETRIZED_2 = "JOB_PARAMETRIZED_2"


class WorkflowNames:
    PULL_REQUEST = "Example Parametrized Jobs"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        # example: parametrize job syntax
        *Job.Config(
            name=JobNames.JOB_PARAMETRIZED_2,
            runs_on=["ubuntu-latest"],
            command="python3 ./ci/tests/example_3/script_for_parametrized_job.py",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
        ).parametrize(
            # example: parametrize over .parameter value:
            # parameter value should be json serializable,
            #  it will be available in the job script via Environment.PARAM[.field_name]
            parameter=[
                {"name": [1, 2, "ABC"], "name_2": 123},  # parameter 1
                {"name": [2, 3]},  # parameter 2
                {"name": "Hi, It's praktika"},  # parameter 3
                123,  # parameter 4
                "I'm a string",  # parameter 5
            ],
            # example: parametrize over runner type .runs_on
            runs_on=[
                ["ubuntu-latest"],
                ["ubuntu-latest"],
                ["ubuntu-latest"],
                ["ubuntu-latest"],
                ["ubuntu-latest"],
            ],
            # example: you can set different timeouts for parametrized jobs:
            timeout=[
                10,
                15,
                20,
                25,
                4,
            ],
            # NOTE: if parametrization over both .runs_on and .parameter their lists must be of the same size as well as .timeout
        )
    ],
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
