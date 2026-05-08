"""Tests for WorkflowState.sweep_completions — the orchestrator's S3-based
job-completion path (phase 2 of the liveness work).

The runner writes ``runs/<run_id>/<job>/final.json`` with
``{rc, environment, ...}`` on exit and the orchestrator polls it here
instead of consuming SQS ``job_completion`` messages. The same
WorkflowState is restart-safe: the file is durable in S3, so an
orchestrator that died after dispatch picks the result up on restart.

The S3 client is faked with a dict-backed stub so the tests don't need
boto3 or network access.
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
    _normalize_job_name_for_s3,
)


class _FakeS3:
    def __init__(self, store=None):
        self._store = store or {}

    def put(self, bucket, key, body):
        self._store[(bucket, key)] = body

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 signature
        if (Bucket, Key) not in self._store:
            raise KeyError(f"NoSuchKey {Bucket}/{Key}")
        return {"Body": io.BytesIO(self._store[(Bucket, Key)])}


def _make_state(job_names, fake_s3, run_id="run42", status=JobStatus.RUNNING):
    state = WorkflowState.__new__(WorkflowState)
    state.jobs = {}
    state._deps = {}
    state._s3 = fake_s3
    state._cancel_s3_bucket = "test-bucket"
    state._run_id = run_id
    state._runs_s3_prefix = f"runs/{run_id}"
    state.local_mode = False
    state.cancelled = False
    state._environment = None
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
        js.status = status
        js.rc = None
        js.started_at = now - 5
        js.finished_at = None
        js.filter_reason = None
        js.last_heartbeat_ts = None
        js._workflow_state = state
        state.jobs[name] = js

    # apply_filtered_jobs is called from sweep_completions when the env
    # snapshot carries a WORKFLOW_CONFIG. Stub it so we don't need the
    # _post_skipped_summary surface (gh_token, repo, head_sha).
    state.apply_filtered_jobs = lambda *_: None
    return state


def _put_final(fake_s3, run_id, job_name, *, rc, environment=None):
    key = f"runs/{run_id}/{_normalize_job_name_for_s3(job_name)}/final.json"
    payload = {"type": "job_completion", "job_name": job_name, "rc": rc, "ts": time.time()}
    if environment is not None:
        payload["environment"] = environment
    fake_s3.put("test-bucket", key, json.dumps(payload).encode())


def test_final_state_advances_running_to_success():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.SUCCESS
    assert state.jobs["A"].rc == 0


def test_final_state_advances_running_to_failure():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    _put_final(s3, "run42", "A", rc=1)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.FAILURE
    assert state.jobs["A"].rc == 1


def test_missing_final_state_keeps_job_running():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.RUNNING


def test_environment_snapshot_propagates_to_state():
    """``environment`` in final.json must be stashed on the state so
    downstream jobs inherit WORKFLOW_CONFIG / JOB_KV_DATA / etc."""
    s3 = _FakeS3()
    state = _make_state(["Config"], s3)
    env = {"WORKFLOW_CONFIG": {"filtered_jobs": {}}, "JOB_KV_DATA": {"k": "v"}}
    _put_final(s3, "run42", "Config", rc=0, environment=env)
    state.sweep_completions()
    assert state._environment == env
    assert state.jobs["Config"].status == JobStatus.SUCCESS


def test_idempotent_for_already_finished_job():
    """A job that already transitioned (e.g. via sweep_liveness fail_dead)
    must not be moved by a final.json arriving after the fact."""
    s3 = _FakeS3()
    state = _make_state(["A"], s3, status=JobStatus.FAILURE)
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    # finish() is gated on RUNNING — FAILURE stays FAILURE.
    assert state.jobs["A"].status == JobStatus.FAILURE


def test_sweep_is_noop_in_local_mode():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    state._s3 = None
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.RUNNING


def test_normalized_job_name_in_final_key():
    s3 = _FakeS3()
    state = _make_state(["Build And Test"], s3)
    _put_final(s3, "run42", "Build And Test", rc=0)
    state.sweep_completions()
    assert state.jobs["Build And Test"].status == JobStatus.SUCCESS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
