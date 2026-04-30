"""End-to-end test for orchestrator.job_runner.run_job.

Drives the standalone-engine entry point against a dummy workflow so the
test exercises both ``_build_ci_environment`` and ``Runner.run`` (with the
full pre/post-run pipeline). Runs in local-fs S3 mode (PRAKTIKA_LOCAL_RUN=1)
so no real S3 calls happen.

The dummy workflow is normally hidden by ``Settings.DISABLED_WORKFLOWS`` so
live yaml/run paths skip it; setUp temporarily un-disables it for the test.
``Shell.check`` is patched so the destructive ``git clean -ffd`` issued by
``Runner._pre_run`` against a dirty working tree is a no-op.
"""
import os
import shutil
import unittest
from pathlib import Path


class TestRunner(unittest.TestCase):
    def setUp(self):
        from praktika.settings import Settings
        from praktika.utils import Shell

        # Make the dummy workflow visible to _get_workflows() during the test
        # without leaving it discoverable in normal runs.
        self._orig_disabled = Settings.DISABLED_WORKFLOWS
        Settings.DISABLED_WORKFLOWS = [
            f for f in (Settings.DISABLED_WORKFLOWS or [])
            if "_dummy_test_workflow" not in f
        ]

        # Stash and patch Shell.check so the test never invokes
        # `git clean -ffd` against the developer's working tree.
        self._orig_shell_check = Shell.check

        def _safe_check(command, *args, **kwargs):
            if "git clean" in command:
                print(f"TEST: skipping destructive command [{command}]")
                return True
            return self._orig_shell_check(command, *args, **kwargs)

        Shell.check = staticmethod(_safe_check)

        # Start from a clean tmp dir so prior runs don't leak state.
        for path in (Settings.TEMP_DIR, "./ci/tmp/s3_local"):
            if Path(path).is_dir():
                shutil.rmtree(path)

    def tearDown(self):
        from praktika.settings import Settings
        from praktika.utils import Shell

        Settings.DISABLED_WORKFLOWS = self._orig_disabled
        Shell.check = self._orig_shell_check
        os.environ.pop("PRAKTIKA_LOCAL_RUN", None)

    def test_run_job_full_pipeline_local(self):
        from praktika.mangle import _get_workflows
        from praktika.orchestrator.job_runner import (
            _build_ci_environment,
            run_job,
        )
        from praktika.result import Result, _ResultS3
        from praktika.utils import Utils

        task = {
            "workflow_name": "DummyRunnerTest",
            "job_name": "dummy",
            # Push-event shape: branch + sha, no pr_number.
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }

        # In real CI the Config Workflow job creates the initial workflow
        # result on S3 via push_pending_ci_report before any other job runs;
        # mimic that minimally so the html hook's update_workflow_results
        # call has something to read.
        os.environ["PRAKTIKA_LOCAL_RUN"] = "1"
        _build_ci_environment(task, job_name=task["job_name"], local_run=True)
        workflow = _get_workflows(name=task["workflow_name"])[0]
        sub_results = [
            Result.create_new(j.name, Result.Status.PENDING)
            for j in workflow.jobs
        ]
        summary = Result.create_new(
            workflow.name, Result.Status.RUNNING, results=sub_results
        )
        summary.start_time = Utils.timestamp()
        summary.dump()
        _ResultS3.copy_result_to_s3_with_version(summary, version=0)

        rc = run_job(task, gh_token=None, local=True)
        self.assertEqual(rc, 0, f"run_job returned non-zero exit code [{rc}]")

        result = Result.from_fs("dummy")
        self.assertEqual(
            result.status,
            Result.Status.OK,
            f"Expected OK, got [{result.status}], info: [{result.info}]",
        )
        # After post-run hooks upload result.files to (local-fs) S3,
        # result.files is cleared and uploaded URLs land in result.links.
        self.assertEqual(
            result.files,
            [],
            f"Expected files cleared after upload, got: {result.files}",
        )
        # A successful upload writes to the local-fs S3 mirror under
        # TEMP_DIR/s3_local/...; a failed upload falls back to a bare
        # file:// pointing at the original (often non-existent) path.
        self.assertTrue(
            any("s3_local" in link and "job.log" in link for link in result.links),
            f"Expected job.log to be uploaded to local-fs S3, got: {result.links}",
        )


if __name__ == "__main__":
    unittest.main()
