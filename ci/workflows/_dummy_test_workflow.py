"""Workflow used only by ci/tests/test_runner.py.

Gated behind ``PRAKTIKA_DUMMY_TEST_ACTIVE`` so that live yaml/run paths
and the ``native_jobs`` subprocess only see this workflow when the test
sets the env var. The enabled features mirror ``praktika_pr_advanced.py``
so the test exercises the broad Config Workflow code paths (secrets,
cache, cidb summary, merge-ready status). The cidb secret names match
the dummy values that ``ci/settings/_test_overrides.py`` writes into
``Settings.SECRET_CI_DB_*`` so ``_check_db`` looks the dummy secrets up
through ``workflow.get_secret`` and then fails at ``get_value`` time
(no env var with the dummy name is ever set), reproducing the
misconfigured-runner failure regardless of the developer's shell.
"""
import os

from praktika import Job, Secret, Workflow


_DUMMY_DB_URL = "DUMMY_TEST_CI_DB_URL_NONEXISTENT"
_DUMMY_DB_USER = "DUMMY_TEST_CI_DB_USER_NONEXISTENT"
_DUMMY_DB_PASSWORD = "DUMMY_TEST_CI_DB_PASSWORD_NONEXISTENT"

WORKFLOWS = []

if os.environ.get("PRAKTIKA_DUMMY_TEST_ACTIVE") == "1":
    WORKFLOWS = [
        Workflow.Config(
            name="DummyRunnerTest",
            event=Workflow.Event.PULL_REQUEST,
            base_branches=["main"],
            jobs=[
                Job.Config(
                    name="dummy",
                    runs_on=["test-runner"],
                    command="python3 ci/tests/_dummy_job_script.py",
                ),
            ],
            secrets=[
                Secret.Config(name=_DUMMY_DB_URL, type=Secret.Type.GH_SECRET),
                Secret.Config(name=_DUMMY_DB_USER, type=Secret.Type.GH_SECRET),
                Secret.Config(name=_DUMMY_DB_PASSWORD, type=Secret.Type.GH_SECRET),
            ],
            enable_report=True,
            enable_cache=True,
            enable_cidb=True,
            enable_merge_ready_status=True,
            enable_gh_summary_comment=True,
        ),
    ]
