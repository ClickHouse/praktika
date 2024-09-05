from dataclasses import dataclass, field
from typing import Optional, List


class Job:
    @dataclass
    class Requirements:
        python: bool = False
        python_requirements_txt: str = ""
        gh_app_auth: bool = False

    @dataclass
    class CacheDigestConfig:
        include_paths: List[str] = field(default_factory=list)
        exclude_paths: List[str] = field(default_factory=list)

    @dataclass
    class Config:
        # Job Name
        name: str

        # Machine's label to run job on. For instance [ubuntu-latest] for free gh runner
        runs_on: List[str]

        # Job Run Command
        command: str

        # What job requires
        #   May be phony or physical names
        requires: List[str] = field(default_factory=list)

        # What job provides
        #   May be phony or physical names
        provides: Optional[List[str]] = None

        job_requirements: Optional["Job.Requirements"] = None

        timeout: int = 1 * 3600

        digest_config: Optional["Job.CacheDigestConfig"] = None

        run_in_docker: str = ""

        run_if_not_cancelled: bool = False

        allow_merge_on_failure: bool = False
