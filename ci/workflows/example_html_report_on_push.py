from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Workflow
from praktika.secret import Secret


class JobNames:
    JOB_A = "Job 1"
    JOB_B = "Job 2"


class WorkflowNames:
    NAME = "Example Push trigger, HTML"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PUSH,
    branches=["**"],
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
    secrets=[
        Secret.Config(
            name="GH_APP_ID",
            type=Secret.Type.GH_SECRET,
        ),
        Secret.Config(
            name="GH_APP_PEM_KEY",
            type=Secret.Type.GH_SECRET,
        ),
    ],
    enable_html=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
