from dataclasses import dataclass
from typing import Optional, List

from recurcipy.settings import Settings


class Job:
    @dataclass
    class Requirements:
        python_requirements: str = ""

        def get_aux_workflow_name(self):
            suffix = ""
            if self.python_requirements:
                suffix += "_py"
            return f"{Settings.WORKFLOW_PATH_PREFIX}/aux_job{suffix}.yaml"

        def get_aux_workflow_input(self):
            res = ""
            if self.python_requirements:
                res += f"      requirements_txt: {self.python_requirements}"
            return res

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

        def set_dependencies(self, dependencies):
            self.auto_dependencies = dependencies
