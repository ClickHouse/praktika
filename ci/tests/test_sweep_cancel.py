"""Tests for WorkflowState.sweep_cancel — the orchestrator's S3-based
cancel-signal detector (phase 2b).

Two channels, both written by the lambda:
  - ``runs/<run_id>/cancel-request`` — manual UI Cancel button.
  - ``pr/<pr>/cancel-before`` carrying ``{ts}`` — fan-out cancel on a
    new push. Older runs (``event_ts < cancel-before-ts``) self-cancel;
    the freshly enqueued run (``event_ts == cancel-before-ts``) stays
    alive (strict less-than).
"""
import io
import json
import time
import types

import pytest

from praktika.orchestrator.state import (
    JobState,
    JobStatus,
    WorkflowState,
)


class _FakeS3:
    def __init__(self, store=None):
        self._store = store or {}

    def put(self, bucket, key, body):
        self._store[(bucket, key)] = body

    def head_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self._store:
            raise KeyError(f"NoSuchKey {Bucket}/{Key}")
        return {}

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self._store:
            raise KeyError(f"NoSuchKey {Bucket}/{Key}")
        return {"Body": io.BytesIO(self._store[(Bucket, Key)])}


def _make_state(
    fake_s3, *, run_id="run42", pr_number=1, event_ts=1000.0, has_running=True
):
    state = WorkflowState.__new__(WorkflowState)
    state.jobs = {}
    state._deps = {}
    state._s3 = fake_s3
    state._cancel_s3_bucket = "test-bucket"
    state._run_id = run_id
    state._runs_s3_prefix = f"runs/{run_id}"
    state._cancel_request_s3_key = f"runs/{run_id}/cancel-request"
    state._pr_cancel_before_s3_key = (
        f"pr/{pr_number}/cancel-before" if pr_number else None
    )
    state._event_ts = event_ts
    state._pr_number = pr_number
    state.local_mode = False
    state.cancelled = False
    state._environment = None

    if has_running:
        js = JobState.__new__(JobState)
        js.job = types.SimpleNamespace(
            name="A",
            runs_on=[],
            requires=[],
            run_after=[],
            provides=[],
            always_run=False,
        )
        js.check = None
        js.status = JobStatus.RUNNING
        js.rc = None
        js.started_at = time.time() - 5
        js.finished_at = None
        js.filter_reason = None
        js.last_heartbeat_ts = None
        js._workflow_state = state
        state.jobs["A"] = js
    return state


def test_no_signal_keeps_state_uncancelled():
    state = _make_state(_FakeS3())
    state.sweep_cancel()
    assert state.cancelled is False


def test_cancel_request_marks_cancelled():
    s3 = _FakeS3()
    s3.put("test-bucket", "runs/run42/cancel-request", b"requested")
    state = _make_state(s3)
    state.sweep_cancel()
    assert state.cancelled is True


def test_cancel_before_newer_than_event_ts_marks_cancelled():
    """Older run (event_ts=1000) sees cancel-before=2000 → cancel."""
    s3 = _FakeS3()
    s3.put("test-bucket", "pr/1/cancel-before", json.dumps({"ts": 2000.0}).encode())
    state = _make_state(s3, event_ts=1000.0)
    state.sweep_cancel()
    assert state.cancelled is True


def test_cancel_before_equal_to_event_ts_does_not_cancel():
    """Freshly enqueued run (event_ts == cancel-before-ts) must stay alive.

    Lambda writes cancel-before with the same ts it stamps on the new
    workflow event, so strict less-than spares the new run from
    self-cancelling.
    """
    s3 = _FakeS3()
    s3.put("test-bucket", "pr/1/cancel-before", json.dumps({"ts": 2000.0}).encode())
    state = _make_state(s3, event_ts=2000.0)
    state.sweep_cancel()
    assert state.cancelled is False


def test_cancel_before_older_than_event_ts_does_not_cancel():
    """Newer run sees a stale cancel-before (from before this run) — no-op."""
    s3 = _FakeS3()
    s3.put("test-bucket", "pr/1/cancel-before", json.dumps({"ts": 500.0}).encode())
    state = _make_state(s3, event_ts=1000.0)
    state.sweep_cancel()
    assert state.cancelled is False


def test_zero_event_ts_skips_cancel_before():
    """event_ts not set (e.g. local CLI run) → don't apply per-PR cancel.

    The strict ``cancel_before > event_ts > 0`` guard avoids treating an
    unset event_ts as "infinitely old".
    """
    s3 = _FakeS3()
    s3.put("test-bucket", "pr/1/cancel-before", json.dumps({"ts": 2000.0}).encode())
    state = _make_state(s3, event_ts=0.0)
    state.sweep_cancel()
    assert state.cancelled is False


def test_no_pr_number_skips_per_pr_check():
    """Push events have no pr_number → skip the cancel-before path."""
    s3 = _FakeS3()
    state = _make_state(s3, pr_number=None)
    state.sweep_cancel()
    assert state.cancelled is False


def test_already_cancelled_is_idempotent():
    """Once cancelled, sweep returns immediately (no re-trigger)."""
    s3 = _FakeS3()
    s3.put("test-bucket", "runs/run42/cancel-request", b"requested")
    state = _make_state(s3)
    state.cancelled = True
    state.sweep_cancel()
    # Still cancelled, but more importantly: no exception, no re-print.
    assert state.cancelled is True


def test_local_mode_is_noop():
    s3 = _FakeS3()
    s3.put("test-bucket", "runs/run42/cancel-request", b"requested")
    state = _make_state(s3)
    state._s3 = None
    state.sweep_cancel()
    assert state.cancelled is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
