import argparse
import dataclasses
import json
import sys

from recurcipy import Shell, Artifact
from recurcipy.cache import Cache
from recurcipy.mangle import _get_workflows
from recurcipy.s3 import S3Utils
from recurcipy.settings import Environment, Settings
from recurcipy.aux_job import _workflow_config_job
from recurcipy.runtime import _WorkflowRuntimeConfig
from recurcipy.utils import Utils


class Runner:
    def pre_run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run pre-run script [{job_name}], workflow [{workflow.name}]")

        envs = {"JOB_NAME": job_name}
        print(f"Exporting env variables [{envs}]")
        for k, v in envs.items():
            Shell.check(f'export {k}="{v}"')
        Shell.check("env")

        if job_name == Settings.CACHE_CONFIG_JOB_NAME:
            job = _workflow_config_job
        else:
            job = workflow.get_job(job_name)
        assert job, "BUG"
        required_artifacts = []
        if job.requires and workflow.artifacts:
            for requires_artifact_name in job.requires:
                for artifact in workflow.artifacts:
                    if (
                        artifact.name == requires_artifact_name
                        and artifact.type == Artifact.Type.S3
                    ):
                        required_artifacts.append(artifact)
        if required_artifacts:
            print(f"Job requires s3 artifacts [{required_artifacts}]")
            runtime_config = None
            if workflow.enable_cache:
                runtime_config = _WorkflowRuntimeConfig.from_fs()
            for artifact in required_artifacts:
                if runtime_config and artifact.name in runtime_config.cache_artifacts:
                    record = runtime_config.cache_artifacts[artifact.name]
                    print(f"Reuse artifact form [{record}]")
                    assert S3Utils.copy_artifact_from_s3(
                        branch=record.branch,
                        pr_number=record.pr_number,
                        sha=record.sha,
                        name=artifact.path,
                    )
                else:
                    assert S3Utils.copy_artifact_from_s3(
                        branch=Environment.BRANCH,
                        pr_number=Environment.EventInfo.PR_NUMBER,
                        sha=Environment.EventInfo.REF_SHA,
                        name=artifact.path,
                    )

    def run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run script [{job_name}], workflow [{workflow.name}]")

        if not workflow:
            print(f"ERROR: failed to get workflow [{workflow.name}]")
        if job_name == Settings.CACHE_CONFIG_JOB_NAME:
            job = _workflow_config_job
        else:
            job = workflow.get_job(job_name)
        assert job
        print(f"Run command [{job.command}]")
        return Shell.run(job.command)

    def post_run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run post-run script [{job_name}], workflow [{workflow.name}]")

        if job_name == Settings.CACHE_CONFIG_JOB_NAME:
            job = _workflow_config_job
        else:
            job = workflow.get_job(job_name)
        assert job, "BUG"
        providing_artifacts = []
        if job.provides and workflow.artifacts:
            for provides_artifact_name in job.provides:
                for artifact in workflow.artifacts:
                    if (
                        artifact.name == provides_artifact_name
                        and artifact.type == Artifact.Type.S3
                    ):
                        providing_artifacts.append(artifact)
        if providing_artifacts:
            print(f"Job provides s3 artifacts [{providing_artifacts}]")
            for artifact in providing_artifacts:
                assert Shell.check(
                    f"ls -l {artifact.path}", verbose=True
                ), f"Artifact {artifact.path} not found"
                assert S3Utils.copy_artifact_to_s3(
                    branch=Environment.BRANCH,
                    pr_number=Environment.EventInfo.PR_NUMBER,
                    sha=Environment.EventInfo.REF_SHA,
                    path=artifact.path,
                )

        if workflow.enable_cache and job.cache_digest:
            # cache is enabled, and it's a job that supposed to be cached (has defined digest config)
            workflow_runtime = _WorkflowRuntimeConfig.from_fs()
            job_digest = workflow_runtime.digests[job_name]
            Cache.push_success_record(job_name, job_digest, workflow_runtime.sha)

    def config(self):
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


def parse_args():
    parser = argparse.ArgumentParser("RecurCIPY")
    parser.add_argument(
        "--job-name",
        type=str,
    )
    parser.add_argument(
        "--workflow-name",
        type=str,
    )
    parser.add_argument(
        "--pre-run",
        action="store_true",
        help="Runs pre-run step for --job-name",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Runs run step for --job-name",
    )
    parser.add_argument(
        "--post-run",
        action="store_true",
        help="Runs post-run step for --job-name",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help="Configure Workflow Run, only for CI cache enabled",
    )
    return parser.parse_args(), parser


if __name__ == "__main__":
    args, parser = parse_args()
    res = 0

    if args.pre_run:
        assert (
            args.job_name and args.workflow_name
        ), f"--job-name required with --pre-run"
        Runner().pre_run(args.job_name, args.workflow_name)
    elif args.run:
        assert args.job_name and args.workflow_name, f"--job-name required with --run"
        res = Runner().run(args.job_name, args.workflow_name)
    elif args.post_run:
        assert (
            args.job_name and args.workflow_name
        ), f"--job-name required with --post-run"
        Runner().post_run(args.job_name, args.workflow_name)
    elif args.config:
        Runner().config()
    else:
        assert False

    sys.exit(res)
