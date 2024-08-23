from praktika import Job
from praktika.settings import Settings

assert Settings.CI_CONFIG_RUNS_ON, "Setting CI_CONFIG_RUNS_ON must be configured"

_workflow_config_job = Job.Config(
    name=Settings.CI_CONFIG_JOB_NAME,
    runs_on=Settings.CI_CONFIG_RUNS_ON,
    command="",
    job_requirements=Job.Requirements(python=True),
)
