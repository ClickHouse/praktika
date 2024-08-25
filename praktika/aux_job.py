from praktika import Job
from praktika.settings import Settings

assert Settings.CI_CONFIG_RUNS_ON, "Setting CI_CONFIG_RUNS_ON must be configured"

# TODO: think about dependencies requirements_with_gh_auth.txt.
#   it's not there outside of this repo
_workflow_config_job = Job.Config(
    name=Settings.CI_CONFIG_JOB_NAME,
    runs_on=Settings.CI_CONFIG_RUNS_ON,
    job_requirements=Job.Requirements(
        python=True,
        python_requirements_txt="requirements_with_gh_auth.txt",
        gh_app_auth=False,
    ),
    command="",
)
