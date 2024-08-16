import dataclasses
from pathlib import Path
from typing import Optional, List

from recurcipy import Workflow, Job, ContextManager, Artifact, Environment
from recurcipy.mangle import _get_workflows
from recurcipy.parser import WorkflowConfigParser, WorkflowYaml, AddonType
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

    def generate_from_example(self, example):
        with ContextManager.cd():
            example = example.removesuffix(".py")
            example_path = f"{Settings.EXAMPLES_DIRECTORY}/{example}.py"
            assert Path(example_path).exists(), f"Example [{example}] does not exist"
            Path(Settings.CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
            Shell.check(f"cp {example_path} {Settings.CONFIG_DIRECTORY}/")
            Shell.check(f"git add {Settings.CONFIG_DIRECTORY}/*.py")
        self.generate()

    def generate(self):
        if not self.py_workflows:
            self.py_workflows = _get_workflows()
            assert self.py_workflows
        for workflow_config in self.py_workflows:
            print(f"Generate workflow [{workflow_config.name}]")
            parser = WorkflowConfigParser(workflow_config).parse()
            if (
                workflow_config.is_event_pull_request()
                or workflow_config.is_event_push()
            ):
                yaml_workflow_str = PullRequestPushYamlGen(parser).generate()
            else:
                assert (
                    False
                ), f"Workflow event not yet supported [{workflow_config.event}]"

            with ContextManager.cd():
                with open(self._get_workflow_file_name(workflow_config.name), "w") as f:
                    f.write(yaml_workflow_str)

        with ContextManager.cd():
            Shell.check("git add ./.github/workflows/*.yaml")


class PullRequestPushYamlGen:
    def __init__(self, parser: WorkflowConfigParser):
        self.workflow_config = parser.workflow_yaml_config
        self.parser = parser

    def generate(self):
        required_aux_workflow_configs = []
        template_1 = Templates.TEMPLATE_PULL_REQUEST_0.strip().format(
            NAME=self.workflow_config.name,
            EVENT=self.workflow_config.event,
            JOBS="{}\n" * len(self.workflow_config.jobs),
            BASE_BRANCH=Settings.MAIN_BRANCH_NAME,
        )

        job_items = []
        setup_envs = Templates.TEMPLATE_SETUP_ENV.format(
            TEMP_DIR=Environment.TEMP_DIR,
            INPUT_DIR=Environment.INPUT_DIR,
            OUTPUT_DIR=Environment.OUTPUT_DIR,
        )
        for i, job in enumerate(self.workflow_config.jobs):
            job_name_normalized = Utils.normalize_string(job.name)
            needs = ", ".join(map(Utils.normalize_string, job.needs))
            job_name = job.name
            job_addons = []
            for addon in job.addons:
                if addon.type == AddonType.PY:
                    job_addons.append(
                        Templates.TEMPLATE_PY_ADDONS.format(REQUIREMENT_PATH=addon.path)
                    )
            uploads_github = []
            for artifact in job.artifacts_gh_provides:
                uploads_github.append(
                    Templates.TEMPLATE_GH_UPLOAD.format(
                        NAME=artifact.name, PATH=artifact.path
                    )
                )
            downloads_github = []
            for artifact in job.artifacts_gh_requires:
                downloads_github.append(
                    Templates.TEMPLATE_GH_DOWNLOAD.format(
                        NAME=artifact.name, PATH=Environment.INPUT_DIR
                    )
                )
            job_item = Templates.TEMPLATE_JOB_0.format(
                JOB_NAME_NORMALIZED=job_name_normalized,
                RUNS_ON=", ".join(job.runs_on),
                NEEDS=needs,
                JOB_NAME=job_name,
                WORKFLOW_NAME=self.workflow_config.name,
                SETUP_ENVS=setup_envs,
                JOB_ADDONS="\n".join(job_addons),
                DOWNLOADS_GITHUB="\n".join(downloads_github),
                UPLOADS_GITHUB="\n".join(uploads_github),
            )
            job_items.append(job_item.rstrip("\n"))
        res = template_1.format(*job_items)

        return res


@dataclasses.dataclass
class AuxConfig:
    # defines aux step to install dependencies
    addon: Job.Requirements
    # defines aux step(s) to upload GH artifacts
    uploads_gh: List[Artifact.Config]
    # defines aux step(s) to download GH artifacts
    downloads_gh: List[Artifact.Config]

    def get_aux_workflow_name(self):
        suffix = ""
        if self.addon.python_requirements:
            suffix += "_py"
        for _ in self.uploads_gh:
            suffix += "_uplgh"
        for _ in self.downloads_gh:
            suffix += "_dnlgh"
        return f"{Settings.WORKFLOW_PATH_PREFIX}/aux_job{suffix}.yaml"

    def get_aux_workflow_input(self):
        res = ""
        if self.addon.python_requirements:
            res += f"      requirements_txt: {self.addon.python_requirements}"
        return res


class AuxYamlGen:
    def __init__(self, aux_config: AuxConfig):
        self.addon = aux_config.addon
        self.config = aux_config

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


if __name__ == "__main__":
    G = YamlGenerator()
    WFS = [
        Workflow.Config(
            name="PR",
            event=Workflow.Event.PULL_REQUEST,
            jobs=[
                Job.Config(
                    name="Hello World",
                    job_requirements=Job.Requirements(
                        python_requirements="./requirement.txt"
                    ),
                )
            ],
        )
    ]
    G.generate()
