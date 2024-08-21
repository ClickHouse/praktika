from recurcipy import Job
from recurcipy.settings import Settings

_workflow_config_job = Job.Config(
    name=Settings.CACHE_CONFIG_JOB_NAME,
    runs_on=Settings.CACHE_CONFIG_RUNS_ON,
    command=f"{Settings.PYTHON_INTERPRETER} -m recurcipy.runner --config",
    job_requirements=Job.Requirements(python=True),
)
