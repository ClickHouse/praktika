from datetime import datetime

from praktika.gh import GH
from praktika.parser import WorkflowConfigParser
from praktika.utils import Utils, MetaClasses
from praktika.html_generator import HtmlGenerator
from praktika.hook_interface import HookInterface
from praktika.runtime import _WorkflowRuntimeConfig, _RuntimeVars
from praktika.result import Result, ResultInfo


class HtmlRunnerHooks(HookInterface, MetaClasses.FormatPrint):
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
        summary_result.start_time = Utils.timestamp()
        summary_result.copy_to_s3()
        _ = HtmlGenerator.generate_recursive(summary_result, upload_to_s3=True)
        res1 = GH.post_commit_status(
            name=_workflow.name,
            status=Result.Status.PENDING,
            description="",
            url=summary_result.html_link,
        )
        res2 = GH.post_pr_comment(
            comment_body=f"[CI Status]({summary_result.html_link}) for [{_workflow.name}] workflow",
            or_update_comment_with_substring="[CI Status]",
        )
        assert (
            res1 or res2
        ), "Failed to set both GH commit status and PR comment with Workflow Status, cannot proceed"

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
            workflow_result, upload_to_s3=True, changed_items=[result]
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
                start_time=None,
                duration=None,
                status=Result.Status.ERROR,
                info=ResultInfo.NOT_FOUND_IMPOSSIBLE,
            ).dump()
            print(
                f"ERROR: critical bug or job misbehaviour, nor job Result neither pre-Result is found"
            )
        elif result.status == Result.Status.RUNNING:
            result.info = ResultInfo.NOT_FOUND
            result.status = Result.Status.ERROR
            print(f"ERROR: job was killed or died without providing Result")

        result.update_duration()
        print(f"Job Result [{result}]")

        workflow_result = Result.from_s3(_workflow.name)
        old_status = workflow_result.status
        workflow_result.update_sub_result(result)

        skipped_job_results = []
        if _RuntimeVars.run_failed():
            print(
                "Current job failed - find dependee jobs in the workflow and set their statuses to skipped"
            )
            workflow_config_parsed = WorkflowConfigParser(_workflow).parse()
            for dependee_job in workflow_config_parsed.workflow_yaml_config.jobs:
                if _job.name in dependee_job.needs:
                    print(
                        f"NOTE: Set job [{_job.name}] status to [{Result.Status.SKIPPED}] due to current failure"
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

        _ = HtmlGenerator.generate_recursive(
            workflow_result,
            upload_to_s3=True,
            changed_items=[result] + skipped_job_results,
        )
        print("Workflow summary Result:")
        print(workflow_result)
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
