import sys
from typing import Dict

from praktika import Job, Workflow
from praktika.digest import Digest
from praktika.docker import Docker
from praktika.environment import Environment
from praktika.hook_cache import CacheRunnerHooks
from praktika.hook_html import HtmlRunnerHooks
from praktika.mangle import _get_workflows
from praktika.result import Result
from praktika.runtime import WorkflowRuntime
from praktika.secret import Secret
from praktika.settings import Settings
from praktika.utils import Utils, Shell

assert Settings.CI_CONFIG_RUNS_ON

# TODO: think about dependencies requirements_with_gh_auth.txt.
#   it's not there outside of this repo
_workflow_config_job = Job.Config(
    name=Settings.CI_CONFIG_JOB_NAME,
    runs_on=Settings.CI_CONFIG_RUNS_ON,
    job_requirements=Job.Requirements(
        python=True,
        python_requirements_txt="requirements_with_gh_auth.txt",
    ),
    command=f"{Settings.PYTHON_INTERPRETER} -m praktika.native_jobs {Settings.CI_CONFIG_JOB_NAME}",
)

_docker_build_job = Job.Config(
    name=Settings.DOCKER_BUILD_JOB_NAME,
    runs_on=Settings.CI_CONFIG_RUNS_ON,
    job_requirements=Job.Requirements(
        python=True,
    ),
    command=f"{Settings.PYTHON_INTERPRETER} -m praktika.native_jobs {Settings.DOCKER_BUILD_JOB_NAME}",
)


def _build_dockers(workflow, job_name):
    print(f"Start [{job_name}], workflow [{workflow.name}]")
    dockers = workflow.dockers
    ready = []
    results = []
    job_status = Result.Status.SUCCESS
    job_info = ""
    dockers = Docker.sort_in_build_order(dockers)
    docker_digests = {}  # type: Dict[str, str]
    for docker in dockers:
        docker_digests[docker.name] = Digest().calc_docker_digest(docker, dockers)

    if not Shell.check(
        "docker buildx inspect --bootstrap | grep -q docker-container", verbose=True
    ):
        print("Install docker container driver")
        if not Shell.check(
            "docker buildx create --use --name mybuilder --driver docker-container",
            verbose=True,
        ):
            job_status = Result.Status.FAILED
            job_info = "Failed to install docker buildx driver"

    if job_status == Result.Status.SUCCESS:
        if not Docker.login(
            Settings.DOCKERHUB_USERNAME,
            user_password=Secret.get_value(
                workflow.get_secret(Settings.DOCKERHUB_SECRET)
            ),
        ):
            job_status = Result.Status.FAILED
            job_info = "Failed to login to dockerhub"

    if job_status == Result.Status.SUCCESS:
        for docker in dockers:
            assert (
                docker.name not in ready
            ), f"All docker names nust be uniq [{dockers}]"
            stopwatch = Utils.Stopwatch()
            digest = Digest().calc_docker_digest(docker, dockers)
            info = f"tag: {digest}"
            log_file = f"{Settings.OUTPUT_DIR}/docker_{Utils.normalize_string(docker.name)}.log"
            ret_code = Docker.build(
                docker, log_file=log_file, digests=docker_digests, add_latest=False
            )
            files = []
            if ret_code == 0:
                status = Result.Status.SUCCESS
            else:
                status = Result.Status.FAILED
                job_status = Result.Status.FAILED
                info += f", failed with exit code: {ret_code}, see log"
                files.append(log_file)
            ready.append(docker.name)
            results.append(
                Result(
                    name=docker.name,
                    status=status,
                    info=info,
                    duration=stopwatch.duration,
                    start_time=stopwatch.start_time,
                    files=files,
                )
            )
    Result.from_fs(job_name).set_status(job_status).set_results(results).set_info(
        job_info
    )

    if job_status != Result.Status.SUCCESS:
        sys.exit(1)


def _config_workflow(workflow: Workflow.Config, job_name):
    print(f"Start [{job_name}], workflow [{workflow.name}]")
    results = []
    files = []
    job_status = Result.Status.SUCCESS

    print("Check workflows are up to date")
    stop_watch = Utils.Stopwatch()
    output, exit_code = Shell.get_output_and_code(
        f"git diff-index HEAD -- {Settings.WORKFLOW_PATH_PREFIX}"
    )
    info = ""
    if exit_code != 0:
        info = f"workspace has uncommitted files unexpectedly [{output}]"
        job_status = Result.Status.ERROR
        print("ERROR: ", info)
    else:
        Shell.check(f"{Settings.PYTHON_INTERPRETER} -m praktika --generate")
        output, exit_code = Shell.get_output_and_code(
            f"git diff-index HEAD -- {Settings.WORKFLOW_PATH_PREFIX}"
        )
        if exit_code != 0:
            info = f"workspace has outdated workflows [{output}] - regenerate with [python -m praktika --generate]"
            job_status = Result.Status.ERROR
            print("ERROR: ", info)
    results.append(
        Result(
            name="Check Workflows updated",
            status=job_status,
            start_time=stop_watch.start_time,
            duration=stop_watch.duration,
            info=info,
        )
    )

    workflow_config = WorkflowRuntime(
        name=workflow.name,
        digest_jobs={},
        digest_dockers={},
        sha=Environment.get().SHA,
        cache_success=[],
        cache_artifacts={},
    ).dump()

    if workflow.dockers:
        print("Calculate docker's digests")
        dockers = workflow.dockers
        dockers = Docker.sort_in_build_order(dockers)
        for docker in dockers:
            workflow_config.digest_dockers[docker.name] = Digest().calc_docker_digest(
                docker, dockers
            )
        workflow_config.dump()

    if workflow.enable_cache:
        print("Check cache")
        stop_watch = Utils.Stopwatch()
        workflow_config = CacheRunnerHooks.configure(workflow)
        # TODO: return result from function configure() call?
        results.append(
            Result(
                name="CacheConfig",
                status=Result.Status.SUCCESS,
                start_time=stop_watch.start_time,
                duration=stop_watch.duration,
            )
        )
        files.append(WorkflowRuntime.file_name_static(workflow.name))

    workflow_config.dump()

    if workflow.enable_html:
        # must follow CacheRunnerHooks.configure(workflow) call,
        #   to see jobs to skip
        print("Check report")
        stop_watch = Utils.Stopwatch()
        HtmlRunnerHooks.configure(workflow)
        # TODO: return result from function configure() call?
        results.append(
            Result(
                name="ReportConfig",
                status=Result.Status.SUCCESS,
                start_time=stop_watch.start_time,
                duration=stop_watch.duration,
            )
        )
        files.append(Result.file_name_static(workflow.name))

    Result.from_fs(job_name).set_status(Result.Status.SUCCESS).set_results(
        results
    ).set_files(files)

    if job_status != Result.Status.SUCCESS:
        sys.exit(1)


if __name__ == "__main__":
    job_name = sys.argv[1]
    assert job_name, "Job name must be provided as input argument"
    workflow = _get_workflows(name=Environment.get().WORKFLOW_NAME)[0]
    if job_name == Settings.DOCKER_BUILD_JOB_NAME:
        _build_dockers(workflow, job_name)
    elif job_name == Settings.CI_CONFIG_JOB_NAME:
        _config_workflow(workflow, job_name)
    else:
        assert False, "BUG"
