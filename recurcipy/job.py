from dataclasses import dataclass
from typing import Optional

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
        name: str
        command: str
        job_requirements: Optional["Job.Requirements"] = None
