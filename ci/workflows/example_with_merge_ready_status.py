from typing import List
from praktika import Job, Workflow
from ci.settings.my_settings import RunnerLabels

class JobNames:
    JOB_A = "Some Job 1 that should block merge on failure"
    JOB_B = "Some Job 2 that should not block merge on failure"


class WorkflowNames:
    NAME = "Example Merge ready Status"


workflow_pr = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=[RunnerLabels.SMALL_FIXED],
        ),
        Job.Config(
            name=JobNames.JOB_B,
            command="python3 ./ci/tests/example_2/some_job_script_2.py",
            job_requirements=Job.Requirements(
                python_requirements_txt="requirements.txt"
            ),
            runs_on=[RunnerLabels.SMALL_FIXED],
            allow_merge_on_failure=True,
        ),
    ],
    enable_merge_ready_status=True,
    enable_html=True,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
