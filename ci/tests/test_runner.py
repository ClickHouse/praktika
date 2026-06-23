"""End-to-end tests for orchestrator.job_runner.run_job.

Drives the standalone-engine entry point against a feature-rich dummy
workflow so the tests exercise both ``_build_ci_environment`` and
``Runner.run`` (with the full pre/post-run pipeline). Runs in local-fs
S3 mode (``PRAKTIKA_LOCAL_RUN=1``) so no real S3 calls happen.

The dummy workflow is gated behind ``PRAKTIKA_TEST_ACTIVE`` so the
live paths — and the ``native_jobs`` subprocess Config Workflow spawns
— only see it when the test sets the env var. The same env var flips
``ci/settings/_test_overrides.py`` into a mock-Settings mode that
redirects ``Settings.TEMP_DIR`` to ``./ci/tmp/_test_runner`` (so test
state never collides with the outer praktika job's ``./ci/tmp``) and
points ``Settings.SECRET_CI_DB_*`` at non-existent dummy secrets.
"""
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace


_TEST_TEMP_DIR = "./ci/tmp/_test_runner"
_DUMMY_DB_CONNECTION = "DUMMY_TEST_CI_DB_CONNECTION_NONEXISTENT"


def test_runner_commit_status_posting_is_only_for_non_praktika_engines():
    from praktika.runner import _should_post_commit_status
    from praktika.workflow import Workflow

    assert _should_post_commit_status(
        SimpleNamespace(engine=Workflow.Engine.GH_ACTIONS)
    )
    assert not _should_post_commit_status(
        SimpleNamespace(engine=Workflow.Engine.PRAKTIKA)
    )
    assert _should_post_commit_status(SimpleNamespace(engine="custom-engine"))


def test_gh_auth_uses_custom_auth_outside_github_actions(monkeypatch):
    from praktika import runner
    from praktika.gh_auth import GHAuth
    from praktika.settings import Settings

    calls = []

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(Settings, "USE_CUSTOM_GH_AUTH", True)
    monkeypatch.setattr(runner, "_GH_authenticated", False)
    monkeypatch.setattr(
        GHAuth,
        "auth_from_settings",
        classmethod(lambda cls: calls.append("auth")),
    )

    assert runner._GH_Auth() is True
    assert calls == ["auth"]


def test_gh_auth_skips_outside_github_actions_without_custom_auth(monkeypatch):
    from praktika import runner
    from praktika.settings import Settings

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(Settings, "USE_CUSTOM_GH_AUTH", False)
    monkeypatch.setattr(runner, "_GH_authenticated", False)

    assert runner._GH_Auth() is False


class TestRunner(unittest.TestCase):
    def setUp(self):
        from praktika.settings import Settings

        # Subprocesses spawned by Runner.run inherit these and run
        # ``ci/settings/_test_overrides.py`` on praktika.settings import.
        # The parent test process imported praktika.settings at unittest
        # discovery time (before this setUp), so the override file's
        # ``if`` branch was False then; mirror its mutations manually.
        os.environ["PRAKTIKA_TEST_ACTIVE"] = "1"
        os.environ["PRAKTIKA_LOCAL_RUN"] = "1"
        Settings.TEMP_DIR = _TEST_TEMP_DIR
        Settings.OUTPUT_DIR = _TEST_TEMP_DIR
        Settings.INPUT_DIR = _TEST_TEMP_DIR
        Settings.SECRET_CI_DB_CONNECTION = _DUMMY_DB_CONNECTION

        # Start from a clean tmp dir so prior runs don't leak state.
        # Settings.TEMP_DIR is now the test-only override, so this
        # cannot touch the outer praktika job's ./ci/tmp.
        if Path(Settings.TEMP_DIR).is_dir():
            shutil.rmtree(Settings.TEMP_DIR)

    def tearDown(self):
        os.environ.pop("PRAKTIKA_TEST_ACTIVE", None)
        os.environ.pop("PRAKTIKA_LOCAL_RUN", None)

    def _bootstrap_workflow_state(self, task):
        """Mimic what Config Workflow's push_pending_ci_report would do
        on a real run: build the env, build a workflow-level Result,
        and dump it to local-fs S3 so subsequent jobs' html hook can
        update it."""
        from praktika.mangle import _get_workflows
        from praktika.orchestrator.job_runner import _build_ci_environment
        from praktika.result import Result, _ResultS3
        from praktika.utils import Utils

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
        return workflow

    def test_run_job_full_pipeline_local(self):
        from praktika.orchestrator.job_runner import run_job
        from praktika.result import Result

        task = {
            "workflow_name": "DummyRunnerTest",
            "job_name": "dummy",
            # PR-event shape: pr_number + base_ref + sha.
            "pr_number": 1,
            "base_ref": "main",
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)
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
        # The S3 key includes both the normalized workflow and job names
        # so artifacts from concurrent workflows don't collide.
        self.assertTrue(
            any(
                "s3_local" in link
                and "/dummyrunnertest/dummy/job.log" in link
                for link in result.links
            ),
            f"Expected job.log under <workflow>/<job>/, got: {result.links}",
        )

    def test_exit_code_result_synthesizes_ok_on_zero_exit(self):
        """enable_exit_code_result=True + script that exits 0 without
        dumping a Result -> synthesized OK Result, run_job rc=0."""
        from praktika.orchestrator.job_runner import run_job
        from praktika.result import Result

        task = {
            "workflow_name": "DummyExitCodeResultTest",
            "job_name": "exit_ok",
            "pr_number": 1,
            "base_ref": "main",
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)
        rc = run_job(task, gh_token=None, local=True)
        self.assertEqual(rc, 0, f"run_job returned non-zero rc [{rc}]")

        result = Result.from_fs("exit_ok")
        self.assertEqual(result.status, Result.Status.OK)
        # Duration should reflect actual job runtime, not zero — pre_run
        # set start_time and update_duration computes now - start_time.
        self.assertIsNotNone(result.duration)
        self.assertGreater(result.duration, 0)

    def test_exit_code_result_synthesizes_fail_on_nonzero_exit(self):
        """enable_exit_code_result=True + script that exits non-zero
        without dumping a Result -> synthesized FAIL Result with the
        exit code embedded in info, run_job rc!=0."""
        from praktika.orchestrator.job_runner import run_job
        from praktika.result import Result

        task = {
            "workflow_name": "DummyExitCodeResultTest",
            "job_name": "exit_fail",
            "pr_number": 1,
            "base_ref": "main",
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)
        rc = run_job(task, gh_token=None, local=True)
        self.assertNotEqual(rc, 0, "non-zero exit must propagate to run_job rc")

        result = Result.from_fs("exit_fail")
        self.assertEqual(result.status, Result.Status.FAIL)
        self.assertIn("exited with code [7]", result.info)
        self.assertIsNotNone(result.duration)
        self.assertGreater(result.duration, 0)

    def test_config_workflow_failure_is_handled_gracefully(self):
        """Reproduce the misconfigured-runner failure: Config Workflow's
        ``_check_db`` fetches the CI DB connection secret via ``get_value()``,
        which raises RuntimeError when the env var isn't set. The check
        must catch the raise locally, surface it as a FAIL/ERROR sub-result
        with diagnostic info, and let the rest of Config Workflow run —
        not let the exception unwind into the script's main try/except,
        which would discard every accumulated sub-result.

        ``_check_db`` is gated by ``not Info().is_local_run``, so we run
        with ``local=False`` (env.LOCAL_RUN=False) to actually exercise
        it; ``PRAKTIKA_LOCAL_RUN=1`` (already set in setUp) keeps the S3
        backend on the local-fs mirror.
        """
        from praktika.orchestrator.job_runner import run_job
        from praktika.result import Result
        from praktika.settings import Settings

        task = {
            "workflow_name": "DummyRunnerTest",
            "job_name": Settings.CI_CONFIG_JOB_NAME,
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)

        try:
            # local=False — env.LOCAL_RUN=False → _check_db runs.
            rc = run_job(task, gh_token=None, local=False)
        except Exception as e:
            self.fail(
                f"run_job leaked exception (job must dump ERROR Result instead): "
                f"{type(e).__name__}: {e}"
            )

        self.assertNotEqual(
            rc, 0, "Config Workflow should fail when CI DB connection env var is missing"
        )
        result = Result.from_fs(Settings.CI_CONFIG_JOB_NAME)
        self.assertEqual(
            result.status,
            Result.Status.ERROR,
            f"Expected ERROR after secret-resolution failure, got [{result.status}]",
        )
        # The "Check CI DB" sub-result must record the captured failure;
        # if it's missing, _check_db raised through its caller and lost
        # the rest of the workflow's progress.
        check_db_results = [r for r in result.results if r.name == "Check CI DB"]
        self.assertEqual(
            len(check_db_results),
            1,
            f"Expected one [Check CI DB] sub-result, got: {[r.name for r in result.results]}",
        )
        check_db = check_db_results[0]
        self.assertEqual(check_db.status, Result.Status.ERROR)
        self.assertIn("Failed to check CI DB", check_db.info)
        # Full traceback should be captured so the report is actionable
        # without grepping the raw job log.
        self.assertIn("Traceback", check_db.info)
        self.assertIn("RuntimeError", check_db.info)


if __name__ == "__main__":
    unittest.main()
