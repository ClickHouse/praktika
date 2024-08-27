import argparse
import sys

from praktika import Artifact
from praktika.hook_html import HtmlRunnerHooks
from praktika.hook_cache import CacheRunnerHooks
from praktika.mangle import _get_workflows
from praktika.runtime import _RuntimeVars
from praktika.settings import Settings
from praktika.environment import Environment
from praktika.utils import Shell
from praktika.s3 import S3


class Runner:
    def pre_run(self, job_name, workflow_name):
        if job_name == Settings.CI_CONFIG_JOB_NAME:
            return

        # reset env if any
        _RuntimeVars(RUN_EXIT_CODE=None).dump()

        # Update and dump environment
        Environment.JOB_NAME = job_name
        Environment.dump()

        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run pre-run script [{job_name}], workflow [{workflow.name}]")

        job = workflow.get_job(job_name)
        assert job, "BUG"

        if workflow.enable_html:
            HtmlRunnerHooks.pre_run(workflow, job)

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
            CacheRunnerHooks.pre_run(
                _job=job, _workflow=workflow, _required_artifacts=required_artifacts
            )
        else:
            for artifact in required_artifacts:
                assert S3.copy_artifact_from_s3(
                    branch=Environment.BRANCH,
                    pr_number=Environment.PR_NUMBER,
                    sha=Environment.SHA,
                    name=artifact.path,
                )

    def run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run script [{job_name}], workflow [{workflow.name}]")

        if not workflow:
            print(f"ERROR: failed to get workflow [{workflow.name}]")

        if job_name == Settings.CI_CONFIG_JOB_NAME:
            if workflow.enable_cache:
                CacheRunnerHooks.configure(workflow)
            if workflow.enable_html:
                HtmlRunnerHooks.configure(workflow)

        else:
            job = workflow.get_job(job_name)
            assert job
            print(f"Run command [{job.command}]")
            exit_code = Shell.run(job.command)
            _RuntimeVars(RUN_EXIT_CODE=exit_code).dump()
            return exit_code

    def post_run(self, job_name, workflow_name):
        if job_name == Settings.CI_CONFIG_JOB_NAME:
            return

        print(f"Run post-run script [{job_name}], workflow [{workflow_name}]")
        workflow = _get_workflows(name=workflow_name)[0]

        job = workflow.get_job(job_name)
        assert job, "BUG"

        run_exit_code = _RuntimeVars.from_fs().RUN_EXIT_CODE
        if run_exit_code == 0:
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
                        pr_number=Environment.PR_NUMBER,
                        sha=Environment.SHA,
                        path=artifact.path,
                    )
        else:
            print(f"Job exit code [{run_exit_code} != 0] - skip artifact upload")

        if workflow.enable_html:
            HtmlRunnerHooks.post_run(workflow, job)

        # always in the end
        if run_exit_code == 0:
            if workflow.enable_cache:
                CacheRunnerHooks.post_run(workflow, job)


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
