from dataclasses import dataclass
from typing import Optional, List

from recurcipy.settings import Settings


class Job:
    @dataclass
    class Requirements:
        python_requirements: str = ""

    @dataclass
    class Config:
        # Job Name
        name: str

        # Job Run Command
        command: str

        # What job requires
        #   May be phony or physical names
        requires: Optional[List[str]] = None

        # What job provides
        #   May be phony or physical names
        provides: Optional[List[str]] = None

        job_requirements: Optional["Job.Requirements"] = None

        auto_dependencies: List[str] = None
