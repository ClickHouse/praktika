import dataclasses
from typing import Dict, List, Optional

from recurcipy import Workflow, Artifact, Job
from recurcipy.mangle import _get_workflows


class AddonType:
    PY = "py"


@dataclasses.dataclass
class WorkflowYaml:
    @dataclasses.dataclass
    class JobYaml:
        name: str
        needs: List[str]
        runs_on: List[str]
        artifacts_gh_requires: List["WorkflowYaml.ArtifactYaml"]
        artifacts_gh_provides: List["WorkflowYaml.ArtifactYaml"]
        addons: List["WorkflowYaml.JobAddonYaml"]

    @dataclasses.dataclass
    class ArtifactYaml:
        name: str
        provided_by: str
        required_by: List[str]
        path: str
        type: str

    @dataclasses.dataclass
    class JobAddonYaml:
        type: str
        path: str

    name: str
    event: str
    jobs: List[JobYaml]
    job_to_config: Dict[str, JobYaml]
    artifact_to_config: Dict[str, ArtifactYaml]


class WorkflowConfigParser:
    def __init__(self, config: Workflow.Config):
        self.workflow_name = config.name
        self.config = config
        self.requires_all = []
        self.provides_all = []
        self.job_names_all = []
        self.artifact_to_providing_job_map = {}
        self.artifact_to_job_requires_map = {}
        self.artifact_map = {}

        self.job_to_provides_artifacts = {}  # type: Dict[str, List[Artifact.Config]]
        self.job_to_requires_artifacts = {}  # type: Dict[str, List[Artifact.Config]]

        self.workflow_yaml_config = WorkflowYaml(
            name=self.workflow_name,
            event=config.event,
            jobs=[],
            job_to_config={},
            artifact_to_config={},
        )

    # def _build_dependencies(self):
    #     for job in self.config.jobs:
    #         if job.requires:
    #             dependencies = []
    #             for artifact in job.requires:
    #                 dependency = self.artifact_to_providing_job_map[artifact]
    #                 dependencies.append(dependency)
    #             job.auto_dependencies = dependencies

    def parse(self):
        # populate WorkflowYaml.artifact_to_config with phony artifacts
        for job in self.config.jobs:
            assert (
                job.name not in self.workflow_yaml_config.artifact_to_config
            ), f"Not uniq Job name [{job.name}], workflow [{self.workflow_name}]"
            self.workflow_yaml_config.artifact_to_config[
                job.name
            ] = WorkflowYaml.ArtifactYaml(
                name=job.name,
                provided_by=job.name,
                required_by=[],
                path="",
                type=Artifact.Type.PHONY,
            )

        # populate jobs
        for job in self.config.jobs:
            job_yaml_config = WorkflowYaml.JobYaml(
                name=job.name,
                addons=[],
                artifacts_gh_requires=[],
                artifacts_gh_provides=[],
                needs=[],
                runs_on=[],
            )
            self.workflow_yaml_config.jobs.append(job_yaml_config)
            assert (
                job.name not in self.workflow_yaml_config.job_to_config
            ), f"Job name [{job.name}] is not uniq, workflow [{self.workflow_name}]"
            self.workflow_yaml_config.job_to_config[job.name] = job_yaml_config

        # populate WorkflowYaml.artifact_to_config
        if self.config.artifacts:
            for artifact in self.config.artifacts:
                assert (
                    artifact.name not in self.workflow_yaml_config.artifact_to_config
                ), f"Artifact name [{artifact.name}] is not uniq, workflow [{self.workflow_name}]"
                artifact_yaml_config = WorkflowYaml.ArtifactYaml(
                    name=artifact.name,
                    provided_by="",
                    required_by=[],
                    path=artifact.path,
                    type=artifact.type,
                )
                self.workflow_yaml_config.artifact_to_config[
                    artifact.name
                ] = artifact_yaml_config

        # populate ArtifactYaml.provided_by
        for job in self.config.jobs:
            if job.provides:
                for artifact_name in job.provides:
                    assert (
                        artifact_name in self.workflow_yaml_config.artifact_to_config
                    ), f"Artifact [{artifact_name}] has no config, job [{job.name}], workflow [{self.workflow_name}]"
                    assert not self.workflow_yaml_config.artifact_to_config[
                        artifact_name
                    ].provided_by, f"Artifact [{artifact_name}] provided by multiple jobs [{self.workflow_yaml_config.artifact_to_config[artifact_name].provided_by}] and [{job.name}]"
                    self.workflow_yaml_config.artifact_to_config[
                        artifact_name
                    ].provided_by = job.name

        # populate ArtifactYaml.required_by
        for job in self.config.jobs:
            if job.requires:
                for artifact_name in job.requires:
                    assert (
                        artifact_name in self.workflow_yaml_config.artifact_to_config
                    ), f"Artifact [{artifact_name}] has no config, job [{job.name}], workflow [{self.workflow_name}]"
                    assert self.workflow_yaml_config.artifact_to_config[
                        artifact_name
                    ].provided_by, f"Artifact [{artifact_name}] has no job providing it, required by job [{job.name}], workflow [{self.workflow_name}]"
                    self.workflow_yaml_config.artifact_to_config[
                        artifact_name
                    ].required_by.append(job.name)

        # populate JobYaml.addons
        for job in self.config.jobs:
            if job.job_requirements:
                if job.job_requirements.python_requirements:
                    addon_yaml = WorkflowYaml.JobAddonYaml(
                        type=AddonType.PY, path=job.job_requirements.python_requirements
                    )
                    self.workflow_yaml_config.job_to_config[job.name].addons.append(
                        addon_yaml
                    )
                else:
                    assert False, "only py addon/requirement supported"

        # populate JobYaml.runs_on
        for job in self.config.jobs:
            self.workflow_yaml_config.job_to_config[job.name].runs_on = job.runs_on

        # populate JobYaml.artifacts_gh_requires, JobYaml.artifacts_gh_provides and JobYaml.needs
        for (
            artifact_name,
            artifact,
        ) in self.workflow_yaml_config.artifact_to_config.items():
            assert (
                artifact.provided_by
                and artifact.provided_by in self.workflow_yaml_config.job_to_config
            ), f"Artifact [{artifact_name}] has no valid job providing it [{artifact.provided_by}]"
            for job_name in artifact.required_by:
                if (
                    artifact.provided_by
                    not in self.workflow_yaml_config.job_to_config[job_name].needs
                ):
                    self.workflow_yaml_config.job_to_config[job_name].needs.append(
                        artifact.provided_by
                    )
                if artifact.type in (Artifact.Type.GH,):
                    self.workflow_yaml_config.job_to_config[
                        job_name
                    ].artifacts_gh_requires.append(artifact)
                elif artifact.type in (Artifact.Type.PHONY, Artifact.Type.S3):
                    pass
                else:
                    assert (
                        False
                    ), f"Artifact [{artifact_name}] has unsupported type [{artifact.type}]"
            if not artifact.required_by and artifact.type != Artifact.Type.PHONY:
                print(
                    f"WARNING: Artifact [{artifact_name}] provided by job [{artifact.provided_by}] not required by any job in workflow [{self.workflow_name}]"
                )
            if artifact.type == Artifact.Type.GH:
                self.workflow_yaml_config.job_to_config[
                    artifact.provided_by
                ].artifacts_gh_provides.append(artifact)

        return self


if __name__ == "__main__":
    # test
    workflows = _get_workflows()
    for workflow in workflows:
        WorkflowConfigParser(workflow).parse()
