from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow


class JobNames:
    JOB_A = "Job 1"
    JOB_B = "Job 2"


class WorkflowNames:
    NAME = "Example HTML report"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python_requirements_txt="./requirements_with_gh_auth.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script_2.py",
            requires=[JobNames.JOB_A],
            job_requirements=Job.Requirements(
                python_requirements_txt="./requirements_with_gh_auth.txt"
            ),
        ),
    ],
    enable_html=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
