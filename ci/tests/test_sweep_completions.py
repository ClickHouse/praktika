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

from praktika.settings import Settings
from praktika.orchestrator.state import (
    JobState,
    JobStatus,
    RUNNER_PICKUP_TIMEOUT_S,
    WorkflowState,
    _normalize_job_name_for_s3,
)
from praktika.orchestrator import state as state_mod


class _FakeS3:
    def __init__(self, store=None):
        self._store = store or {}

    def put(self, bucket, key, body):
        self._store[(bucket, key)] = body

    def get_object(self, Bucket, Key):  # noqa: N803 — boto3 signature
        if (Bucket, Key) not in self._store:
            raise KeyError(f"NoSuchKey {Bucket}/{Key}")
        return {"Body": io.BytesIO(self._store[(Bucket, Key)])}


class _FakeCheck:
    def __init__(self):
        self.completed = []

    def complete(self, conclusion, output=None, details_url=None):
        self.completed.append(
            {"conclusion": conclusion, "output": output, "details_url": details_url}
        )


class _PostedCheck:
    def __init__(self, record):
        self.record = record

    def complete(self, conclusion, output=None, details_url=None):
        self.record["completed"].append(
            {"conclusion": conclusion, "output": output, "details_url": details_url}
        )


def _capture_check_queues(monkeypatch):
    checks = []

    def fake_queue(token, repo, head_sha, name, output=None):
        record = {
            "token": token,
            "repo": repo,
            "head_sha": head_sha,
            "name": name,
            "output": output,
            "completed": [],
        }
        checks.append(record)
        return _PostedCheck(record)

    def fake_create_completed(
        token, repo, head_sha, name, conclusion, output=None, details_url=None
    ):
        # Skipped jobs post their terminal state in a single call. Record the
        # conclusion under the same "completed" shape the queue()->complete()
        # path used, so tests assert on one surface regardless of which path
        # a job took.
        record = {
            "token": token,
            "repo": repo,
            "head_sha": head_sha,
            "name": name,
            "output": output,
            "completed": [
                {
                    "conclusion": conclusion,
                    "output": output,
                    "details_url": details_url,
                }
            ],
        }
        checks.append(record)
        return _PostedCheck(record)

    monkeypatch.setattr(state_mod.JobCheckRun, "queue", fake_queue)
    monkeypatch.setattr(state_mod.JobCheckRun, "create_completed", fake_create_completed)
    return checks


def _make_state(
    job_names,
    fake_s3,
    run_id="run42",
    status=JobStatus.QUEUED,
    apply_config=False,
):
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
    state.workflow = types.SimpleNamespace(name="CI")
    state._gh_token = None
    state._repo = None
    state._head_sha = None
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
        js.result = None
        js._workflow_state = state
        state.jobs[name] = js

    # apply_workflow_config is called from sweep_completions when the env
    # snapshot carries a WORKFLOW_CONFIG. Stub it by default so most tests
    # don't need the skip/check-run surface.
    if not apply_config:
        state.apply_workflow_config = lambda *_: None
    return state


def _put_final(
    fake_s3,
    run_id,
    job_name,
    *,
    rc,
    environment=None,
    result=None,
    details_url=None,
    instance_id=None,
):
    key = f"runs/{run_id}/{_normalize_job_name_for_s3(job_name)}/final.json"
    payload = {"type": "job_completion", "job_name": job_name, "rc": rc, "ts": time.time()}
    if environment is not None:
        payload["environment"] = environment
    if result is not None:
        payload["result"] = result
    if details_url is not None:
        payload["details_url"] = details_url
    if instance_id is not None:
        payload["instance_id"] = instance_id
    fake_s3.put("test-bucket", key, json.dumps(payload).encode())


def _result_payload(name, status="OK"):
    """Minimal serialized Result (Result.to_dict shape) for completion tests."""
    return {
        "name": name,
        "status": status,
        "start_time": None,
        "duration": 12.0,
        "results": [],
        "files": [],
        "links": [],
        "info": "",
        "ext": {},
    }


def test_final_state_advances_queued_to_success():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.SUCCESS
    assert state.jobs["A"].rc == 0


def test_final_state_renders_check_from_result_payload():
    """The orchestrator renders the check-run output from the Result shipped
    in the payload, stashes the raw Result on the JobState (for AI
    observation), and completes the check with the rendered output."""
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    check = _FakeCheck()
    state.jobs["A"].check = check
    result = _result_payload("A", status="OK")
    details_url = "https://example.com/report"
    _put_final(
        s3,
        "run42",
        "A",
        rc=0,
        result=result,
        details_url=details_url,
        instance_id="i-runner",
    )

    state.sweep_completions()

    assert state.jobs["A"].runner_instance_id == "i-runner"
    # Raw Result retained verbatim for AI observation.
    assert state.jobs["A"].result == result
    assert len(check.completed) == 1
    completed = check.completed[0]
    assert completed["conclusion"] == "success"
    assert completed["details_url"] == details_url
    output = completed["output"]
    assert output["title"] == "OK"
    assert "**OK**" in output["summary"]
    assert details_url in output["summary"]
    assert "runner `i-runner`" in output["summary"]


def test_final_state_without_result_completes_bodyless():
    """A payload with no Result still finishes the job (bodyless check)."""
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    check = _FakeCheck()
    state.jobs["A"].check = check
    _put_final(s3, "run42", "A", rc=0)

    state.sweep_completions()

    assert state.jobs["A"].status == JobStatus.SUCCESS
    assert state.jobs["A"].result is None
    assert check.completed == [
        {"conclusion": "success", "output": None, "details_url": None}
    ]


def test_final_state_advances_running_to_success():
    s3 = _FakeS3()
    state = _make_state(["A"], s3, status=JobStatus.RUNNING)
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.SUCCESS
    assert state.jobs["A"].rc == 0


def test_final_state_advances_queued_to_failure():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    _put_final(s3, "run42", "A", rc=1)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.FAILURE
    assert state.jobs["A"].rc == 1


def test_missing_final_state_keeps_job_queued():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.QUEUED


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


def test_workflow_config_cache_success_skips_job_with_report_check(monkeypatch):
    monkeypatch.setattr(Settings, "S3_REPORT_BUCKET", "silk-reports")
    monkeypatch.setattr(Settings, "S3_BUCKET_TO_HTTP_ENDPOINT", {})

    s3 = _FakeS3()
    state = _make_state(["Formatting"], s3, status=JobStatus.PENDING, apply_config=True)
    state.workflow = types.SimpleNamespace(name="Pull Request CI")
    state._gh_token = "token"
    state._repo = "ClickHouse/silk"
    state._head_sha = "af1e0ee"
    checks = _capture_check_queues(monkeypatch)

    state.apply_workflow_config(
        {
            "filtered_jobs": {},
            "cache_success": ["Formatting"],
            "cache_jobs": {
                "Formatting": {
                    "type": "success",
                    "sha": "f3ffcfe0c728a75081fe1b6f168a43ad1e564a01",
                    "pr_number": 69,
                    "branch": "add-praktika-ci-config",
                    "workflow": "Pull Request CI",
                }
            },
        }
    )

    assert state.jobs["Formatting"].status == JobStatus.SKIPPED
    assert state.jobs["Formatting"].filter_reason == "reused from cache"

    per_job = checks[0]
    assert per_job["name"] == "Pull Request CI / Formatting"
    assert per_job["completed"][0]["conclusion"] == "skipped"
    details_url = per_job["completed"][0]["details_url"]
    assert details_url.startswith("https://silk-reports/praktika.html?PR=69")
    assert "sha=f3ffcfe0c728a75081fe1b6f168a43ad1e564a01" in details_url
    assert "name_0=Pull%20Request%20CI" in details_url
    assert "name_1=Formatting" in details_url
    assert details_url in per_job["completed"][0]["output"]["summary"]
    assert len(checks) == 1


def test_workflow_config_filtered_job_posts_per_job_skip_check(monkeypatch):
    s3 = _FakeS3()
    state = _make_state(["Lint"], s3, status=JobStatus.PENDING, apply_config=True)
    state.workflow = types.SimpleNamespace(name="Pull Request CI")
    state._gh_token = "token"
    state._repo = "ClickHouse/silk"
    state._head_sha = "af1e0ee"
    checks = _capture_check_queues(monkeypatch)

    state.apply_workflow_config(
        {
            "filtered_jobs": {"Lint": "not affected by this diff"},
            "cache_success": [],
            "cache_jobs": {},
        }
    )

    assert state.jobs["Lint"].status == JobStatus.SKIPPED
    assert state.jobs["Lint"].filter_reason == "not affected by this diff"
    assert len(checks) == 1
    assert checks[0]["name"] == "Pull Request CI / Lint"
    assert checks[0]["completed"] == [
        {
            "conclusion": "skipped",
            "output": {
                "title": "SKIPPED",
                "summary": "SKIPPED: not affected by this diff.",
            },
            "details_url": None,
        }
    ]


def test_sweep_completion_applies_cache_success_from_workflow_config():
    s3 = _FakeS3()
    state = _make_state(
        ["Config", "Formatting"], s3, status=JobStatus.PENDING, apply_config=True
    )
    state.jobs["Config"].status = JobStatus.QUEUED
    env = {
        "WORKFLOW_CONFIG": {
            "filtered_jobs": {},
            "cache_success": ["Formatting"],
            "cache_jobs": {},
        }
    }
    _put_final(s3, "run42", "Config", rc=0, environment=env)

    state.sweep_completions()

    assert state.jobs["Config"].status == JobStatus.SUCCESS
    assert state.jobs["Formatting"].status == JobStatus.SKIPPED


def test_idempotent_for_already_finished_job():
    """A job that already transitioned (e.g. via sweep_liveness fail_dead)
    must not be moved by a final.json arriving after the fact."""
    s3 = _FakeS3()
    state = _make_state(["A"], s3, status=JobStatus.FAILURE)
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    # finish() is gated on in-flight states — FAILURE stays FAILURE.
    assert state.jobs["A"].status == JobStatus.FAILURE


def test_sweep_is_noop_in_local_mode():
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    state._s3 = None
    _put_final(s3, "run42", "A", rc=0)
    state.sweep_completions()
    assert state.jobs["A"].status == JobStatus.QUEUED


def test_normalized_job_name_in_final_key():
    s3 = _FakeS3()
    state = _make_state(["Build And Test"], s3)
    _put_final(s3, "run42", "Build And Test", rc=0)
    state.sweep_completions()
    assert state.jobs["Build And Test"].status == JobStatus.SUCCESS


def test_wait_processes_final_before_liveness(monkeypatch):
    """A landed final.json should win over a missing heartbeat in the same poll."""
    s3 = _FakeS3()
    state = _make_state(["A"], s3)
    state.jobs["A"].started_at = time.time() - (RUNNER_PICKUP_TIMEOUT_S + 30)
    state.sweep_cancel = lambda: None
    _put_final(s3, "run42", "A", rc=0)

    monkeypatch.setattr(state_mod.time, "sleep", lambda _: None)

    state.wait()

    assert state.jobs["A"].status == JobStatus.SUCCESS
    assert state.jobs["A"].rc == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
