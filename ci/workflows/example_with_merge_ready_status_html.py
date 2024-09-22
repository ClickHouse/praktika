from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow
from praktika.secret import Secret


class JobNames:
    JOB_A = "Some Job 1 that should block merge on failure"
    JOB_B = "Some Job 2 that should not block merge on failure"


class WorkflowNames:
    NAME = "Example Merge ready Status, Report"


workflow_pr = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.JOB_A,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
        ),
        Job.Config(
            name=JobNames.JOB_B,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script_2.py",
            requires=[JobNames.JOB_A],
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            timeout=5,
            # example: This job won't set "Ready For Merge" status on failure
            allow_merge_on_failure=True,
        ),
    ],
    # example: This property enables "Ready For Merge" status for this workflow
    enable_merge_ready_status=True,
    # example: This property enables Report for this workflow
    enable_report=True,
)

WORKFLOWS = [
    workflow_pr,
]  # type: List[Workflow.Config]
