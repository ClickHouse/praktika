from types import SimpleNamespace

import pytest

from praktika.orchestrator import ai
from praktika.orchestrator.ai import Advisor, build_observation
from praktika.orchestrator.ai.mock import MockProvider
from praktika.orchestrator.ai.provider import Observation, resolve


def _job(name, status, started_at=None, finished_at=None, filter_reason=None):
    return SimpleNamespace(
        name=name,
        status=SimpleNamespace(value=status),
        started_at=started_at,
        finished_at=finished_at,
        filter_reason=filter_reason,
    )


def _state(jobs, summary="summary"):
    return SimpleNamespace(
        jobs={j.name: j for j in jobs},
        md_status_summary=lambda: summary,
    )


EVENT = {
    "type": "pull_request",
    "action": "synchronize",
    "pr_number": 7,
    "head_sha": "deadbeef",
    "head_ref": "feature",
}


# --------------------------------------------------------------- registry


def test_registry_resolves_mock():
    assert resolve("mock") is MockProvider


def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        resolve("does-not-exist")


# --------------------------------------------------------------- factory


def test_maybe_create_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", False, raising=False)
    assert Advisor.maybe_create(run_id="r1") is None


def test_maybe_create_enabled_returns_advisor(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)
    assert isinstance(advisor, Advisor)
    assert isinstance(advisor._provider, MockProvider)


# --------------------------------------------------------------- mock provider


def test_mock_provider_does_nothing():
    obs = Observation(event=EVENT, jobs=[], changed=[], summary="")
    turn = MockProvider(model="m").decide(obs)
    assert turn.decision == []
    assert turn.error is None
    assert turn.usage.input_tokens == 0
    assert turn.usage.output_tokens == 0
    assert turn.usage.cost_usd == 0.0
    assert turn.usage.provider == "mock"


# --------------------------------------------------------------- observation


def test_build_observation_snapshot():
    state = _state(
        [
            _job("A", "success", started_at=100.0, finished_at=105.0),
            _job("B", "running"),
        ]
    )
    obs = build_observation(state, EVENT, changed=[{"name": "A", "status": "success"}])
    assert obs.event["pr_number"] == 7
    assert obs.summary == "summary"
    by_name = {j["name"]: j for j in obs.jobs}
    assert by_name["A"]["duration_s"] == 5.0
    assert "duration_s" not in by_name["B"]


# --------------------------------------------------------------- advisor flow


def test_turn_fires_only_on_new_terminal(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    a = _job("A", "running")
    b = _job("B", "pending")
    state = _state([a, b])

    # Nothing terminal yet -> no turn.
    assert advisor.on_workflow_update(state, EVENT) is None

    # A finishes -> exactly one turn, recording the change.
    a.status = SimpleNamespace(value="success")
    turn = advisor.on_workflow_update(state, EVENT)
    assert turn is not None
    assert advisor._ledger.turns == 1

    # No further change -> no new turn.
    assert advisor.on_workflow_update(state, EVENT) is None
    assert advisor._ledger.turns == 1

    # B finishes -> second turn.
    b.status = SimpleNamespace(value="failure")
    assert advisor.on_workflow_update(state, EVENT) is not None
    assert advisor._ledger.turns == 2


def test_advisory_only_no_mutation_and_zero_cost(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    a = _job("A", "success", started_at=1.0, finished_at=2.0)
    state = _state([a])
    turn = advisor.on_workflow_update(state, EVENT)

    # Mock makes no decision and the job state is untouched.
    assert turn.decision == []
    assert a.status.value == "success"
    assert advisor._ledger.cost_usd == 0.0


def test_provider_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(ai.Settings, "AI_ORCHESTRATION_ENABLED", True, raising=False)
    monkeypatch.setattr(ai.Settings, "AI_PROVIDER", "mock", raising=False)
    advisor = Advisor.maybe_create(run_id="r1", local_mode=True)

    def boom(observation):
        raise RuntimeError("provider exploded")

    advisor._provider.decide = boom

    a = _job("A", "success")
    turn = advisor.on_workflow_update(_state([a]), EVENT)
    assert turn.error is not None
    assert "provider exploded" in turn.error
