from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Hello User Name"


class WorkflowNames:
    NAME = "Example Dispatch"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.DISPATCH,
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_5/some_code.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
    ],
    inputs=[
        Workflow.Config.InputConfig(
            name="user_name",
            is_required=True,
            default_value="",
            description="User Name",
        ),
        Workflow.Config.InputConfig(
            name="user_age",
            is_required=False,
            default_value="0",
            description="User Age",
        ),
    ],
)

# WORKFLOWS = [
#     workflow,
# ]  # type: List[Workflow.Config]
