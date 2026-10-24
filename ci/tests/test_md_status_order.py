"""The top-level check's per-job table is ordered by status: most actionable
first (failures, in-flight, waiting), with the did-not-really-run outcomes last.
"""
from test_rerun_state import _FakeS3, _make_state

from praktika.orchestrator.state import JobStatus


def test_md_status_table_sorted_by_status():
    # Deliberately scrambled insertion order.
    jobs = {
        "a_skip": JobStatus.SKIPPED,
        "b_success": JobStatus.SUCCESS,
        "c_fail": JobStatus.FAILURE,
        "d_pending": JobStatus.PENDING,
        "e_running": JobStatus.RUNNING,
        "f_cancelled": JobStatus.CANCELLED,
        "g_queued": JobStatus.QUEUED,
        "h_ready": JobStatus.READY,
    }
    state = _make_state(_FakeS3(), jobs)
    state._event = {}
    state._head_sha = "deadbeef"

    rows = [ln for ln in state.md_status().splitlines() if ln.startswith("| `")]
    order = [ln.split("|")[2].strip() for ln in rows]
    assert order == [
        "failure",
        "running",
        "queued",
        "ready",
        "pending",
        "success",
        "cancelled",
        "skipped",
    ], order


def test_md_status_table_stable_within_same_status():
    # Jobs sharing a status keep their original (DAG/insertion) order.
    jobs = {
        "z_fail": JobStatus.FAILURE,
        "a_fail": JobStatus.FAILURE,
        "m_fail": JobStatus.FAILURE,
    }
    state = _make_state(_FakeS3(), jobs)
    state._event = {}
    state._head_sha = "deadbeef"
    rows = [ln for ln in state.md_status().splitlines() if ln.startswith("| `")]
    names = [ln.split("|")[1].strip().strip("`") for ln in rows]
    assert names == ["z_fail", "a_fail", "m_fail"], names
