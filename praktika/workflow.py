from dataclasses import dataclass, field
from typing import List, Optional

from praktika import Job, Artifact


class Workflow:
    class Event:
        PULL_REQUEST = "pull_request"
        PUSH = "push"

    @dataclass
    class Config:
        """
        branches - List of branch names or patterns, for push trigger only
        base_branches - List of base branches (target branch), for pull_request trigger only
        """

        name: str
        event: str
        jobs: List[Job.Config]
        branches: List[str] = field(default_factory=list)
        base_branches: List[str] = field(default_factory=list)
        artifacts: Optional[List[Artifact.Config]] = None
        enable_cache: bool = False
        enable_html: bool = False

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
