import glob
from itertools import chain
from pathlib import Path

from praktika import Workflow
from praktika._settings import GHRunners
from praktika.mangle import _get_workflows
from praktika.settings import Settings
from praktika.utils import ContextManager


class Validator:
    @classmethod
    def validate(cls):
        print("===Start validating Pipeline and settings===")
        workflows = _get_workflows()
        for workflow in workflows:
            print(f"Validating workflow [{workflow.name}]")

            cls.validate_file_paths_in_run_command(workflow)
            cls.validate_file_paths_in_digest_configs(workflow)

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
                    Settings.CI_CONFIG_RUNS_ON
                ), f"Runner label to run workflow config job must be provided via CACHE_CONFIG_RUNS_ON setting if enable_cache=True, workflow [{workflow.name}]"

                assert (
                    Settings.CACHE_S3_PATH
                ), f"CACHE_S3_PATH Setting must be defined if enable_cache=True, workflow [{workflow.name}]"

            if workflow.enable_html:
                assert (
                    Settings.HTML_S3_PATH
                ), f"HTML_S3_PATH Setting must be defined if enable_html=True, workflow [{workflow.name}]"
                assert (
                    Settings.S3_BUCKET_TO_HTTP_ENDPOINT
                ), f"S3_BUCKET_TO_HTTP_ENDPOINT Setting must be defined if enable_html=True, workflow [{workflow.name}]"
                assert (
                    Settings.HTML_S3_PATH.split("/")[0]
                    in Settings.S3_BUCKET_TO_HTTP_ENDPOINT
                ), f"S3_BUCKET_TO_HTTP_ENDPOINT Setting must include bucket name [{Settings.HTML_S3_PATH}] from HTML_S3_PATH, workflow [{workflow.name}]"

            if workflow.enable_cache:
                for artifact in workflow.artifacts or []:
                    assert (
                        artifact.is_s3_artifact()
                    ), f"All artifacts must be of S3 type if enable_cache|enable_html=True, artifact [{artifact.name}], type [{artifact.type}], workflow [{workflow.name}]"

            if workflow.dockers:
                assert (
                    Settings.DOCKERHUB_USERNAME
                ), f"Settings.DOCKERHUB_USERNAME must be provided if workflow has dockers, workflow [{workflow.name}]"
                assert (
                    Settings.DOCKERHUB_SECRET
                ), f"Settings.DOCKERHUB_SECRET must be provided if workflow has dockers, workflow [{workflow.name}]"
                assert workflow.get_secret(
                    Settings.DOCKERHUB_SECRET
                ), f"Secret [{Settings.DOCKERHUB_SECRET}] must have configuration in workflow.secrets, workflow [{workflow.name}]"

    @classmethod
    def validate_file_paths_in_run_command(cls, workflow: Workflow.Config) -> None:
        if not Settings.VALIDATE_FILE_PATHS:
            return
        with ContextManager.cd():
            for job in workflow.jobs:
                run_command = job.command
                command_parts = run_command.split(" ")
                for part in command_parts:
                    if ">" in part:
                        return
                    if "/" in part:
                        assert (
                            Path(part).is_file() or Path(part).is_dir()
                        ), f"Apparently run command [{run_command}] for job [{job}] has invalid path [{part}]. Setting to disable check: VALIDATE_FILE_PATHS"

    @classmethod
    def validate_file_paths_in_digest_configs(cls, workflow: Workflow.Config) -> None:
        if not Settings.VALIDATE_FILE_PATHS:
            return
        with ContextManager.cd():
            for job in workflow.jobs:
                if not job.digest_config:
                    continue
                for include_path in chain(
                    job.digest_config.include_paths, job.digest_config.exclude_paths
                ):
                    if "*" in include_path:
                        assert glob.glob(
                            include_path, recursive=True
                        ), f"Apparently file glob [{include_path}] in job [{job.name}] digest_config [{job.digest_config}] invalid, workflow [{workflow.name}]. Setting to disable check: VALIDATE_FILE_PATHS"
                    else:
                        assert (
                            Path(include_path).is_file() or Path(include_path).is_dir()
                        ), f"Apparently file path [{include_path}] in job [{job.name}] digest_config [{job.digest_config}] invalid, workflow [{workflow.name}]. Setting to disable check: VALIDATE_FILE_PATHS"
