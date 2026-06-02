"""Tests for the orchestrator's "Finish Workflow always runs" semantics.

The behaviour is baked into two DAG rules instead of a special helper:

1. ``get_ready`` promotes any ``always_run=True`` job to READY
   as soon as every dep reaches *any* terminal state — SUCCESS, FAILURE,
   SKIPPED, or CANCELLED. Normal jobs still require deps to succeed
   (SUCCESS or SKIPPED).
2. ``cancel_unfinished_jobs`` marks PENDING and RUNNING jobs as
   CANCELLED on a cancel signal, but leaves ``always_run``
   jobs alone so they can still fire via rule 1 once their deps settle.

Run with:
    pytest ci/tests/test_finish_workflow_always_runs.py
"""
import types

from praktika.orchestrator.state import JobState, JobStatus, WorkflowState


def _make_state(*job_specs):
    """Build a WorkflowState with hand-rolled JobStates — enough surface
    for get_ready / cancel_unfinished_jobs without touching the SQS / DAG
    machinery in WorkflowState.__init__.

    Each entry is ``(name, status, deps=[], always_run=False)``.
    """
    state = WorkflowState.__new__(WorkflowState)
    state.jobs = {}
    state._deps = {}
    state._s3 = None
    for spec in job_specs:
        name, status = spec[0], spec[1]
        deps = spec[2] if len(spec) > 2 else []
        ruc = spec[3] if len(spec) > 3 else False
        js = JobState.__new__(JobState)
        js.job = types.SimpleNamespace(
            name=name,
            runs_on=[],
            requires=[],
            run_after=[],
            provides=[],
            always_run=ruc,
        )
        js.check = None
        js.status = status
        js.rc = None
        js.started_at = None
        js.finished_at = None
        js.filter_reason = None
        js._workflow_state = state
        state.jobs[name] = js
        state._deps[name] = set(deps)
    return state


def test_always_run_promoted_when_upstream_failed():
    """Finish Workflow (always_run=True) must reach READY even
    when every upstream ended in FAILURE — its post-hooks (CIDB, Slack,
    merge-ready) are exactly what's useful on a bad run."""
    state = _make_state(
        ("Build", JobStatus.FAILURE),
        ("Finish", JobStatus.PENDING, ["Build"], True),
    )
    ready = state.get_ready()
    assert [j.name for j in ready] == ["Finish"]
    assert state.jobs["Finish"].status == JobStatus.READY


def test_always_run_promoted_when_deps_mixed_terminal():
    """Finish Workflow promotes once every dep is *any* terminal state —
    SUCCESS, SKIPPED, FAILURE, or CANCELLED. Here one succeeded, one
    failed, one was cancelled."""
    state = _make_state(
        ("A", JobStatus.SUCCESS),
        ("B", JobStatus.FAILURE),
        ("C", JobStatus.CANCELLED),
        ("Finish", JobStatus.PENDING, ["A", "B", "C"], True),
    )
    ready = state.get_ready()
    assert [j.name for j in ready] == ["Finish"]


def test_always_run_waits_for_non_terminal_dep():
    """Don't fire Finish Workflow while any dep is still running."""
    state = _make_state(
        ("Build", JobStatus.RUNNING),
        ("Finish", JobStatus.PENDING, ["Build"], True),
    )
    ready = state.get_ready()
    assert ready == []
    assert state.jobs["Finish"].status == JobStatus.PENDING


def test_normal_job_cancelled_when_upstream_failed():
    """Upstream FAILURE cascades to CANCELLED (not SKIPPED) for normal
    downstream jobs — SKIPPED is reserved for Config-Workflow-filtered
    "didn't need to run" cases where outputs are still reachable from S3.
    """
    state = _make_state(
        ("Build", JobStatus.FAILURE),
        ("Tests", JobStatus.PENDING, ["Build"]),
    )
    state.get_ready()
    assert state.jobs["Tests"].status == JobStatus.CANCELLED


def test_normal_job_cancelled_when_upstream_cancelled():
    """CANCELLED upstream also cascades to CANCELLED downstream — the
    downstream job can't run either way."""
    state = _make_state(
        ("Build", JobStatus.CANCELLED),
        ("Tests", JobStatus.PENDING, ["Build"]),
    )
    state.get_ready()
    assert state.jobs["Tests"].status == JobStatus.CANCELLED


def test_cancel_unfinished_jobs_leaves_always_run_alone():
    """A cancel signal mid-run cancels PENDING and RUNNING regular jobs
    but leaves ``always_run`` ones so Finish Workflow still
    runs once its deps settle (via the subsequent get_ready call)."""
    state = _make_state(
        ("Build", JobStatus.RUNNING),
        ("Tests", JobStatus.PENDING, ["Build"]),
        ("Finish", JobStatus.PENDING, ["Tests"], True),
    )
    state.cancel_unfinished_jobs()
    assert state.jobs["Build"].status == JobStatus.CANCELLED
    assert state.jobs["Tests"].status == JobStatus.CANCELLED
    assert state.jobs["Finish"].status == JobStatus.PENDING


def test_skipped_is_success_equivalent_for_dep_resolution():
    """A SKIPPED upstream (Config Workflow filtered it out, artifact
    reachable from S3) must not block downstream."""
    state = _make_state(
        ("Build", JobStatus.SKIPPED),
        ("Tests", JobStatus.PENDING, ["Build"]),
    )
    ready = state.get_ready()
    assert [j.name for j in ready] == ["Tests"]
    assert state.jobs["Tests"].status == JobStatus.READY


def test_any_failed_ignores_skipped():
    """SKIPPED must not count as a failure for workflow-level summary."""
    state = _make_state(
        ("A", JobStatus.SUCCESS),
        ("B", JobStatus.SKIPPED),
    )
    assert state.any_failed() is False


def test_any_failed_counts_cancelled():
    """CANCELLED counts — either the run was cancelled or an upstream
    failed, both meaningful."""
    state = _make_state(
        ("A", JobStatus.SUCCESS),
        ("B", JobStatus.CANCELLED),
    )
    assert state.any_failed() is True
