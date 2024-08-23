from dataclasses import dataclass
from typing import Optional, List


class Job:
    @dataclass
    class Requirements:
        python: bool = False
        python_requirements_txt: str = ""

    @dataclass
    class CacheDigestConfig:
        include_paths: Optional[List[str]] = None
        exclude_paths: Optional[List[str]] = None

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
        requires: Optional[List[str]] = None

        # What job provides
        #   May be phony or physical names
        provides: Optional[List[str]] = None

        job_requirements: Optional["Job.Requirements"] = None

        auto_dependencies: Optional[List[str]] = None

        cache_digest: Optional["Job.CacheDigestConfig"] = None
