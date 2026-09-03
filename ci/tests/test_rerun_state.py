"""Tests for the partial re-run state machinery on WorkflowState:
apply_rerun (reset a failed job + its failed downstream), the state.json
snapshot round-trip (save_snapshot / seed_from_snapshot), and sweep_rerun
(apply + consume S3 re-run requests)."""
import io
import json
import types

from praktika.orchestrator.state import JobState, JobStatus, WorkflowState


class _FakeS3:
    def __init__(self):
        self.store = {}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.store[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.store:
            raise KeyError(f"NoSuchKey {Key}")
        return {"Body": io.BytesIO(self.store[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.store.pop((Bucket, Key), None)

    def list_objects_v2(self, Bucket, Prefix):  # noqa: N803
        return {
            "Contents": [
                {"Key": k} for (b, k) in self.store if b == Bucket and k.startswith(Prefix)
            ]
        }


def _job(name, always_run=False):
    return types.SimpleNamespace(
        name=name, runs_on=[], requires=[], run_after=[], provides=[], always_run=always_run
    )


def _make_state(s3, statuses, always_run=()):
    """Build a WorkflowState for the linear DAG A -> B -> C with given statuses."""
    state = WorkflowState.__new__(WorkflowState)
    state.workflow = types.SimpleNamespace(name="PR")
    state.local_mode = False
    state._s3 = s3
    state._cancel_s3_bucket = "test-bucket"
    state._run_id = "run42"
    state._runs_s3_prefix = "runs/run42"
    state._state_s3_key = "runs/run42/state.json"
    state._rerun_request_prefix = "runs/run42/rerun-request/"
    state._repo = "owner/repo"
    state._head_sha = "a" * 40
    state._pr_number = 7
    state._event_ts = 1000.0
    state._environment = {"WORKFLOW_CONFIG": {}}
    state._gh_token = None  # can_post_checks False -> no check API calls
    state.jobs = {}
    for name, status in statuses.items():
        js = JobState.__new__(JobState)
        js.job = _job(name, always_run=name in always_run)
        js.check = None
        js.status = status
        js.rc = 0 if status == JobStatus.SUCCESS else (1 if status == JobStatus.FAILURE else None)
        js.non_blocking = False
        js.result = None
        js.started_at = None
        js.finished_at = None
        js.last_heartbeat_ts = None
        js.runner_instance_id = None
        js.last_heartbeat_phase = None
        js.attempt = 1
        js.stale_flagged = False
        js.filter_reason = None
        js.rerun_count = 0
        js._workflow_state = state
        state.jobs[name] = js
    state._deps = {"A": (), "B": ("A",), "C": ("B",)}
    state._dependents = {"A": {"B"}, "B": {"C"}}
    return state


def test_apply_rerun_resets_job_and_failed_downstream():
    s3 = _FakeS3()
    # A failed; its downstream cascade-cancelled.
    state = _make_state(
        s3,
        {"A": JobStatus.FAILURE, "B": JobStatus.CANCELLED, "C": JobStatus.CANCELLED},
    )
    reset = state.apply_rerun(["A"])
    assert reset == {"A", "B", "C"}
    assert all(state.jobs[n].status == JobStatus.PENDING for n in ("A", "B", "C"))
    # Reset bumps the per-job re-run counter (surfaced in the check output).
    assert state.jobs["A"].rerun_count == 1
    # A second re-run only counts once the job has finished again (the guard
    # skips a job still mid-re-run).
    state.jobs["A"].status = JobStatus.FAILURE
    state.apply_rerun(["A"])
    assert state.jobs["A"].rerun_count == 2


def test_apply_rerun_skips_job_already_rerunning():
    # A job that is not terminal (already reset / re-running) must not be reset
    # again — otherwise a lingering rerun-request would runaway rerun_count.
    s3 = _FakeS3()
    state = _make_state(s3, {"A": JobStatus.PENDING, "B": JobStatus.SUCCESS, "C": JobStatus.SUCCESS})
    reset = state.apply_rerun(["A"])
    assert reset == set()
    assert state.jobs["A"].rerun_count == 0


def test_apply_rerun_leaves_successful_downstream_alone():
    s3 = _FakeS3()
    # B succeeded (e.g. A was a non-blocking failure); it must not be reset.
    state = _make_state(
        s3,
        {"A": JobStatus.FAILURE, "B": JobStatus.SUCCESS, "C": JobStatus.SUCCESS},
    )
    reset = state.apply_rerun(["A"])
    assert reset == {"A"}
    assert state.jobs["A"].status == JobStatus.PENDING
    assert state.jobs["B"].status == JobStatus.SUCCESS
    assert state.jobs["C"].status == JobStatus.SUCCESS


def test_apply_rerun_resets_successful_always_run_downstream():
    s3 = _FakeS3()
    # C is always_run (e.g. Finish Workflow): it succeeds even though A failed,
    # so it must still be reset so its aggregate/merge-readiness re-runs.
    state = _make_state(
        s3,
        {"A": JobStatus.FAILURE, "B": JobStatus.CANCELLED, "C": JobStatus.SUCCESS},
        always_run=("C",),
    )
    reset = state.apply_rerun(["A"])
    assert reset == {"A", "B", "C"}
    assert state.jobs["C"].status == JobStatus.PENDING


def test_reset_deletes_stale_final_json_and_heartbeat():
    s3 = _FakeS3()
    s3.put_object("test-bucket", "runs/run42/A/final.json", b"{}")
    s3.put_object("test-bucket", "runs/run42/A/heartbeat.json", b"{}")
    state = _make_state(s3, {"A": JobStatus.FAILURE, "B": JobStatus.SUCCESS, "C": JobStatus.SUCCESS})
    state.apply_rerun(["A"])
    assert ("test-bucket", "runs/run42/A/final.json") not in s3.store
    assert ("test-bucket", "runs/run42/A/heartbeat.json") not in s3.store


def test_snapshot_roundtrip():
    s3 = _FakeS3()
    state = _make_state(
        s3, {"A": JobStatus.SUCCESS, "B": JobStatus.FAILURE, "C": JobStatus.CANCELLED}
    )
    state.jobs["B"].rc = 1
    state.jobs["B"].rerun_count = 2
    state.save_snapshot(finalized=True)

    raw = s3.store[("test-bucket", "runs/run42/state.json")]
    snap = json.loads(raw)
    assert snap["finalized"] is True
    assert snap["workflow_name"] == "PR"
    assert snap["jobs"]["A"]["status"] == "success"

    fresh = _make_state(s3, {"A": JobStatus.PENDING, "B": JobStatus.PENDING, "C": JobStatus.PENDING})
    fresh.seed_from_snapshot(snap)
    assert fresh.jobs["A"].status == JobStatus.SUCCESS
    assert fresh.jobs["B"].status == JobStatus.FAILURE
    assert fresh.jobs["C"].status == JobStatus.CANCELLED
    assert fresh.jobs["B"].rerun_count == 2
    assert fresh._environment == {"WORKFLOW_CONFIG": {}}


def test_sweep_rerun_applies_and_consumes_requests():
    s3 = _FakeS3()
    s3.put_object(
        "test-bucket", "runs/run42/rerun-request/d1.json", json.dumps({"jobs": ["A"]}).encode()
    )
    state = _make_state(
        s3,
        {"A": JobStatus.FAILURE, "B": JobStatus.CANCELLED, "C": JobStatus.CANCELLED},
    )
    changed = state.sweep_rerun()
    assert changed is True
    assert state.jobs["A"].status == JobStatus.PENDING
    assert state.jobs["C"].status == JobStatus.PENDING
    # request consumed
    assert ("test-bucket", "runs/run42/rerun-request/d1.json") not in s3.store
    # snapshot written after reset
    assert ("test-bucket", "runs/run42/state.json") in s3.store


def test_clear_stale_cancel_deletes_markers():
    # A resume of a cancelled run must drop the old cancel-request + kill flag,
    # else the reset job is cancelled immediately.
    s3 = _FakeS3()
    s3.put_object("test-bucket", "runs/run42/cancel-request", b"requested")
    s3.put_object("test-bucket", "runs/run42/cancel", b"cancelled")
    state = _make_state(s3, {"A": JobStatus.FAILURE, "B": JobStatus.SUCCESS, "C": JobStatus.SUCCESS})
    state._cancel_request_s3_key = "runs/run42/cancel-request"
    state._cancel_s3_key = "runs/run42/cancel"
    state.cancelled = True

    state.clear_stale_cancel()

    assert state.cancelled is False
    assert ("test-bucket", "runs/run42/cancel-request") not in s3.store
    assert ("test-bucket", "runs/run42/cancel") not in s3.store


def test_sweep_rerun_no_requests_is_noop():
    s3 = _FakeS3()
    state = _make_state(s3, {"A": JobStatus.SUCCESS, "B": JobStatus.SUCCESS, "C": JobStatus.SUCCESS})
    assert state.sweep_rerun() is False
