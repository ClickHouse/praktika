"""Top-level workflow check PATCH throttling / change-detection.

Covers the rate-limit pacing in ``orchestrator._patch_top_check``:
- the dedup signature keys off *material* state, not the rendered body (whose
  per-job Duration column ticks every poll from ``time.time()``), so an
  otherwise-idle running workflow does NOT re-PATCH every 10s;
- a real state change (a job completing) does PATCH, subject to the interval;
- ``force=True`` bypasses both dedup and interval.
"""
from unittest import mock

import praktika.orchestrator as orch


class _FakeCheck:
    def __init__(self):
        self.patches = []

    def update(self, output=None, details_url=None):
        self.patches.append(output.get("summary", ""))


class _FakeState:
    """Minimal WorkflowState stand-in: the rendered body embeds an
    ever-changing duration, but status_signature() is stable until `sig` is
    changed to model a real state transition."""

    cancelled = False

    def __init__(self, sig):
        self._sig = sig
        self._tick = 0

    def status_signature(self):
        return self._sig

    def md_status_summary(self):
        return "1 running"

    def md_status(self):
        # Volatile: a duration that advances on every render, like the real
        # md_status() Duration column.
        self._tick += 1
        return f"| job | RUNNING | {self._tick}s |"


def test_ticking_duration_does_not_repatch():
    wf = mock.Mock()
    wf.name = "W"
    check = _FakeCheck()
    state = _FakeState("A=RUNNING")
    t = {"v": 1000.0}
    with mock.patch.object(orch.time, "monotonic", lambda: t["v"]):
        orch._patch_top_check(check, wf, state, phase="running")  # first -> patches
        t["v"] += 1000  # well past any interval
        orch._patch_top_check(check, wf, state, phase="running")  # duration ticked, status same
        t["v"] += 1000
        orch._patch_top_check(check, wf, state, phase="running")
    # Only the first PATCH lands; the ticking duration must not re-PATCH.
    assert len(check.patches) == 1, check.patches


def test_status_change_repatches_after_interval():
    wf = mock.Mock()
    wf.name = "W"
    check = _FakeCheck()
    state = _FakeState("A=RUNNING")
    t = {"v": 1000.0}
    with mock.patch.object(orch.time, "monotonic", lambda: t["v"]):
        orch._patch_top_check(check, wf, state, phase="running")  # patches
        state._sig = "A=SUCCESS"  # real transition
        orch._patch_top_check(check, wf, state, phase="running")  # too soon (default 20s)
        assert len(check.patches) == 1, check.patches
        t["v"] += 1000  # past the interval
        orch._patch_top_check(check, wf, state, phase="running")  # now flushes
    assert len(check.patches) == 2, check.patches


def test_force_bypasses_dedup_and_interval():
    wf = mock.Mock()
    wf.name = "W"
    check = _FakeCheck()
    state = _FakeState("A=RUNNING")
    t = {"v": 1000.0}
    with mock.patch.object(orch.time, "monotonic", lambda: t["v"]):
        orch._patch_top_check(check, wf, state, phase="running")
        # identical state, no time advance, but forced -> patches anyway
        orch._patch_top_check(check, wf, state, phase="running", force=True)
    assert len(check.patches) == 2, check.patches


def test_forced_patch_updates_baseline():
    """A forced PATCH must advance the dedup signature + throttle window, so the
    next regular poll of the same state neither re-PATCHes nor skips the
    interval it should now be subject to."""
    wf = mock.Mock()
    wf.name = "W"
    check = _FakeCheck()
    state = _FakeState("A=RUNNING")
    t = {"v": 1000.0}
    with mock.patch.object(orch.time, "monotonic", lambda: t["v"]):
        # Only a forced PATCH, then an identical non-forced poll.
        orch._patch_top_check(check, wf, state, phase="running", force=True)
        orch._patch_top_check(check, wf, state, phase="running")  # same sig -> dedup
        assert len(check.patches) == 1, check.patches
        # A real change still waits out the interval measured from the forced PATCH.
        state._sig = "A=SUCCESS"
        t["v"] += 5
        orch._patch_top_check(check, wf, state, phase="running")  # too soon
        assert len(check.patches) == 1, check.patches
        t["v"] += 1000
        orch._patch_top_check(check, wf, state, phase="running")  # flushes
    assert len(check.patches) == 2, check.patches
