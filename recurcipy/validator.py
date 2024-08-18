from recurcipy.defaultsettings import GHRunners
from recurcipy.mangle import _get_workflows
from recurcipy.settings import Settings


class Validator:
    @classmethod
    def validate(cls):
        print("Start validating Pipeline and settings")
        workflows = _get_workflows()
        for workflow in workflows:
            print(f"Validating workflow [{workflow.name}]")
            if workflow.artifacts:
                for artifact in workflow.artifacts:
                    if artifact.is_s3_artifact():
                        assert (
                            Settings.S3_ARTIFACT_PATH
                        ), "Provide S3_ARTIFACT_PATH setting in any .py file in ./ci/settings/* to be able to use s3 for artifacts"

            for job in workflow.jobs:
                if job.requires and workflow.artifacts:
                    for require in job.requires:
                        if (
                            require in workflow.artifacts
                            and workflow.artifacts[require].is_s3_artifact()
                        ):
                            assert not any(
                                [r in GHRunners for r in job.runs_on]
                            ), f"GH runners [{job.name}:{job.runs_on}] must not be used with S3 as artifact storage"

            if workflow.enable_cache:
                assert (
                    Settings.CACHE_CONFIG_RUNS_ON
                ), f"Runner label to run workflow config job must be provided via CACHE_CONFIG_RUNS_ON setting if enable_cache=True, workflow [{workflow.name}]"

                assert (
                    Settings.CACHE_S3_PATH
                ), f"CACHE_S3_PATH Setting must be defined if enable_cache=True, workflow [{workflow.name}]"

                for artifact in workflow.artifacts:
                    assert (
                        artifact.is_s3_artifact()
                    ), f"All artifacts must be of S3 type if enable_cache=True, artifact [{artifact.name}], type [{artifact.type}], workflow [{workflow.name}]"
