from pathlib import Path
from typing import Optional, List

from recurcipy import Workflow, Job, ContextManager
from recurcipy.mangle import _get_workflows
from recurcipy.parser import WorkflowConfigParser
from recurcipy.settings import Settings
from recurcipy.utils import Utils, Shell
from recurcipy.yaml_templates import Templates


class YamlGenerator:
    def __init__(self, workflows: Optional[List[Workflow.Config]] = None):
        with ContextManager.cd():
            Path(Settings.WORKFLOW_PATH_PREFIX).mkdir(parents=True, exist_ok=True)
        self.py_workflows = []  # type: List[Workflow.Config]

    @classmethod
    def _get_workflow_file_name(cls, workflow_name):
        return f"{Settings.WORKFLOW_PATH_PREFIX}/{Utils.normalize_string(workflow_name)}.yaml"

    def hello_world(self):
        with ContextManager.cd():
            Path(Settings.CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
            with open(f"{Settings.CONFIG_DIRECTORY}/hello_world.py", "w") as f:
                f.write(HELLO_WORLD_EXAMPLE_PY)
            Shell.check("git add ./ci/*.py")
        self.generate()

    def generate(self):
        if not self.py_workflows:
            self.py_workflows = _get_workflows()
            assert self.py_workflows
        aux_configs_all = []
        for workflow_config in self.py_workflows:
            print(f"Generate workflow [{workflow_config.name}]")
            WorkflowConfigParser(workflow_config).parse()
            if workflow_config.is_event_pull_request():
                yaml_workflow, aux_configs = PullRequestPushYamlGen(workflow_config).generate()
                aux_configs_all += aux_configs
            elif workflow_config.is_event_push():
                yaml_workflow, aux_configs = PullRequestPushYamlGen(workflow_config).generate()
                aux_configs_all += aux_configs
            else:
                raise NotImplemented(f"Workflow event not yet supported [{workflow_config.event}]")

            with ContextManager.cd():
                with open(self._get_workflow_file_name(workflow_config.name), "w") as f:
                    f.write(yaml_workflow)

        for aux_config in aux_configs_all:  # type: Job.Requirements
            print(f"Generating aux workflow [{aux_config}]")
            yaml_workflow = AuxYamlGen(aux_config).generate()
            with ContextManager.cd():
                with open(aux_config.get_aux_workflow_name(), "w") as f:
                    f.write(yaml_workflow.strip() + "\n")

        Shell.check("git add ./.github/workflows/*.yaml")


class PullRequestPushYamlGen:
    def __init__(self, workflow_config: Workflow.Config):
        self.workflow_config = workflow_config

    def generate(self):
        required_aux_workflow_configs = []
        template_1 = Templates.TEMPLATE_PULL_REQUEST_0.strip().format(
            NAME=self.workflow_config.name,
            EVENT=self.workflow_config.event,
            JOBS="{}" * len(self.workflow_config.jobs),
            BASE_BRANCH=Settings.MAIN_BRANCH_NAME)
        template_1_args = []
        for i, job in enumerate(self.workflow_config.jobs):
            aux_workflow_name = job.job_requirements.get_aux_workflow_name()
            aux_workflow_input = job.job_requirements.get_aux_workflow_input()
            needs_line=",".join(job.auto_dependencies) if job.auto_dependencies else ""
            needs_line.removeprefix(",")
            required_aux_workflow_configs.append(job.job_requirements)
            template_1_args.append(
                Templates.TEMPLATE_JOB.format(
                    JOB_NAME=job.name,
                    NEEDS=needs_line,
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
    JOB_HELLO_WORLD = "Hello_World"
    JOB_HELLO_RECURCIPY = "Hello_RecurCIPY"
    JOB_LINT = "Yaml_Lint"


class ArtifactNames(MetaClasses.WithIter):
    """
    Predefined names of artifacts
    """
    GREET = "greet"


class WorkflowNames(MetaClasses.WithIter):
    """
    Workflow names
    """
    PULL_REQUEST = "Pull Request"
    MASTER = "Main"


workflow_pr = Workflow.Config(
    name=WorkflowNames.PULL_REQUEST,
    event=Workflow.Event.PULL_REQUEST,
    jobs=[
        Job.Config(
            name=JobNames.JOB_HELLO_WORLD,
            command="echo Hello World",
            provides=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_HELLO_RECURCIPY,
            command="echo Hello World",
            requires=[ArtifactNames.GREET],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            requires=[JobNames.JOB_HELLO_RECURCIPY],
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ]
)

workflow_master = Workflow.Config(
    name=WorkflowNames.MASTER,
    event=Workflow.Event.PUSH,
    jobs=[
        Job.Config(
            name=JobNames.JOB_HELLO_WORLD,
            command="echo Hello Hello World",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
        Job.Config(
            name=JobNames.JOB_LINT,
            command="yamllint . --config-file=.yamllint",
            job_requirements=Job.Requirements(python_requirements="requirements.txt")
        ),
    ]
)

"""
recurCIPY entry-point for generating yaml configs
each item ends up in workflow yaml file
"""
WORKFLOWS = [
    workflow_pr,
    workflow_master,
]  # type: List[Workflow.Config]
'''
