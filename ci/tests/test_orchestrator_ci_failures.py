from types import SimpleNamespace

import pytest

from praktika.orchestrator import INFRA_EXIT_CODE, _orchestrate_single, orchestrate


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


class _FakeCheck:
    """Records check-run updates/completion instead of hitting the GH API."""

    def __init__(self):
        self.id = 4242
        self.updates = []
        self.completed = None

    def update(self, output=None, details_url=None):
        self.updates.append(output)

    def complete(self, conclusion, output=None, details_url=None):
        self.completed = (conclusion, output)


class _FakeState:
    cancelled = False

    def __init__(self, *a, **k):
        pass

    def print_plan(self):
        pass

    def print_summary(self):
        pass

    def cleanup(self):
        pass

    def not_finished(self):
        return False

    def get_ready(self):
        return []

    def wait(self):
        pass

    def md_status_summary(self):
        return "all done"

    def md_status(self):
        return "table"


_EVENT = {
    "repo": "ClickHouse/praktika",
    "head_sha": "deadbeef",
    "head_ref": "test",
    "pr_number": 1,
}


def _patch_common(monkeypatch, check, attempts=3):
    monkeypatch.setattr(
        "praktika.orchestrator.check_run.CheckRun.start", lambda *a, **k: check
    )
    # AI off so maybe_create returns None and isn't the thing under test.
    monkeypatch.setattr(
        "praktika.settings.Settings.AI_ORCHESTRATION_ENABLED", False, raising=False
    )
    monkeypatch.setattr(
        "praktika.settings.Settings.MAX_RETRIES_ORCHESTRATOR", attempts, raising=False
    )
    monkeypatch.setattr("praktika.orchestrator.time.sleep", lambda *_: None)


def test_startup_crash_finalizes_check_with_phase_tagged_error(monkeypatch):
    # A crash during startup (here: plan build) must finalize the check as
    # failure with an explicit, phase-tagged message — never leave it stuck
    # in_progress (PR #130 regression).
    workflow = SimpleNamespace(name="Demo Workflow", enable_report=False, jobs=[1, 2])
    check = _FakeCheck()
    _patch_common(monkeypatch, check, attempts=3)

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("infra blew up")

    monkeypatch.setattr("praktika.orchestrator.state.WorkflowState", boom)

    rc = _orchestrate_single(workflow, _EVENT, gh_token="token", local_mode=False)

    # Startup crash (DAG never ran) → infra exit code so the controller retries
    # on a fresh instance.
    assert rc == INFRA_EXIT_CODE
    assert calls["n"] == 3  # retried MAX_RETRIES_ORCHESTRATOR times before giving up
    assert check.completed is not None
    conclusion, output = check.completed
    assert conclusion == "failure"
    assert "infra blew up" in output["text"]
    assert "planning" in output["summary"]  # phase surfaced in the failure summary


def test_running_phase_crash_is_not_infra(monkeypatch):
    # A crash after the DAG started (jobs already dispatched) is an ordinary
    # failure (rc=1), NOT infra — the controller must not retry/terminate.
    workflow = SimpleNamespace(name="Demo Workflow", enable_report=False, jobs=[1])
    check = _FakeCheck()
    _patch_common(monkeypatch, check, attempts=3)

    class _CrashingState(_FakeState):
        def not_finished(self):
            return True

        def wait(self):
            raise RuntimeError("loop blew up mid-run")

    monkeypatch.setattr(
        "praktika.orchestrator.state.WorkflowState", lambda *a, **k: _CrashingState()
    )

    rc = _orchestrate_single(workflow, _EVENT, gh_token="token", local_mode=False)

    assert rc == 1  # not INFRA_EXIT_CODE
    conclusion, _ = check.completed
    assert conclusion == "failure"


def test_attempt_label_surfaced_on_check(monkeypatch):
    monkeypatch.setenv("PRAKTIKA_ATTEMPT", "2/3")
    workflow = SimpleNamespace(name="Demo Workflow", enable_report=False, jobs=[1])
    check = _FakeCheck()
    _patch_common(monkeypatch, check, attempts=1)
    monkeypatch.setattr(
        "praktika.orchestrator.state.WorkflowState", lambda *a, **k: _FakeState()
    )

    _orchestrate_single(workflow, _EVENT, gh_token="token", local_mode=False)

    # Some in-progress PATCH carried the attempt label.
    assert any("2/3" in (u or {}).get("summary", "") for u in check.updates)


def test_startup_retry_succeeds_on_second_attempt(monkeypatch):
    workflow = SimpleNamespace(name="Demo Workflow", enable_report=False, jobs=[1])
    check = _FakeCheck()
    _patch_common(monkeypatch, check, attempts=3)

    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient s3 hiccup")
        return _FakeState()

    monkeypatch.setattr("praktika.orchestrator.state.WorkflowState", flaky)

    rc = _orchestrate_single(workflow, _EVENT, gh_token="token", local_mode=False)

    assert rc == 0
    assert calls["n"] == 2  # failed once, then succeeded
    assert check.completed is not None
    conclusion, _ = check.completed
    assert conclusion == "neutral"
