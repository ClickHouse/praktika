"""Unit tests for `VisibilityHeartbeat` — the background thread that keeps
an in-flight SQS workflow-trigger message hidden from other consumers while
`handle_workflow` is still running.

Run with:
    pytest ci/praktika/orchestrator/test_visibility_heartbeat.py
"""
import time
from unittest.mock import MagicMock

from praktika.orchestrator.orch import VisibilityHeartbeat


def _wait_for_calls(mock, n, timeout=1.0):
    """Spin until `mock` has been called at least `n` times, or `timeout`
    elapses. Avoids flaky sleeps tied to the heartbeat interval."""
    deadline = time.time() + timeout
    while time.time() < deadline and mock.call_count < n:
        time.sleep(0.01)


def test_heartbeat_extends_visibility_periodically():
    """While the heartbeat runs it keeps calling `change_message_visibility`
    with the configured visibility timeout on every tick, not just once."""
    sqs = MagicMock()
    hb = VisibilityHeartbeat(
        sqs, "q-url", "receipt-1", visibility_timeout=600, interval=0.05
    )
    hb.start()
    try:
        _wait_for_calls(sqs.change_message_visibility, 3)
    finally:
        hb.stop()

    n = sqs.change_message_visibility.call_count
    assert n >= 3, f"expected ≥3 ticks in 150 ms, got {n}"
    # Every tick uses the same message identifiers and the full timeout.
    for c in sqs.change_message_visibility.call_args_list:
        assert c.kwargs == {
            "QueueUrl": "q-url",
            "ReceiptHandle": "receipt-1",
            "VisibilityTimeout": 600,
        }


def test_context_manager_stops_the_thread():
    """After `__exit__` runs the heartbeat must be idle — no more ticks
    reach SQS, even if we wait past several intervals."""
    sqs = MagicMock()
    with VisibilityHeartbeat(sqs, "q", "r", visibility_timeout=600, interval=0.05):
        _wait_for_calls(sqs.change_message_visibility, 1)
        n_inside = sqs.change_message_visibility.call_count

    # Outside the `with` block, the thread should be joined and silent.
    time.sleep(0.2)
    assert sqs.change_message_visibility.call_count == n_inside, (
        "heartbeat kept ticking after the context manager exited"
    )


def test_heartbeat_survives_api_errors():
    """A transient network hiccup must not kill the thread — the loop has
    to keep ticking so later extensions still land."""
    sqs = MagicMock()
    sqs.change_message_visibility.side_effect = [
        RuntimeError("transient"),
        None,
        None,
        None,
    ]
    hb = VisibilityHeartbeat(sqs, "q", "r", visibility_timeout=600, interval=0.05)
    hb.start()
    try:
        _wait_for_calls(sqs.change_message_visibility, 3)
    finally:
        hb.stop()
    assert sqs.change_message_visibility.call_count >= 3, (
        "heartbeat stopped after a single API exception"
    )
