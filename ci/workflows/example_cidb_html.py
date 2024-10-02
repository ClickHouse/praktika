from typing import List

from ci.settings.my_settings import RunnerLabels
from praktika import Job, Secret, Workflow


class JobNames:
    NAME_1 = "Job"


class WorkflowNames:
    NAME = "Example CI DB, Report"


workflow = Workflow.Config(
    name=WorkflowNames.NAME,
    event=Workflow.Event.PULL_REQUEST,
    base_branches=["main"],
    jobs=[
        Job.Config(
            name=JobNames.NAME_1,
            runs_on=[RunnerLabels.SMALL_FIXED],
            command="python3 ./ci/tests/example_2/some_job_script.py",
            job_requirements=Job.Requirements(
                python=True, python_requirements_txt="./ci/requirements.txt"
            ),
            digest_config=Job.CacheDigestConfig(
                include_paths=["./ci/tests/example_2/some_job_script.py"],
            ),
        ),
    ],
    secrets=[
        # example: provide required secrets and store CI DB secrets names in user settings:
        #   CI_DB_URL="CI_DB_URL", CI_DB_PASSWORD="CI_DB_PASSWORD"
        Secret.Config(
            name="CI_DB_URL",
            type=Secret.Type.GH_SECRET,
        ),
        Secret.Config(
            name="CI_DB_PASSWORD",
            type=Secret.Type.GH_SECRET,
        ),
    ],
    enable_report=True,
    # example: enable ci db
    enable_cidb=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
