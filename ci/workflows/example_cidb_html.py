from typing import List

from praktika import Job, Workflow, Secret
from ci.settings.my_settings import RunnerLabels


class JobNames:
    NAME_1 = "Job"


class WorkflowNames:
    NAME = "Example CI DB, HTML"


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
                python_requirements_txt="./requirements.txt"
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
    # example: enable ci db
    enable_cidb=True,
)

WORKFLOWS = [
    workflow,
]  # type: List[Workflow.Config]
