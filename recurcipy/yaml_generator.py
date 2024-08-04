from pathlib import Path
from typing import Optional, List

from recurcipy import Workflow, Job, ContextManager
from recurcipy.mangle import _get_workflows
from recurcipy.settings import Settings
from recurcipy.utils import Utils
from recurcipy.yaml_templates import Templates


class YamlGenerator:
    def __init__(self, workflows: Optional[List[Workflow.Config]] = None):
        with ContextManager.cd():
            Path(Settings.WORKFLOW_PATH_PREFIX).mkdir(parents=True, exist_ok=True)

        if not workflows:
            self.py_workflows = _get_workflows()
        else:
            self.py_workflows = workflows
        assert self.py_workflows

    @classmethod
    def _get_workflow_file_name(cls, workflow_name):
        return f"{Settings.WORKFLOW_PATH_PREFIX}/{Utils.normalize_string(workflow_name)}.yaml"

    def hello_world(self):
        with ContextManager.cd():
            Path("./ci").mkdir(parents=True, exist_ok=True)
            with open("./ci/hello_world.py", "w") as f:
                f.write(HELLO_WORLD_EXAMPLE_PY)
        self.generate()

    def generate(self):
        aux_configs_all = []
        for workflow_config in self.py_workflows:
            print(f"Generate workflow [{workflow_config.name}]")
            if workflow_config.is_event_pull_request():
                yaml_workflow, aux_configs = PullRequestYamlGen(workflow_config).generate()
                aux_configs_all += aux_configs
            else:
                assert False

            with ContextManager.cd():
                with open(self._get_workflow_file_name(workflow_config.name), "w") as f:
                    f.write(yaml_workflow)

        for aux_config in aux_configs_all:  # type: Job.Requirements
            print(f"Generating aux workflow [{aux_config}]")
            yaml_workflow = AuxYamlGen(aux_config).generate()
            with ContextManager.cd():
                with open(aux_config.get_aux_workflow_name(), "w") as f:
                    f.write(yaml_workflow.strip() + "\n")


class PullRequestYamlGen:
    def __init__(self, workflow_config: Workflow.Config):
        self.workflow_config = workflow_config

    def generate(self):
        required_aux_workflow_configs = []
        template_1 = Templates.TEMPLATE_PULL_REQUEST_0.strip().format(NAME=self.workflow_config.name,
                                                                      JOBS="{}" * len(self.workflow_config.jobs),
                                                                      BASE_BRANCH=Settings.MAIN_BRANCH_NAME)
        template_1_args = []
        for i, job in enumerate(self.workflow_config.jobs):
            aux_workflow_name = job.job_requirements.get_aux_workflow_name()
            aux_workflow_input = job.job_requirements.get_aux_workflow_input()
            required_aux_workflow_configs.append(job.job_requirements)
            template_1_args.append(
                Templates.TEMPLATE_JOB.format(
                    JOB_NAME_YAML=f"j{i}",
                    JOB_NAME=job.name,
                    AUX_WORKFLOW=aux_workflow_name,
                    AUX_INPUT=aux_workflow_input
                )
            )
        yaml_workflow = template_1.format(*template_1_args)

        return yaml_workflow, required_aux_workflow_configs


class AuxYamlGen:
    def __init__(self, addon: Job.Requirements):
        self.addon = addon

    def generate(self):
        addon_inputs = []
        addon_steps = []

        if self.addon.python_requirements:
            addon_inputs.append(Templates.ADDON_PY_INPUT)
            addon_steps.append(Templates.ADDON_PY_STEPS)

        template_1 = Templates.CALLABLE_JOB_TEMPLATE_0.strip().format(
            ADDONS_INPUTS="{}" * len(addon_inputs),
            ADDONS_STEPS="{}" * len(addon_steps),
        )

        result = template_1.format(*addon_inputs, *addon_steps)

        return result


if __name__ == '__main__':
    G = YamlGenerator()
    WFS = [
        Workflow.Config(
            name="PR",
            event=Workflow.Event.PULL_REQUEST,
            jobs=[
                Job.Config(
                    name="Hello World",
                    job_requirements=Job.Requirements(python_requirements="./requirement.txt")
                )
            ]
        )
    ]
    G.generate()

HELLO_WORLD_EXAMPLE_PY = '''
from typing import List

from recurcipy import Job, Workflow
from recurcipy.utils import MetaClasses


class JobNames(MetaClasses.WithIter):
    """
    Inclusive List of Job names
    """
    JOB_HELLO_WORLD = "Hello World"
    JOB_LINT = "Yaml Lint"


class WorkflowNames(MetaClasses.WithIter):
    """
    Workflow names
    """
    PULL_REQUEST = "Pull Request"


w1 = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_HELLO_WORLD,
            command="echo Hello World",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ]
)

# this is only variable recurcipy cares about
WORKFLOWS = [
    w1,
]  # type: List[Workflow.Config]
'''
