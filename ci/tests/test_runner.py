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


def test_job_python_env_prefers_runtime_paths_before_repo_paths(monkeypatch, tmp_path):
    from praktika.runner import _job_python_env

    monkeypatch.chdir(tmp_path)
    other_path = tmp_path / "other"
    other_path.mkdir()
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join([".", str(tmp_path), str(other_path)]),
    )

    env = _job_python_env()
    pythonpath = env["PYTHONPATH"].split(os.pathsep)

    assert env["PYTHONSAFEPATH"] == "1"
    assert "." in pythonpath
    # "./ci" must NOT be on the path: it would let a bare `import praktika`
    # resolve to the repo's vendored ci/praktika instead of the installed one.
    assert "./ci" not in pythonpath
    assert str(other_path) in pythonpath
    assert str(tmp_path) in pythonpath

    site_paths = [
        entry
        for entry in pythonpath[: pythonpath.index(".")]
        if "site-packages" in entry or "dist-packages" in entry
    ]
    assert site_paths


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
            # Required by _build_ci_environment: event_type is never defaulted,
            # and a pull_request task must carry head_repo (internal PR -> == repo).
            "event_type": "pull_request",
            "head_repo": "test-org/test-repo",
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

    def test_runner_crash_is_recorded_in_job_result(self):
        """A crash inside Runner.run (e.g. _post_run) must land an ERROR
        Result with the traceback, so the orchestrator's check-run / report
        shows the failure instead of a bare, info-less error."""
        from unittest import mock

        from praktika.orchestrator import job_runner
        from praktika.result import Result

        task = {
            "workflow_name": "DummyRunnerTest",
            "job_name": "dummy",
            "pr_number": 1,
            # Required by _build_ci_environment: event_type is never defaulted,
            # and a pull_request task must carry head_repo (internal PR -> == repo).
            "event_type": "pull_request",
            "head_repo": "test-org/test-repo",
            "base_ref": "main",
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)

        def boom(*_a, **_k):
            raise FileNotFoundError(2, "No such file or directory", "")

        with mock.patch.object(job_runner.Runner, "run", boom):
            rc = job_runner.run_job(task, gh_token=None, local=True)

        self.assertEqual(rc, 1)
        result = Result.from_fs("dummy")
        self.assertEqual(result.status, Result.Status.ERROR)
        self.assertIn("Runner crashed", result.info)

    def test_exit_code_result_synthesizes_ok_on_zero_exit(self):
        """enable_exit_code_result=True + script that exits 0 without
        dumping a Result -> synthesized OK Result, run_job rc=0."""
        from praktika.orchestrator.job_runner import run_job
        from praktika.result import Result

        task = {
            "workflow_name": "DummyExitCodeResultTest",
            "job_name": "exit_ok",
            "pr_number": 1,
            # Required by _build_ci_environment: event_type is never defaulted,
            # and a pull_request task must carry head_repo (internal PR -> == repo).
            "event_type": "pull_request",
            "head_repo": "test-org/test-repo",
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

    def test_non_docker_job_does_not_mutate_parent_pythonpath(self):
        from praktika.orchestrator.job_runner import run_job

        os.environ["PYTHONPATH"] = "/existing"
        task = {
            "workflow_name": "DummyExitCodeResultTest",
            "job_name": "exit_ok",
            "pr_number": 1,
            # Required by _build_ci_environment: event_type is never defaulted,
            # and a pull_request task must carry head_repo (internal PR -> == repo).
            "event_type": "pull_request",
            "head_repo": "test-org/test-repo",
            "base_ref": "main",
            "head_ref": "test-branch",
            "head_sha": "0" * 40,
            "repo": "test-org/test-repo",
        }
        self._bootstrap_workflow_state(task)

        try:
            rc = run_job(task, gh_token=None, local=True)
            self.assertEqual(rc, 0, f"run_job returned non-zero rc [{rc}]")
            self.assertEqual(os.environ["PYTHONPATH"], "/existing")
        finally:
            os.environ.pop("PYTHONPATH", None)

    def test_docker_job_mounts_installed_package_dir(self):
        Path(_TEST_TEMP_DIR).mkdir(parents=True, exist_ok=True)

        import sys
        import types
        from unittest import mock

        with mock.patch.dict(sys.modules, {"requests": types.ModuleType("requests")}):
            from praktika import runner

            captured = {}

            class DummyTeePopen:
                def __init__(self, command, **kwargs):
                    captured["command"] = command
                    self.timeout_exceeded = False

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    return False

                def wait(self):
                    return 0

                def get_latest_log(self, max_lines=20):
                    return ""

            staged_package_dir = Path(runner._staged_praktika_package_dir())
            staged_praktika_package = staged_package_dir / "praktika"
            job = SimpleNamespace(
                name="docker-job",
                run_in_docker="example/image:latest",
                timeout=1,
                timeout_shell_cleanup=None,
                enable_gh_auth=False,
                command="echo ok",
            )
            workflow = SimpleNamespace(name="workflow")

            with mock.patch.object(runner, "TeePopen", DummyTeePopen), mock.patch.object(
                runner.Shell, "check", lambda *args, **kwargs: False
            ), mock.patch.object(
                runner.Shell, "run", lambda *args, **kwargs: None
            ), mock.patch.object(
                runner.Result,
                "from_fs",
                staticmethod(
                    lambda *args, **kwargs: SimpleNamespace(
                        is_completed=lambda: True,
                        is_running=lambda: False,
                        is_error=lambda: False,
                        dump=lambda: None,
                    )
                ),
            ), mock.patch.object(
                runner._Environment,
                "get",
                staticmethod(
                    lambda: SimpleNamespace(
                        WORKFLOW_CONFIG=None,
                        dump=lambda: None,
                    )
                ),
            ):
                rc = runner.Runner()._run(workflow=workflow, job=job, no_docker=False)

            self.assertEqual(rc, 0)
            self.assertTrue(staged_package_dir.is_dir())
            self.assertTrue(staged_praktika_package.is_dir())
            self.assertIn(
                f"--volume {staged_package_dir}:{staged_package_dir}",
                captured["command"],
            )
            # Staged Praktika dir first (so `import praktika` resolves to the
            # installed copy, not the repo's vendored ci/praktika), then the
            # checkout root so the repo's own `ci.*` modules are importable.
            self.assertIn(
                f"-e PYTHONPATH={staged_package_dir}:{os.getcwd()}",
                captured["command"],
            )
            # Never the bare relative "." form.
            self.assertNotIn("PYTHONPATH=.", captured["command"])

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
            # Required by _build_ci_environment: event_type is never defaulted,
            # and a pull_request task must carry head_repo (internal PR -> == repo).
            "event_type": "pull_request",
            "head_repo": "test-org/test-repo",
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
            # Non-PR (Config Workflow) run: push event, no head_repo needed.
            "event_type": "push",
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
