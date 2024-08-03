from recurcipy import JobConfig
from recurcipy.ci_utils import WithIter


class Jobs(WithIter):
    """
    Inclusive List of Job names
    """
    JOB_HELLO_WORLD = "Hello World"


class Workflows(WithIter):
    """
    Workflow names
    """
    PULL_REQUEST = "PullRequest"


CI_CONFIG = [
    JobConfig(name=Jobs.JOB_HELLO_WORLD,
              run_command="echo Hello World!"
              ),
]
