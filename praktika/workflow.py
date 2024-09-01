from dataclasses import dataclass, field
from typing import List, Optional

from praktika import Job, Artifact
from praktika.docker import Docker
from praktika.secret import Secret


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
        artifacts: List[Artifact.Config] = field(default_factory=list)
        dockers: List[Docker.Config] = field(default_factory=list)
        secrets: List[Secret.Config] = field(default_factory=list)
        enable_cache: bool = False
        enable_html: bool = False

        def is_event_pull_request(self):
            return self.event == Workflow.Event.PULL_REQUEST

        def is_event_push(self):
            return self.event == Workflow.Event.PUSH

        def get_job(self, name: str) -> Optional[Job.Config]:
            # from praktika.native_configs import _docker_build_job, _workflow_config_job
            #
            # for job in (_docker_build_job, _workflow_config_job):
            #     if job.name == name:
            #         print(f"Native praktika job requested [{name}]")
            #         return job
            for job in self.jobs:
                if job.name == name:
                    return job
            else:
                return None

        def get_secret(self, name) -> Optional[Secret.Config]:
            for secret in self.secrets:
                if secret.name == name:
                    return secret
            return None
