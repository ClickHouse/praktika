from dataclasses import dataclass
from typing import List, Optional

from recurcipy import Job
from recurcipy.artifact import Artifact


class Workflow:
    class Event:
        PULL_REQUEST = "pull_request"
        PUSH = "push"

    @dataclass
    class Config:
        """
        branches - List of branch names or patterns, valid for push trigger only, if not provided [Settings.MAIN_BRANCH_NAME] will be used
        """

        name: str
        event: str
        jobs: List[Job.Config]
        artifacts: Optional[List[Artifact.Config]] = None
        branches: Optional[List[str]] = None

        def is_event_pull_request(self):
            return self.event == Workflow.Event.PULL_REQUEST

        def is_event_push(self):
            return self.event == Workflow.Event.PUSH

        def get_job(self, name: str) -> Optional[Job.Config]:
            for job in self.jobs:
                if job.name == name:
                    return job
            else:
                return None
