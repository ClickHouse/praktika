"""Minimal mode: a re-run must overwrite a job's previous failed check with the
re-run's own terminal result, whatever that result is.

In minimal mode a passing/skipped job posts no per-job check, so the only way a
job carries a check is that an earlier attempt failed (red). On re-run that red
check must be superseded by a fresh check-run that reflects the new outcome:
- re-run passes  -> a green (success) check,
- re-run fails   -> a new red (failure) check,
- re-run dies    -> a new red (failure) check.

This exercises the real JobState check lifecycle (_create_completed_check for the
first failure, then the _reset_job force path -> queue -> complete) used by
orchestrator re-runs.
"""
from unittest import mock

import pytest

import praktika.orchestrator.state as state_mod
from praktika.orchestrator.state import JobState, JobStatus


class _RecordingCheck:
    """Stand-in for JobCheckRun: records this check-run's lifecycle events."""

    _next_id = 0

    def __init__(self, kind, events):
        _RecordingCheck._next_id += 1
        self.id = _RecordingCheck._next_id
        self._events = events

    def complete(self, conclusion, **kwargs):
        self._events.append(f"complete:{conclusion}")

    def set_in_progress(self, **kwargs):
        self._events.append("set_in_progress")


def _install_fake_check_run(monkeypatch, events):
    def queue(token, repo, head_sha, name, output=None, external_id=None):
        events.append("queue")
        return _RecordingCheck("queued", events)

    def create_completed(token, repo, head_sha, name, conclusion, **kwargs):
        events.append(f"create_completed:{conclusion}")
        return _RecordingCheck("completed", events)

    monkeypatch.setattr(state_mod.JobCheckRun, "queue", staticmethod(queue))
    monkeypatch.setattr(
        state_mod.JobCheckRun, "create_completed", staticmethod(create_completed)
    )


def _make_job():
    job = mock.Mock()
    job.name = "j"
    job.runs_on = ["x"]
    ws = mock.Mock()
    ws.can_post_checks = True
    ws._gh_token = "t"
    ws._repo = "r"
    ws._head_sha = "s"
    ws._run_id = "rid"
    ws.local_mode = True
    ws.workflow = mock.Mock()
    ws.workflow.name = "W"
    js = JobState(job, workflow_state=ws)
    return js


def _fail_then_reset(js, events):
    """First attempt fails (posts the red check), then simulate _reset_job's
    overwrite trigger: a job that had a check gets a fresh full-lifecycle check."""
    js.status = JobStatus.RUNNING
    js.finish(success=False, output={"title": "t", "summary": "s"})
    assert js.check is not None, "a failed job must carry a check in minimal mode"
    assert events == ["create_completed:failure"], events
    # _reset_job: previously-checked job -> drop handle, force a fresh check.
    had_check = js.check is not None
    js.check = None
    js._create_check(force=had_check)
    assert js.check is not None, "re-run must re-post a check for a previously-failed job"
    assert events == ["create_completed:failure", "queue"], events


@pytest.mark.parametrize("outcome", ["pass", "fail", "dead"])
def test_rerun_overwrites_failed_check_in_minimal_mode(monkeypatch, outcome):
    monkeypatch.setattr(state_mod.Settings, "JOB_CHECKS_MODE", "minimal", raising=False)
    events = []
    _install_fake_check_run(monkeypatch, events)

    js = _make_job()
    _fail_then_reset(js, events)

    js.status = JobStatus.RUNNING
    if outcome == "pass":
        js.finish(success=True, output={"title": "t", "summary": "s"})
        expected_terminal = "complete:success"
    elif outcome == "fail":
        js.finish(success=False, output={"title": "t", "summary": "s"})
        expected_terminal = "complete:failure"
    else:  # dead
        js.fail_dead("no heartbeat")
        expected_terminal = "complete:failure"

    # The re-run completes the SAME fresh check-run (the one posted by queue),
    # so the terminal result overwrites the earlier red check.
    assert events == ["create_completed:failure", "queue", expected_terminal], events
