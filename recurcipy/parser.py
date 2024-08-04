from recurcipy import Workflow
from recurcipy.mangle import _get_workflows


class WorkflowConfigParser:

    def __init__(self, config: Workflow.Config):
        self.workflow_name = config.name
        self.config = config
        self.requires_all = []
        self.provides_all = []
        self.job_names_all = []
        self.artifact_to_job_provides_map = {}
        self.artifact_to_job_requires_map = {}

    def _build_dependencies(self):
        for job in self.config.jobs:
            if job.requires:
                dependencies = []
                for artifact in job.requires:
                    dependency = self.artifact_to_job_provides_map[artifact]
                    dependencies.append(dependency)
                job.set_dependencies(dependencies)

    def parse(self):
        for job in self.config.jobs:
            assert job.name not in self.job_names_all, f"Job Name must be uniq per workflow, check workflow config for [{self.workflow_name}]"
            self.job_names_all.append(job.name)
            if job.provides:
                for artifact in job.provides:
                    assert artifact not in self.provides_all, f"Names in @provides must be uniq per workflow, check workflow/job config [{self.workflow_name}/{job.name}]"
                    self.provides_all.append(artifact)
                    self.artifact_to_job_provides_map[artifact] = job.name
        for job in self.job_names_all:
            if job not in self.provides_all:
                self.provides_all.append(job)
                self.artifact_to_job_provides_map[job] = job

        for job in self.config.jobs:
            if job.requires:
                for artifact in job.requires:
                    assert artifact in self.provides_all, f"Names in @requires must exist in @provides for other jobs or be a valid job names, check workflow/job config [{self.workflow_name}/{job.name}]"
                    assert artifact not in self.requires_all, f"Names in @provides must be uniq per workflow, check workflow/job config [{self.workflow_name}/{job.name}]"
                    self.requires_all.append(artifact)
                    self.artifact_to_job_requires_map[artifact] = job.name

        self._build_dependencies()

        return self.config


if __name__ == '__main__':
    # test
    workflows = _get_workflows()
    for workflow in workflows:
        WorkflowConfigParser(workflow).parse()

