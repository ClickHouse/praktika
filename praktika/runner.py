import argparse
import sys

from praktika._settings import _Settings
from praktika.artifact import Artifact
from praktika.cidb import CIDB
from praktika.hook_html import HtmlRunnerHooks
from praktika.hook_cache import CacheRunnerHooks
from praktika.mangle import _get_workflows
from praktika.result import Result, ResultInfo
from praktika.runtime import WorkflowRuntime
from praktika._environment import _Environment
from praktika.settings import Settings
from praktika.utils import Shell, Utils, TeePopen
from praktika.s3 import S3


class Runner:
    def pre_run(self, job_name, workflow_name):
        # Update and dump environment
        env = _Environment.from_env().set_job_name(job_name)
        print(f"Environment: [{env}]")

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
                    branch=_Environment.get().BRANCH,
                    pr_number=_Environment.get().PR_NUMBER,
                    sha=_Environment.get().SHA,
                    name=artifact.path,
                )

        # set pre-step ok in env
        env.PRAKTIKA_PRERUN_STEP_EXIT_CODE = 0
        env.dump()
        return True

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

        with TeePopen(cmd, timeout=job.timeout) as process:
            exit_code = process.wait()

            result = Result.from_fs(job_name)
            if process.timeout_exceeded:
                print(
                    f"WARNING: Job timed out: [{job_name}], timeout [{job.timeout}], exit code [{exit_code}]"
                )
                if not result.is_completed() or result.is_ok():
                    result.set_status(Result.Status.ERROR)
                result.set_info(ResultInfo.TIMEOUT)
            elif exit_code != 0:
                result.set_status(Result.Status.ERROR).set_info(ResultInfo.KILLED)
            result.dump()

        env = _Environment.get()
        env.PRAKTIKA_RUN_STEP_EXIT_CODE = exit_code
        env.dump()

        if exit_code == 0:
            print(f"run command failed with exit code [{exit_code}]")

        return exit_code == 0

    def post_run(self, job_name, workflow_name):
        print(f"Run post-run script [{job_name}], workflow [{workflow_name}]")
        info_errors = []
        workflow = _get_workflows(name=workflow_name)[0]
        job = workflow.get_job(job_name)
        assert job, "BUG"
        env = _Environment.get()

        if not env.setup_ok():
            info = "ERROR: Set up Env step failed. praktika bug or misconfiguration"
            print(info)
            # set Result with error and logs
            Result(
                name=job_name,
                status=Result.Status.ERROR,
                start_time=Utils.timestamp(),
                duration=0.0,
                info=ResultInfo.SETUP_ENV_JOB_FAILED,
            ).dump()
            info_errors.append(info)
        elif not env.prerun_ok():
            info = "ERROR: Prerun step failed. praktika bug or misconfiguration"
            print(info)
            # set Result with error and logs
            Result(
                name=job_name,
                status=Result.Status.ERROR,
                start_time=Utils.timestamp(),
                duration=0.0,
                info=ResultInfo.PRE_JOB_FAILED,
                files=[Settings.POST_LOG],
            ).dump()
            info_errors.append(info)

        if not Result.exist(job_name):
            Result(
                name=job_name,
                start_time=Utils.timestamp(),
                duration=None,
                status=Result.Status.ERROR,
                info=ResultInfo.NOT_FOUND_IMPOSSIBLE,
            ).dump()
            print(f"ERROR: {ResultInfo.NOT_FOUND_IMPOSSIBLE}")
            info_errors.append(ResultInfo.NOT_FOUND_IMPOSSIBLE)

        result = Result.from_fs(job_name)
        if not env.run_ok() and result.info:
            # provide job info to workflow level
            info_errors.append(result.info)

        if not result.is_completed():
            result.info = ResultInfo.KILLED
            result.status = Result.Status.ERROR
            print(f"ERROR: {ResultInfo.KILLED}")
            info_errors.append(ResultInfo.KILLED)
        result.set_files(files=[Settings.RUN_LOG]).update_duration().dump()

        run_exit_code = env.PRAKTIKA_RUN_STEP_EXIT_CODE
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
                        branch=_Environment.get().BRANCH,
                        pr_number=_Environment.get().PR_NUMBER,
                        sha=_Environment.get().SHA,
                        path=artifact.path,
                    )
        else:
            print(f"Job exit code [{run_exit_code} != 0] - skip artifact upload")

        if workflow.enable_cidb:
            print("Insert results to CIDB")
            try:
                CIDB(
                    url=workflow.get_secret(Settings.SECRET_CI_DB_URL).get_value(),
                    passwd=workflow.get_secret(
                        Settings.SECRET_CI_DB_PASSWORD
                    ).get_value(),
                ).insert(result)
            except Exception as ex:
                error = f"ERROR: Failed to insert data into CI DB, exception [{ex}]"
                print(error)
                info_errors.append(error)

        if workflow.enable_html:
            HtmlRunnerHooks.post_run(workflow, job, info_errors)

        # always in the end
        if run_exit_code == 0:
            if workflow.enable_cache:
                CacheRunnerHooks.post_run(workflow, job)

        return True


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

    res = False
    if args.pre_run:
        assert (
            args.job_name and args.workflow_name
        ), f"--job-name required with --pre-run"
        res = Runner().pre_run(args.job_name, args.workflow_name)
    elif args.run:
        assert args.job_name and args.workflow_name, f"--job-name required with --run"
        res = Runner().run(args.job_name, args.workflow_name)
    elif args.post_run:
        assert (
            args.job_name and args.workflow_name
        ), f"--job-name required with --post-run"
        res = Runner().post_run(args.job_name, args.workflow_name)
    else:
        assert False

    if not res:
        sys.exit(1)
