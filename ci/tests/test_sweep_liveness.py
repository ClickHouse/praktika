"""Tests for WorkflowState.sweep_liveness — the orchestrator's S3-heartbeat
based pickup detector and dead-job detector.

Rules under test:
  1. QUEUED + heartbeat seen → RUNNING.
  2. RUNNING + heartbeat age > HEARTBEAT_TIMEOUT_S → fail_dead.
  3. QUEUED + no heartbeat ever + age since kick > RUNNER_PICKUP_TIMEOUT_S
     → fail_dead.
A fresh QUEUED job (within grace, no heartbeat yet) must stay QUEUED.

The S3 client is faked with a dict-backed stub so the tests don't need
boto3 or network access. ``_make_queued_state`` builds a minimal
WorkflowState with one or more QUEUED jobs and a stubbed _s3 — enough
surface for sweep_liveness without booting the full DAG init.
"""

import io
import json
import time
import types

import pytest

from praktika.orchestrator import state as state_mod
from praktika.orchestrator.state import (
    HEARTBEAT_TIMEOUT_S,
    JobState,
    JobStatus,
    RUNNER_PICKUP_TIMEOUT_S,
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


def _make_queued_state(job_names, started_at_offsets, fake_s3, run_id="run42"):
    """Build a WorkflowState with the given jobs already QUEUED.

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
        js.status = JobStatus.QUEUED
        js.rc = None
        js.started_at = now - started_at_offsets[name]
        js.finished_at = None
        js.filter_reason = None
        js.last_heartbeat_ts = None
        js.last_heartbeat_phase = None
        js.runner_instance_id = None
        js._workflow_state = state
        state.jobs[name] = js
    return state


def _put_heartbeat(fake_s3, run_id, job_name, ts, **fields):
    key = f"runs/{run_id}/{_normalize_job_name_for_s3(job_name)}/heartbeat.json"
    body = {"ts": ts, "status": "running"}
    body.update(fields)
    fake_s3.put("test-bucket", key, json.dumps(body).encode())


class _FakeCheck:
    def __init__(self):
        self.in_progress = []
        self.updated = []
        self.completed = []

    def set_in_progress(self, output=None, details_url=None):
        self.in_progress.append({"output": output, "details_url": details_url})

    def update(self, output=None, details_url=None, status=None, conclusion=None):
        self.updated.append(
            {
                "output": output,
                "details_url": details_url,
                "status": status,
                "conclusion": conclusion,
            }
        )

    def complete(self, conclusion, output=None, details_url=None):
        self.completed.append(
            {"conclusion": conclusion, "output": output, "details_url": details_url}
        )


def test_queued_check_output_names_state_and_pool(monkeypatch):
    job = types.SimpleNamespace(name="A", runs_on=["arm-small"])
    js = JobState(
        job,
        workflow_state=types.SimpleNamespace(
            can_post_checks=True,
            workflow=types.SimpleNamespace(name="CI"),
            _gh_token="token",
            _repo="org/repo",
            _head_sha="sha",
        ),
    )
    calls = []

    def fake_queue(token, repo, head_sha, name, output=None):
        calls.append(
            {
                "token": token,
                "repo": repo,
                "head_sha": head_sha,
                "name": name,
                "output": output,
            }
        )
        return _FakeCheck()

    monkeypatch.setattr(state_mod.JobCheckRun, "queue", fake_queue)

    js._create_check()

    assert calls == [
        {
            "token": "token",
            "repo": "org/repo",
            "head_sha": "sha",
            "name": "CI / A",
            "output": {
                "title": "QUEUED",
                "summary": "QUEUED: job dispatched to runner pool `arm-small`.",
            },
        }
    ]


def test_fresh_queued_job_stays_queued():
    """No heartbeat yet, kicked seconds ago — must NOT be marked dead."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": 5}, s3)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.QUEUED


def test_pickup_timeout_expired_with_no_heartbeat_marks_dead():
    """Job queued longer than RUNNER_PICKUP_TIMEOUT_S with no heartbeat → FAILURE."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 30}, s3)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.FAILURE
    assert state.jobs["A"].rc == 1


def test_transient_heartbeat_read_error_does_not_mark_dead():
    """Unknown S3 read errors should not be treated as a missing heartbeat."""

    class _TransientS3:
        def get_object(self, **_):
            raise RuntimeError("temporary")

    state = _make_queued_state(
        ["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 30}, _TransientS3()
    )

    state.sweep_liveness()

    assert state.jobs["A"].status == JobStatus.QUEUED


def test_pickup_grace_reason_mentions_runner_pool():
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 30}, s3)
    state.jobs["A"].job.runs_on = ["arm-2xsmall"]
    reasons = []
    state.jobs["A"].fail_dead = reasons.append

    state.sweep_liveness()

    assert reasons == [
        (
            "runner pool `arm-2xsmall` never started job "
            f"(no heartbeat in {RUNNER_PICKUP_TIMEOUT_S + 30}s, "
            f"timeout={RUNNER_PICKUP_TIMEOUT_S}s)"
        )
    ]


def test_within_pickup_grace_with_no_heartbeat_stays_queued():
    """Queue/ASG delays must not be flagged before grace expires."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S - 30}, s3)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.QUEUED


def test_recent_heartbeat_marks_job_running():
    """Heartbeat was within the dead threshold — job is alive."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 60}, s3)
    _put_heartbeat(s3, "run42", "A", time.time() - 10)
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.RUNNING
    assert state.jobs["A"].last_heartbeat_ts is not None


def test_heartbeat_sets_check_in_progress_with_runner_id():
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": 5}, s3)
    check = _FakeCheck()
    state.jobs["A"].check = check
    key = f"runs/run42/{_normalize_job_name_for_s3('A')}/heartbeat.json"
    s3.put(
        "test-bucket",
        key,
        json.dumps(
            {
                "ts": time.time() - 1,
                "status": "running",
                "instance_id": "i-runner",
                "phase": "cloning",
            }
        ).encode(),
    )

    state.sweep_liveness()

    assert state.jobs["A"].status == JobStatus.RUNNING
    assert state.jobs["A"].runner_instance_id == "i-runner"
    assert state.jobs["A"].last_heartbeat_phase == "cloning"
    assert check.in_progress == [
        {
            "output": {
                "title": "RUNNING",
                "summary": "RUNNING on runner `i-runner`. Phase: `cloning`.",
            },
            "details_url": None,
        }
    ]


def test_running_heartbeat_refreshes_check_output_on_phase_change():
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 5}, s3)
    check = _FakeCheck()
    state.jobs["A"].check = check
    state.jobs["A"].status = JobStatus.RUNNING
    state.jobs["A"].last_heartbeat_ts = time.time() - 10
    state.jobs["A"].last_heartbeat_phase = "cloning"
    state.jobs["A"].runner_instance_id = "i-runner"
    key = f"runs/run42/{_normalize_job_name_for_s3('A')}/heartbeat.json"
    s3.put(
        "test-bucket",
        key,
        json.dumps(
            {
                "ts": time.time() - 1,
                "status": "running",
                "instance_id": "i-runner",
                "phase": "running_job",
            }
        ).encode(),
    )

    state.sweep_liveness()

    assert state.jobs["A"].last_heartbeat_phase == "running_job"
    assert check.updated == [
        {
            "output": {
                "title": "RUNNING",
                "summary": "RUNNING on runner `i-runner`. Phase: `running_job`.",
            },
            "details_url": None,
            "status": "in_progress",
            "conclusion": None,
        }
    ]


def test_stale_heartbeat_marks_dead():
    """Stale heartbeat after pickup → FAILURE (runner died mid-job)."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 200}, s3)
    _put_heartbeat(s3, "run42", "A", time.time() - (HEARTBEAT_TIMEOUT_S + 30))
    state.sweep_liveness()
    assert state.jobs["A"].status == JobStatus.FAILURE


def test_stale_heartbeat_reason_names_runner_and_phase():
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 200}, s3)
    state.jobs["A"].status = JobStatus.RUNNING
    state.jobs["A"].job.runs_on = ["arm-medium"]
    reasons = []
    state.jobs["A"].fail_dead = reasons.append
    _put_heartbeat(
        s3,
        "run42",
        "A",
        time.time() - (HEARTBEAT_TIMEOUT_S + 30),
        instance_id="i-runner",
        phase="cloning",
    )

    state.sweep_liveness()

    assert reasons == [
        (
            "runner `i-runner` in pool `arm-medium` stopped heartbeating "
            f"during phase `cloning` (no heartbeat in {HEARTBEAT_TIMEOUT_S + 30}s, "
            f"timeout={HEARTBEAT_TIMEOUT_S}s)"
        )
    ]


def test_sweep_is_noop_in_local_mode():
    """No S3 client in local mode — sweep must short-circuit."""
    state = _make_queued_state(["A"], {"A": RUNNER_PICKUP_TIMEOUT_S + 60}, _FakeS3())
    state._s3 = None
    state.sweep_liveness()
    # Job stays QUEUED despite the long age — sweep didn't run.
    assert state.jobs["A"].status == JobStatus.QUEUED


def test_normalized_job_name_in_heartbeat_key():
    """Job names with spaces use the same normalization as the orchestrator
    side, so the heartbeat written by the agent is the one read here."""
    s3 = _FakeS3()
    state = _make_queued_state(
        ["Build And Test"], {"Build And Test": RUNNER_PICKUP_TIMEOUT_S + 60}, s3
    )
    _put_heartbeat(s3, "run42", "Build And Test", time.time() - 5)
    state.sweep_liveness()
    assert state.jobs["Build And Test"].status == JobStatus.RUNNING


def test_sweep_with_no_running_jobs_is_noop():
    """If nothing is in flight, sweep returns immediately without any S3 calls."""
    s3 = _FakeS3()
    state = _make_queued_state(["A"], {"A": 0}, s3)
    state.jobs["A"].status = JobStatus.SUCCESS

    # Replace _s3 with one that fails on any call to ensure no S3 traffic.
    class _BoomS3:
        def get_object(self, **_):
            raise AssertionError("S3 must not be called when nothing is running")

    state._s3 = _BoomS3()
    state.sweep_liveness()  # must not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
