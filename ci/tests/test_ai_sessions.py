from types import SimpleNamespace

import pytest

from praktika.orchestrator.ai.provider import Observation, Turn, Usage
from praktika.orchestrator.ai.session import SessionManager
from praktika.orchestrator.ai.store import LocalSessionStore


def _store(tmp_path):
    return LocalSessionStore(str(tmp_path))


def _turn(cost=0.0, decision=None, provider="mock"):
    return Turn(
        reasoning="r",
        decision=decision or [{"type": "note"}],
        usage=Usage(provider=provider, model="m", cost_usd=cost, input_tokens=10, output_tokens=5),
    )


def _obs(changed, summary="s"):
    return Observation(event={}, jobs=[], changed=changed, summary=summary)


def _mgr(tmp_path):
    return SessionManager("ClickHouse/praktika", "7", _store(tmp_path))


# ---------------------------------------------------------- round lifecycle


def test_round_opens_implicitly_on_failure(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})

    # A success turn does not open a round.
    m.observe_turn(_obs([{"name": "A", "status": "success"}]), _turn())
    assert m.session.open_round_id is None

    # A failure turn opens one implicitly.
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn())
    assert m.session.open_round_id is not None
    assert m._round.goal.startswith("Investigate failure")


def test_green_run_resolves_round(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn())
    round_id = m.session.open_round_id

    m.finalize_run("success", job_outcomes=[{"name": "B", "status": "success"}])
    assert m.session.open_round_id is None
    rnd = m.store.read_json(m._round_key(round_id))
    assert rnd["status"] == "resolved"


# ---------------------------------------------------------- continuity


def test_next_run_rejoins_open_round(tmp_path):
    # Run 1 fails -> round opens and persists.
    m1 = _mgr(tmp_path)
    m1.begin_run("run-1", "sha1", {})
    m1.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn())
    round_id = m1.session.open_round_id
    m1.finalize_run("failure")

    # A brand-new manager (fresh orchestrator on the next sha) rejoins it.
    m2 = SessionManager("ClickHouse/praktika", "7", _store(tmp_path))
    m2.begin_run("run-2", "sha2", {})
    assert m2._round is not None
    assert m2._round.round_id == round_id
    assert "run-2" in m2._round.run_ids


# ---------------------------------------------------------- cost roll-up


def test_cost_rolls_up_pr_round_run(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn(cost=0.02))
    m.observe_turn(_obs([{"name": "C", "status": "failure"}]), _turn(cost=0.03))

    summary = m.cost_summary()
    assert summary["pr"]["cost_usd"] == pytest.approx(0.05)
    assert summary["pr"]["turns"] == 2
    assert m._run.usage["cost_usd"] == pytest.approx(0.05)
    assert m._round.usage["cost_usd"] == pytest.approx(0.05)


# ---------------------------------------------------------- edits + fetch


def test_record_edit_and_round_log(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn())
    round_id = m.session.open_round_id

    edit_id = m.record_edit("--- a\n+++ b\n", commit_sha="cafe1234", files=["x.py"])
    assert edit_id is not None

    log = m.round_log(round_id)
    assert log["round"]["edits"][0]["commit_sha"] == "cafe1234"
    assert len(log["runs"]) == 1
    assert log["runs"][0]["turns"][0]["reasoning"] == "r"


def test_round_context_for_prompt(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn())
    round_id = m.session.open_round_id
    m.record_edit("patch", commit_sha="cafe", files=["x.py"])

    ctx = m.round_context_for_prompt(round_id)
    assert ctx["goal"].startswith("Investigate failure")
    assert ctx["edits"][0]["files"] == ["x.py"]
    assert ctx["attempts"][0]["sha"] == "sha1"


# ---------------------------------------------------------- budget stub


def test_budget_cost_cap(tmp_path):
    m = _mgr(tmp_path)
    m.session.budget["cost_cap_usd"] = 0.01
    m.begin_run("run-1", "sha1", {})
    ok, _ = m.can_continue_round()
    assert ok is True
    m.observe_turn(_obs([{"name": "B", "status": "failure"}]), _turn(cost=0.05))
    ok, reason = m.can_continue_round()
    assert ok is False
    assert "cost cap" in reason


def test_record_edit_without_round_is_noop(tmp_path):
    m = _mgr(tmp_path)
    m.begin_run("run-1", "sha1", {})
    assert m.record_edit("p", "sha") is None
