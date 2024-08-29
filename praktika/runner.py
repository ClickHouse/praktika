import argparse
import sys

from praktika._settings import _Settings
from praktika.artifact import Artifact
from praktika.hook_html import HtmlRunnerHooks
from praktika.hook_cache import CacheRunnerHooks
from praktika.mangle import _get_workflows
from praktika.result import Result, ResultInfo
from praktika.runtime import _RuntimeVars, WorkflowRuntime
from praktika.environment import Environment
from praktika.utils import Shell, Utils
from praktika.s3 import S3


class Runner:
    def pre_run(self, job_name, workflow_name):
        # reset env if any
        _RuntimeVars(exit_code=None).dump()

        # Update and dump environment
        Environment.get().set_job_name(job_name)

        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run pre-run script [{job_name}], workflow [{workflow.name}]")

        job = workflow.get_job(job_name)
        assert job, "BUG"

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
                    branch=Environment.get().BRANCH,
                    pr_number=Environment.get().PR_NUMBER,
                    sha=Environment.get().SHA,
                    name=artifact.path,
                )

    def run(self, job_name, workflow_name):
        workflow = _get_workflows(name=workflow_name)[0]
        print(f"Run script [{job_name}], workflow [{workflow.name}]")

        if not workflow:
            print(f"ERROR: failed to get workflow [{workflow.name}]")

        job = workflow.get_job(job_name)
        assert job
        log_file = f"{_Settings.TEMP_DIR}/job_{Utils.normalize_string(job_name)}.log"
        print(f"Run command [{job.command}], log file [{log_file}]")
        if job.run_in_docker:
            # TODO: support any image, including not from ci
            docker_tag = WorkflowRuntime.from_fs(workflow_name).digest_dockers[
                job.run_in_docker
            ]
            cmd = f"docker run --rm -e PYTHONPATH='{_Settings.DOCKER_WD}' --volume ./:{_Settings.DOCKER_WD} --volume {_Settings.TEMP_DIR}:{_Settings.TEMP_DIR} --workdir={_Settings.DOCKER_WD} {job.run_in_docker}:{docker_tag} {job.command}"
        else:
            cmd = job.command
        exit_code = Shell.run(cmd, log_file=log_file, verbose=True)
        _RuntimeVars(exit_code=exit_code, log_files=[log_file]).dump()
        return exit_code

    def post_run(self, job_name, workflow_name):
        print(f"Run post-run script [{job_name}], workflow [{workflow_name}]")
        workflow = _get_workflows(name=workflow_name)[0]
        job = workflow.get_job(job_name)
        assert job, "BUG"

        result = Result.from_fs(job_name)
        if not result:
            result = Result(
                name=job_name,
                start_time=None,
                duration=None,
                status=Result.Status.ERROR,
                info=ResultInfo.NOT_FOUND_IMPOSSIBLE,
            )
            print(f"ERROR: {ResultInfo.NOT_FOUND_IMPOSSIBLE}")
        elif result.status == Result.Status.RUNNING:
            result.info = ResultInfo.NOT_FOUND
            result.status = Result.Status.ERROR
            print(f"ERROR: {ResultInfo.NOT_FOUND}")
        result.update_duration().dump()

        run_exit_code = _RuntimeVars.from_fs().exit_code
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
                        branch=Environment.get().BRANCH,
                        pr_number=Environment.get().PR_NUMBER,
                        sha=Environment.get().SHA,
                        path=artifact.path,
                    )
        else:
            print(f"Job exit code [{run_exit_code} != 0] - skip artifact upload")
            result.set_files(files=_RuntimeVars.from_fs().log_files)

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
