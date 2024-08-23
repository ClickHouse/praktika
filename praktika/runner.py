import argparse
import sys

from praktika import Artifact
from praktika.auto_hooks import _CacheRunnerHooks, _HtmlRunnerHooks
from praktika.mangle import _get_workflows
from praktika.settings import Environment, Settings
from praktika.aux_job import _workflow_config_job
from praktika.utils import Shell
from praktika.s3 import S3


class Runner:
    def pre_run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run pre-run script [{job_name}], workflow [{workflow.name}]")

        envs = {"JOB_NAME": job_name}
        print(f"Exporting env variables [{envs}]")
        for k, v in envs.items():
            Shell.check(f'export {k}="{v}"')
        Shell.check("env")

        if job_name == Settings.CI_CONFIG_JOB_NAME:
            return

        job = workflow.get_job(job_name)
        assert job, "BUG"

        if workflow.enable_html:
            _HtmlRunnerHooks.pre_run(workflow, job)

        required_artifacts = []
        if job.requires and workflow.artifacts:
            for requires_artifact_name in job.requires:
                for artifact in workflow.artifacts:
                    if (
                        artifact.name == requires_artifact_name
                        and artifact.type == Artifact.Type.S3
                    ):
                        required_artifacts.append(artifact)
        print(f"Job requires s3 artifacts [{required_artifacts}]")
        if workflow.enable_cache:
            _CacheRunnerHooks.pre_run(
                _job=job, _workflow=workflow, _required_artifacts=required_artifacts
            )
        else:
            for artifact in required_artifacts:
                assert S3.copy_artifact_from_s3(
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

        if job_name == Settings.CI_CONFIG_JOB_NAME:
            if workflow.enable_cache:
                _CacheRunnerHooks.configure(workflow)
            if workflow.enable_html:
                _HtmlRunnerHooks.configure(workflow)

        else:
            job = workflow.get_job(job_name)
            assert job
            print(f"Run command [{job.command}]")
            return Shell.run(job.command)

    def post_run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run post-run script [{job_name}], workflow [{workflow.name}]")

        if job_name == Settings.CI_CONFIG_JOB_NAME:
            return

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
                assert S3.copy_artifact_to_s3(
                    branch=Environment.BRANCH,
                    pr_number=Environment.EventInfo.PR_NUMBER,
                    sha=Environment.EventInfo.REF_SHA,
                    path=artifact.path,
                )

        if workflow.enable_html:
            _HtmlRunnerHooks.post_run(workflow, job)

        # always in the end
        if workflow.enable_cache:
            _CacheRunnerHooks.post_run(workflow, job)


def parse_args():
    parser = argparse.ArgumentParser("praktika")
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
    else:
        assert False

    sys.exit(res)
