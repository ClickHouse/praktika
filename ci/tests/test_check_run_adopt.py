"""Unit tests for adopting a pre-clone bootstrap check run.

The controller opens a check run before cloning (so the PR shows CI
immediately); the orchestrator then adopts that check-run id and renames it to
the matched workflow instead of opening a fresh one.
"""

import praktika.orchestrator as orch
import praktika.orchestrator.check_run as check_run_mod
from praktika.orchestrator.check_run import CheckRun


def test_retitle_patches_name_status_actions_and_url(monkeypatch):
    calls = []

    def fake_api(method, url, token, json_body=None):
        calls.append((method, url, json_body))
        return {}

    monkeypatch.setattr(CheckRun, "_api", staticmethod(fake_api))

    check = CheckRun("tok", "o/r", 777, "CI")
    result = check.retitle("Pull Request CI", details_url="http://report")

    assert result is check
    assert check.name == "Pull Request CI"
    method, url, body = calls[0]
    assert method == "PATCH"
    assert url.endswith("/repos/o/r/check-runs/777")
    assert body["name"] == "Pull Request CI"
    assert body["status"] == "in_progress"
    assert body["details_url"] == "http://report"
    assert body["actions"][0]["identifier"] == "cancel"


def test_orchestrate_completes_bootstrap_check_neutral_when_no_workflows(monkeypatch):
    completed = []

    class FakeCheck:
        def __init__(self, token, repo, id, name):
            self.id = id
            self.name = name

        def complete(self, conclusion, output=None):
            completed.append(conclusion)

    monkeypatch.setattr(check_run_mod, "CheckRun", FakeCheck)
    monkeypatch.setattr(orch, "find_workflows_for_event", lambda *a, **k: [])

    rc = orch.orchestrate(
        {"type": "pull_request", "repo": "o/r", "head_sha": "s"},
        gh_token="tok",
        ci=True,
        bootstrap_check_id=123,
    )

    assert rc == 0
    # The bootstrap check is adopted and closed as neutral, not left dangling.
    assert completed == ["neutral"]
