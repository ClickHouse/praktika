from types import SimpleNamespace

import pytest

from praktika.orchestrator import _orchestrate_single, orchestrate


def test_ci_mode_fails_when_gh_token_cannot_be_minted(monkeypatch):
    class BrokenProvider:
        def get(self):
            raise RuntimeError("mint failed")

    monkeypatch.setattr("praktika.gh_auth.GHTokenProvider", BrokenProvider)

    event = {
        "type": "pull_request",
        "action": "synchronize",
        "repo": "ClickHouse/praktika",
        "head_sha": "deadbeef",
        "head_ref": "test",
        "base_ref": "main",
        "pr_number": 1,
        "sender": "tester",
    }

    with pytest.raises(RuntimeError, match="Failed to mint GH token"):
        orchestrate(event, ci=True)


def test_ci_mode_fails_when_initial_check_run_cannot_be_created(monkeypatch):
    workflow = SimpleNamespace(
        name="Demo Workflow",
        enable_report=False,
        jobs=[],
    )

    def fail_start(*args, **kwargs):
        raise RuntimeError("check api down")

    monkeypatch.setattr("praktika.orchestrator.check_run.CheckRun.start", fail_start)

    event = {
        "repo": "ClickHouse/praktika",
        "head_sha": "deadbeef",
        "head_ref": "test",
        "pr_number": 1,
    }

    with pytest.raises(RuntimeError, match="Failed to open initial check run"):
        _orchestrate_single(workflow, event, gh_token="token", local_mode=False)
