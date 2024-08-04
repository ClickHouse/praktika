from dataclasses import dataclass
from typing import List, Optional

from recurcipy import Job


class Workflow:
    class Event:
        PULL_REQUEST = "pull_request"
        PUSH = "push"

    @dataclass
    class Config:
        name: str
        event: str
        jobs: List[Job.Config]

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
