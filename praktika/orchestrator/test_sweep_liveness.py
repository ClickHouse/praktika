"""Tests for WorkflowState.sweep_liveness — the orchestrator's S3-heartbeat
based dead-job detector.

Two rules under test:
  1. RUNNING + heartbeat seen + age > DEAD_THRESHOLD_S → fail_dead.
  2. RUNNING + no heartbeat ever + age since kick > PICKUP_GRACE_S → fail_dead.
A fresh RUNNING job (within grace, no heartbeat yet) must stay RUNNING.

The S3 client is faked with a dict-backed stub so the tests don't need
boto3 or network access. ``_make_running_state`` builds a minimal
WorkflowState with one or more RUNNING jobs and a stubbed _s3 — enough
surface for sweep_liveness without booting the full DAG init.
"""
import io
import json
import time
import types

import pytest

from praktika.orchestrator.state import (
    HEARTBEAT_DEAD_THRESHOLD_S,
    HEARTBEAT_PICKUP_GRACE_S,
    JobState,
    JobStatus,
    WorkflowState,
    _normalize_job_name_for_s3,
)


class _FakeS3:
    """Minimal in-memory get_object stub (returns bytes wrapped in a
    Body-like object). Missing keys raise the same way moto/boto would."""

    def __init__(self, store=None):
        self._store = store or {}

    def put(self, bucket, key, body):
        self._store[(bucket, key)] = body

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 signature
        if (Bucket, Key) not in self._store:
            raise KeyError(f"NoSuchKey {Bucket}/{Key}")
        return {"Body": io.BytesIO(self._store[(Bucket, Key)])}


def _make_running_state(job_names, started_at_offsets, fake_s3, run_id="run42"):
    """Build a WorkflowState with the given jobs already RUNNING.

    ``started_at_offsets`` is a dict {name: seconds_ago} for each job's
    ``started_at`` relative to ``time.time()``.
    """
    state = WorkflowState.__new__(WorkflowState)
    state.jobs = {}
    state._deps = {}
    state._s3 = fake_s3
    state._cancel_s3_bucket = "test-bucket"
    state._run_id = run_id
    state._runs_s3_prefix = f"runs/{run_id}"
    state.local_mode = False
    state.cancelled = False
    state._completions_queue_url = "test-queue"
    now = time.time()
    for name in job_names:
        js = JobState.__new__(JobState)
        js.job = types.SimpleNamespace(
            name=name,
            runs_on=[],
            requires=[],
            run_after=[],
            provides=[],
            always_run=False,
        )
        js.check = None
        js.status = JobStatus.RUNNING
        js.rc = None
        js.started_at = now - started_at_offsets[name]
        js.finished_at = None
        js.filter_reason = None
        js.last_heartbeat_ts = None
        js._workflow_state = state
        state.jobs[name] = js
    return state


def _put_heartbeat(fake_s3, run_id, job_name, ts):
    key = f"runs/{run_id}/{_normalize_job_name_for_s3(job_name)}/heartbeat.json"
    fake_s3.put("test-bucket", key, json.dumps({"ts": ts, "status": "running"}).encode())


def test_fresh_running_job_stays_running():
    """No heartbeat yet, kicked seconds ago — must NOT be marked dead."""
    s3 = _FakeS3()
    state = _make_running_state(["A"], {"A": 5}, s3)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.RUNNING


def test_pickup_grace_expired_with_no_heartbeat_marks_dead():
    """Job kicked > PICKUP_GRACE_S ago and no heartbeat ever → FAILURE."""
    s3 = _FakeS3()
    state = _make_running_state(
        ["A"], {"A": HEARTBEAT_PICKUP_GRACE_S + 30}, s3
    )
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.FAILURE
    assert state.jobs["A"].rc == 1


def test_within_pickup_grace_with_no_heartbeat_stays_running():
    """Slow clone/pip install path — must not be flagged before grace expires."""
    s3 = _FakeS3()
    state = _make_running_state(
        ["A"], {"A": HEARTBEAT_PICKUP_GRACE_S - 30}, s3
    )
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.RUNNING


def test_recent_heartbeat_keeps_job_running():
    """Heartbeat was within the dead threshold — job is alive."""
    s3 = _FakeS3()
    state = _make_running_state(
        ["A"], {"A": HEARTBEAT_PICKUP_GRACE_S + 60}, s3
    )
    _put_heartbeat(s3, "run42", "A", time.time() - 10)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.RUNNING
    assert state.jobs["A"].last_heartbeat_ts is not None


def test_stale_heartbeat_marks_dead():
    """Heartbeat older than DEAD_THRESHOLD_S → FAILURE (runner died mid-job)."""
    s3 = _FakeS3()
    state = _make_running_state(
        ["A"], {"A": HEARTBEAT_PICKUP_GRACE_S + 200}, s3
    )
    _put_heartbeat(s3, "run42", "A", time.time() - (HEARTBEAT_DEAD_THRESHOLD_S + 30))
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.FAILURE


def test_sweep_is_noop_in_local_mode():
    """No S3 client in local mode — sweep must short-circuit."""
    state = _make_running_state(["A"], {"A": HEARTBEAT_PICKUP_GRACE_S + 60}, _FakeS3())
    state._s3 = None
    state.sweep_liveness()
    # Job stays RUNNING despite the long age — sweep didn't run.
    assert state.jobs["A"].status == JobStatus.RUNNING


def test_normalized_job_name_in_heartbeat_key():
    """Job names with spaces use the same normalization as the orchestrator
    side, so the heartbeat written by the agent is the one read here."""
    s3 = _FakeS3()
    state = _make_running_state(
        ["Build And Test"], {"Build And Test": HEARTBEAT_PICKUP_GRACE_S + 60}, s3
    )
    _put_heartbeat(s3, "run42", "Build And Test", time.time() - 5)
    state.sweep_liveness()
    assert state.jobs["Build And Test"].status == JobStatus.RUNNING


def test_sweep_with_no_running_jobs_is_noop():
    """If nothing is RUNNING, sweep returns immediately without any S3 calls."""
    s3 = _FakeS3()
    state = _make_running_state(["A"], {"A": 0}, s3)
    state.jobs["A"].status = JobStatus.SUCCESS

    # Replace _s3 with one that fails on any call to ensure no S3 traffic.
    class _BoomS3:
        def get_object(self, **_):
            raise AssertionError("S3 must not be called when nothing is running")

    state._s3 = _BoomS3()
    state.sweep_liveness()  # must not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
