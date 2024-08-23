import dataclasses
import json
from datetime import datetime

from praktika.gh import GH
from praktika.utils import Utils, MetaClasses
from praktika.s3 import S3
from praktika.cache import Cache
from praktika.html_generator import HtmlGenerator
from praktika.interfaces import HookInterface
from praktika.mangle import _get_workflows
from praktika.runtime import _WorkflowRuntimeConfig
from praktika.result import Result, _PreResult, ResultInfo
from praktika.settings import Environment, Settings


class _CacheRunnerHooks(HookInterface):
    @classmethod
    def configure(cls, _workflow):
        workflow_runtime_config = _WorkflowRuntimeConfig(
            digests={},
            sha=Environment.EventInfo.REF_SHA,
            cache_success=[],
            cache_artifacts={},
        )
        cache = Cache()
        assert Environment.WORKFLOW_NAME
        workflow = _get_workflows(name=Environment.WORKFLOW_NAME)[0]
        print(f"Workflow Configure, workflow [{workflow.name}]")
        assert (
            workflow.enable_cache
        ), f"Outdated yaml pipelines or BUG. Configuration must be run only for workflow with enabled cache, workflow [{workflow.name}]"
        artifact_digest_map = {}
        job_digest_map = {}
        for job in workflow.jobs:
            if not job.cache_digest:
                print(
                    f"NOTE: job [{job.name}] has no Config.cache_digest - skip cache check, always run"
                )
            digest = cache.digest.calc_digest(job.cache_digest)
            job_digest_map[job.name] = digest
            if job.provides:
                # assign the job digest also to the artifacts it provides
                for artifact in job.provides:
                    artifact_digest_map[artifact] = digest
        for job in workflow.jobs:
            digests_combined_list = []
            if job.requires:
                # include digest of required artifact to the job digest, so that they affect job state
                for artifact_name in job.requires:
                    if artifact_name not in [
                        artifact.name for artifact in workflow.artifacts
                    ]:
                        # phony artifact assumed to be not affecting jobs that depend on it
                        continue
                    digests_combined_list.append(artifact_digest_map[artifact_name])
            digests_combined_list.append(job_digest_map[job.name])
            final_digest = "-".join(digests_combined_list)
            workflow_runtime_config.digests[job.name] = final_digest

        assert (
            workflow_runtime_config.digests
        ), f"BUG, Workflow with enabled cache must have job digests after configuration, wf [{workflow.name}]"

        print("Check remote cache")
        job_to_cache_record = {}
        for job_name, job_digest in workflow_runtime_config.digests.items():
            record = cache.fetch_success(job_name=job_name, job_digest=job_digest)
            if record:
                assert (
                    Utils.normalize_string(job_name)
                    not in workflow_runtime_config.cache_success
                )
                workflow_runtime_config.cache_success.append(job_name)
                job_to_cache_record[job_name] = record

        print("Check artifacts to reuse")
        for job in workflow.jobs:
            if job.name in workflow_runtime_config.cache_success:
                if job.provides:
                    for artifact_name in job.provides:
                        workflow_runtime_config.cache_artifacts[
                            artifact_name
                        ] = job_to_cache_record[job.name]

        print(f"Write config to job output env: {Environment.JOB_OUTPUT_STREAM}")
        with open(Environment.JOB_OUTPUT_STREAM, "a", encoding="utf8") as f:
            print(
                f"DATA={json.dumps(dataclasses.asdict(workflow_runtime_config))}",
                file=f,
            )
        print(f"WorkflowRuntimeConfig: [{workflow_runtime_config}]")

    @classmethod
    def pre_run(cls, _workflow, _job, _required_artifacts=None):
        runtime_config = _WorkflowRuntimeConfig.from_fs()
        required_artifacts = []
        if _required_artifacts:
            required_artifacts = _required_artifacts
        for artifact in required_artifacts:
            if artifact.name in runtime_config.cache_artifacts:
                record = runtime_config.cache_artifacts[artifact.name]
                print(f"Reuse artifact form [{record}]")
                assert S3.copy_artifact_from_s3(
                    branch=record.branch,
                    pr_number=record.pr_number,
                    sha=record.sha,
                    name=artifact.path,
                )

    @classmethod
    def run(cls, workflow, job):
        pass

    @classmethod
    def post_run(cls, workflow, job):
        if job.name == Settings.CI_CONFIG_JOB_NAME:
            return
        if job.cache_digest:
            # cache is enabled, and it's a job that supposed to be cached (has defined digest config)
            workflow_runtime = _WorkflowRuntimeConfig.from_fs()
            job_digest = workflow_runtime.digests[job.name]
            Cache.push_success_record(job.name, job_digest, workflow_runtime.sha)


class _HtmlRunnerHooks(HookInterface, MetaClasses.FormatPrint):
    @classmethod
    def configure(cls, _workflow):
        # generate pending Results for all jobs in workflow
        if _workflow.enable_cache:
            skip_jobs = _WorkflowRuntimeConfig.from_fs().cache_success
        else:
            skip_jobs = []

        results = []
        for job in _workflow.jobs:
            if job.name not in skip_jobs:
                result = Result.generate_pending(job.name)
                results.append(result)
        summary_result = Result.generate_pending(_workflow.name, results=results)
        summary_result.copy_to_s3()
        link = HtmlGenerator.generate_recursive(summary_result, upload_to_s3=True)
        GH.post_commit_status(
            name=_workflow.name,
            status=Result.Status.PENDING,
            description="",
            url=link,
        )

    @classmethod
    def pre_run(cls, _workflow, _job):
        cls.format_print("pre run hook")
        result = Result(
            name=_job.name,
            status=Result.Status.RUNNING,
            start_time=datetime.now().timestamp(),
        )
        result.dump()
        workflow_result = Result.from_s3(_workflow.name)
        workflow_result.update_sub_result(result)
        workflow_result.copy_to_s3()
        HtmlGenerator.generate_recursive(
            workflow_result, upload_to_s3=True, changed_item=result
        )

    @classmethod
    def run(cls, _workflow, _job):
        pass

    @classmethod
    def post_run(cls, _workflow, _job):
        result = Result.from_fs(_job.name)
        if not result:
            result = Result(
                name=_job.name,
                start_time=0.0,
                duration=0.0,
                status=Result.Status.ERROR,
                info=ResultInfo.NOT_FOUND_IMPOSSIBLE,
            ).dump()
        elif result.status == Result.Status.RUNNING:
            result.info = ResultInfo.NOT_FOUND
            result.status = Result.Status.ERROR
            result.update_duration()
        else:
            result.update_duration()

        workflow_result = Result.from_s3(_workflow.name)
        workflow_result.update_sub_result(result)
        workflow_result.copy_to_s3()
        HtmlGenerator.generate_recursive(
            workflow_result, upload_to_s3=True, changed_item=result
        )
