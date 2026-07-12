"""Runner queue routing from a job's runs_on labels.

The praktika engine routes a job to ``<project-slug>-<label>``, ignoring the
GitHub-Actions "self-hosted" runner-group label (which has no meaning here).
"""

from praktika.orchestrator import state
from praktika.settings import Settings


def test_queue_for_runs_on_skips_self_hosted(monkeypatch):
    monkeypatch.setattr(Settings, "PROJECT_SLUG", "clickhouse")

    # ["self-hosted", "<pool>"] routes to the pool label, not "self-hosted".
    assert (
        state._queue_for_runs_on(["self-hosted", "style-checker-aarch64"])
        == "clickhouse-style-checker-aarch64"
    )
    # Bare single label still works.
    assert state._queue_for_runs_on(["arm-2xsmall"]) == "clickhouse-arm-2xsmall"
    # self-hosted only / empty → no queue.
    assert state._queue_for_runs_on(["self-hosted"]) is None
    assert state._queue_for_runs_on([]) is None
