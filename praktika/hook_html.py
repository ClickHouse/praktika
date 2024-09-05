from datetime import datetime
import urllib.parse
from pathlib import Path

from praktika._environment import _Environment
from praktika.gh import GH
from praktika.parser import WorkflowConfigParser
from praktika.settings import Settings
from praktika.utils import Utils
from praktika.runtime import WorkflowRuntime
from praktika.result import Result, ResultInfo


class HtmlRunnerHooks:
    @classmethod
    def configure(cls, _workflow):
        # generate pending Results for all jobs in the workflow
        if _workflow.enable_cache:
            skip_jobs = WorkflowRuntime.from_fs(_workflow.name).cache_success
        else:
            skip_jobs = []

        results = []
        for job in _workflow.jobs:
            if job.name not in skip_jobs:
                result = Result.generate_pending(job.name)
            else:
                result = Result.generate_skipped(job.name)
            results.append(result)
        summary_result = Result.generate_pending(_workflow.name, results=results)
        summary_result.aux_links.append(_Environment.get().CHANGE_URL)
        summary_result.aux_links.append(_Environment.get().RUN_URL)
        summary_result.start_time = Utils.timestamp()
        json_url_encoded = urllib.parse.quote(summary_result.get_link(), safe="")
        page_url = "/".join(
            ["https:/", Settings.HTML_S3_PATH, str(Path(Settings.HTML_PAGE_FILE).name)]
        )
        for bucket, endpoint in Settings.S3_BUCKET_TO_HTTP_ENDPOINT.items():
            page_url = page_url.replace(bucket, endpoint)
        page_url += f"?results={json_url_encoded}"
        summary_result.html_link = page_url
        _ = summary_result.copy_to_s3()
        print(f"CI Status page url [{page_url}]")

        res1 = GH.post_commit_status(
            name=_workflow.name,
            status=Result.Status.PENDING,
            description="",
            url=page_url,
        )
        res2 = GH.post_pr_comment(
            comment_body=f"[CI Status]({page_url}), commit [{_Environment.get().SHA[:8]}], workflow [{_workflow.name}]",
            or_update_comment_with_substring=f", workflow [{_workflow.name}]",
        )
        assert (
            res1 or res2
        ), "Failed to set both GH commit status and PR comment with Workflow Status, cannot proceed"

    @classmethod
    def pre_run(cls, _workflow, _job):
        result = Result(
            name=_job.name,
            status=Result.Status.RUNNING,
            start_time=datetime.now().timestamp(),
        )
        result.dump()
        if not _workflow.enable_html or _job.name == Settings.CI_CONFIG_JOB_NAME:
            # SPECIAL handling
            return
        workflow_result = Result.from_s3(_workflow.name)
        workflow_result.update_sub_result(result)
        workflow_result.copy_to_s3()

    @classmethod
    def run(cls, _workflow, _job):
        pass

    @classmethod
    def post_run(cls, _workflow, _job, info_errors):
        print(f"Post run for job [{_job.name}], workflow [{_workflow.name}]")
        result = Result.from_fs(_job.name)

        workflow_result = Result.from_s3(_workflow.name)
        if info_errors:
            info_errors = [f"{_job.name}: {error}" for error in info_errors]
            info_str = workflow_result.info + "\n" + "\n".join(info_errors)
            print("Update workflow results with new info")
            workflow_result.set_info(info_str)
        old_status = workflow_result.status

        result.upload_files()
        workflow_result.update_sub_result(result)

        skipped_job_results = []
        if _Environment.get().PRAKTIKA_RUN_STEP_EXIT_CODE != 0:
            print(
                "Current job failed - find dependee jobs in the workflow and set their statuses to skipped"
            )
            workflow_config_parsed = WorkflowConfigParser(_workflow).parse()
            for dependee_job in workflow_config_parsed.workflow_yaml_config.jobs:
                if _job.name in dependee_job.needs:
                    print(
                        f"NOTE: Set job [{dependee_job.name}] status to [{Result.Status.SKIPPED}] due to current failure"
                    )
                    skipped_job_results.append(
                        Result(
                            name=dependee_job.name,
                            status=Result.Status.SKIPPED,
                            info=ResultInfo.SKIPPED_DUE_TO_PREVIOUS_FAILURE
                            + f" [{_job.name}]",
                        )
                    )
        for skipped_job_result in skipped_job_results:
            workflow_result.update_sub_result(skipped_job_result)

        workflow_result.copy_to_s3()
        if workflow_result.status != old_status:
            print(
                f"Update GH commit status [{result.name}]: [{old_status} -> {workflow_result.status}]"
            )
            GH.post_commit_status(
                name=workflow_result.name,
                status=GH.convert_to_gh_status(workflow_result.status),
                description="",
                url=workflow_result.html_link,
            )
