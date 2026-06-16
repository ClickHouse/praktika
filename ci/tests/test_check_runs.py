"""Live smoke tests for posting GitHub check runs via the woolenwolf App.

Each test creates *real* check runs on a real commit. Gated behind
``CI_ENGINE_LIVE_TESTS=1`` so a plain ``pytest ci/praktika/orchestrator/``
during normal development doesn't spam the PR with
``ci-engine-test / <ms-timestamp>`` checks.

Run explicitly with:
    CI_ENGINE_LIVE_TESTS=1 AWS_DEFAULT_REGION=us-east-1 \
        python -m pytest -s ci/praktika/orchestrator/test_check_runs.py

Overrides:
    TEST_REPO=ClickHouse/clickhouse-private
    TEST_HEAD_SHA=<sha>     # defaults to `git rev-parse HEAD` in CWD
"""

import os
import subprocess
import time

import pytest
import requests

from praktika.orchestrator.check_run import CheckRun
from praktika_controller.common import get_github_token

pytestmark = pytest.mark.skipif(
    os.environ.get("CI_ENGINE_LIVE_TESTS") != "1",
    reason="live GitHub smoke tests; opt in with CI_ENGINE_LIVE_TESTS=1",
)

API_ROOT = "https://api.github.com"


@pytest.fixture(scope="session")
def gh_token():
    return get_github_token()


@pytest.fixture(scope="session")
def target():
    repo = os.environ.get("TEST_REPO", "ClickHouse/clickhouse-private")
    sha = os.environ.get("TEST_HEAD_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, sha


@pytest.fixture
def check_name():
    return f"ci-engine-test / {int(time.time() * 1000)}"


def _post(token, repo, body):
    r = requests.post(
        f"{API_ROOT}/repos/{repo}/check-runs",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _patch(token, repo, check_id, body):
    r = requests.patch(
        f"{API_ROOT}/repos/{repo}/check-runs/{check_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def test_lifecycle_in_progress_then_success(gh_token, target, check_name):
    repo, sha = target
    run = CheckRun.start(gh_token, repo, sha, check_name)
    assert run.id > 0

    data = _patch(
        gh_token,
        repo,
        run.id,
        {
            "status": "completed",
            "conclusion": "success",
            "output": {
                "title": "All green",
                "summary": "Build + unit tests passed",
                "text": "```\n[ok] 42 tests, 0 failures\n```",
            },
        },
    )
    assert data["status"] == "completed"
    assert data["conclusion"] == "success"
    print("success:", data["html_url"])


def test_failure_with_inline_log(gh_token, target, check_name):
    repo, sha = target
    data = _post(
        gh_token,
        repo,
        {
            "name": check_name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://example.com/logs/demo",
            "output": {
                "title": "Build failed",
                "summary": "Compilation error in src/foo.cpp",
                "text": (
                    "### Tail of build.log\n\n"
                    "```\n"
                    "[12:00:01] Starting build...\n"
                    "[12:00:42] src/foo.cpp:17:5: error: use of undeclared identifier 'bar'\n"
                    "[12:00:42] 1 error generated.\n"
                    "[12:00:42] ninja: build stopped.\n"
                    "```\n"
                ),
            },
        },
    )
    assert data["conclusion"] == "failure"
    assert data["output"]["title"] == "Build failed"
    print("failure:", data["html_url"])


def test_neutral_with_annotations(gh_token, target, check_name):
    repo, sha = target
    data = _post(
        gh_token,
        repo,
        {
            "name": check_name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "neutral",
            "output": {
                "title": "Lint: 2 warnings",
                "summary": "clang-tidy reported 2 issues",
                "annotations": [
                    {
                        "path": "src/Parsers/obfuscateQueries.cpp",
                        "start_line": 1,
                        "end_line": 1,
                        "annotation_level": "warning",
                        "message": "Demo annotation (no real issue).",
                    },
                    {
                        "path": "bootstrap/src/praktika_controller/controller.py",
                        "start_line": 38,
                        "end_line": 38,
                        "annotation_level": "notice",
                        "message": "Demo notice on bootstrap token minting logic.",
                    },
                ],
            },
        },
    )
    assert data["conclusion"] == "neutral"
    print("neutral:", data["html_url"])


def test_skipped(gh_token, target, check_name):
    repo, sha = target
    data = _post(
        gh_token,
        repo,
        {
            "name": check_name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "skipped",
            "output": {"title": "Skipped", "summary": "No relevant files changed."},
        },
    )
    assert data["conclusion"] == "skipped"
    print("skipped:", data["html_url"])


def test_update_details_url(gh_token, target, check_name):
    repo, sha = target
    run = CheckRun.start(gh_token, repo, sha, check_name)

    mid = _patch(
        gh_token,
        repo,
        run.id,
        {"details_url": "https://example.com/logs/early", "output": {
            "title": "Running", "summary": "started",
        }},
    )
    assert mid["details_url"] == "https://example.com/logs/early"

    final = _patch(
        gh_token,
        repo,
        run.id,
        {
            "status": "completed",
            "conclusion": "cancelled",
            "details_url": "https://example.com/logs/final",
            "output": {"title": "Cancelled", "summary": "user aborted"},
        },
    )
    assert final["conclusion"] == "cancelled"
    assert final["details_url"] == "https://example.com/logs/final"
    print("cancelled:", final["html_url"])
