"""CI capacity throttle.

Before a workflow starts dispatching jobs, the orchestrator checks whether any
of the runner pools the workflow uses are already at max capacity. If so it holds
the *whole* workflow (dispatches nothing) and waits until every pool it needs has
headroom again — backpressure that keeps a saturated pool's SQS queue from
growing an ever-longer backlog, and makes the saturation visible on the PR's top
check from the very start of the run.

Capacity is read from a small `pool_capacity.json` snapshot that the pool
autoscaler writes to the artifact bucket once a minute (see
``infrastructure/native/lambda_pool_autoscaler.py``). The snapshot is keyed by
**queue name** — the exact identifier the orchestrator derives from a job's
``runs_on`` via ``_queue_for_runs_on`` — so this module never needs to know pool
or ASG names.

Fail-open by design: if the snapshot is absent or unreadable (throttle not
deployed, S3 blip), or a pool the workflow uses isn't in the snapshot, the
workflow dispatches immediately. The throttle can only ever *delay* a run, never
block one on missing data.
"""

import os
import time

from ..settings import Settings
from .state import _queue_for_runs_on

# Hardcoded on both writer (lambda) and reader (here); see
# ``lambda_pool_autoscaler.CAPACITY_S3_KEY``.
CAPACITY_S3_KEY = "pool_capacity.json"


def _workflow_queues(workflow):
    """The set of runner-pool queue names a workflow's jobs dispatch to.

    Jobs with no mappable ``runs_on`` (e.g. only the ``self-hosted`` label, or a
    GitHub-hosted label with no praktika queue) contribute nothing — there is no
    ASG pool to throttle on."""
    queues = set()
    for job in getattr(workflow, "jobs", None) or ():
        queue = _queue_for_runs_on(getattr(job, "runs_on", None))
        if queue:
            queues.add(queue)
    return queues


def load_capacity_snapshot():
    """Read the pool-capacity snapshot from S3. Returns the ``pools`` dict
    (keyed by queue name), or None on any failure — a None result makes the
    caller fail open."""
    bucket = (getattr(Settings, "S3_ARTIFACT_BUCKET", "") or "").strip()
    if not bucket:
        return None
    import boto3

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    # A bucket may carry a key prefix ("bucket/prefix"); mirror the writer's
    # split so Bucket= is a bare name.
    bucket_name, _, prefix = bucket.partition("/")
    key = f"{prefix.rstrip('/')}/{CAPACITY_S3_KEY}" if prefix else CAPACITY_S3_KEY
    try:
        obj = boto3.client("s3", region_name=region).get_object(
            Bucket=bucket_name, Key=key
        )
        payload = obj["Body"].read()
        import json

        data = json.loads(payload)
    except Exception as e:
        print(f"  [warn] capacity snapshot unavailable, skipping throttle: {type(e).__name__}: {e}")
        return None
    pools = data.get("pools") if isinstance(data, dict) else None
    return pools if isinstance(pools, dict) else {}


def saturated_pools(queues, snapshot):
    """Among ``queues`` (queue names), the ones the snapshot marks saturated.

    A pool absent from the snapshot is treated as having headroom (fail-open):
    we only ever hold on positive evidence of saturation. Returns a list of
    ``{queue, pool, backlog, max}`` dicts, sorted for a stable check message."""
    out = []
    for queue in queues:
        info = snapshot.get(queue)
        if not isinstance(info, dict) or not info.get("saturated"):
            continue
        visible = int(info.get("visible", 0) or 0)
        in_flight = int(info.get("in_flight", 0) or 0)
        out.append(
            {
                "queue": queue,
                "pool": info.get("pool_name") or queue,
                "backlog": visible + in_flight,
                "max": int(info.get("max", 0) or 0),
            }
        )
    out.sort(key=lambda p: p["pool"])
    return out


def format_throttle_message(saturated):
    """One-line, check-friendly summary of which pools are full (and how full)."""
    pools = ", ".join(f"`{p['pool']}` ({p['backlog']}/{p['max']})" for p in saturated)
    noun = "runner pool" if len(saturated) == 1 else "runner pools"
    return f"Waiting for CI capacity — {noun} at max: {pools}"


def wait_for_capacity(workflow, on_throttle=None, state=None):
    """Block until none of ``workflow``'s runner pools are at max capacity.

    ``on_throttle(message, saturated)`` is invoked once per poll while the
    workflow is held, so the caller can surface the saturation on the top check.
    Returns True if the run was held at all, False if it was clear immediately.

    No-op (returns False) when the workflow uses no throttleable pools. Fails
    open on a missing/unreadable snapshot.

    ``state`` (a WorkflowState) is swept for cancellation each poll: a manual
    cancel or a newer push sets ``state.cancelled`` and ends the hold, so the
    caller returns into ``_drive_dag`` with the flag already set — it cancels the
    unfinished jobs before its first dispatch rather than kicking them.
    """
    queues = _workflow_queues(workflow)
    if not queues:
        return False

    poll_interval = int(getattr(Settings, "CI_CAPACITY_POLL_INTERVAL_S", 60) or 60)
    held = False
    while True:
        # Stop holding the moment a cancel lands, before re-reading capacity, so
        # a cancelled run never waits for capacity it will not use.
        if state is not None:
            state.sweep_cancel()
            if getattr(state, "cancelled", False):
                return held
        snapshot = load_capacity_snapshot()
        if snapshot is None:
            return held  # fail open
        saturated = saturated_pools(queues, snapshot)
        if not saturated:
            return held
        held = True
        message = format_throttle_message(saturated)
        print(f"  [throttle] {message}")
        if on_throttle is not None:
            try:
                on_throttle(message, saturated)
            except Exception as e:
                print(f"  [warn] throttle check patch failed: {type(e).__name__}: {e}")
        time.sleep(poll_interval)
