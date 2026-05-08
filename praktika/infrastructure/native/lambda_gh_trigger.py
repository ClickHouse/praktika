import base64
import hashlib
import hmac
import json
import os
import time

import boto3

WEBHOOK_SECRET = os.environ.get("GH_WEBHOOK_SECRET", "")
SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "praktika-workflows")
# Bucket holding the per-run S3 prefix where the orchestrator polls for
# cancel signals. Same artifact bucket the runners use; passed via env so
# the lambda doesn't import praktika.settings.
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# Only process events from these senders (for PoC)
ALLOWED_SENDERS = {"maxknv"}
ALLOWED_PUSH_BRANCHES = {"main"}


def _get_raw_body(event) -> str:
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return body


def verify_github_signature(event) -> None:
    if not WEBHOOK_SECRET:
        print("WARNING: GH_WEBHOOK_SECRET not set, skipping signature verification")
        return

    headers = event.get("headers") or {}
    signature = headers.get("X-Hub-Signature-256") or headers.get(
        "x-hub-signature-256"
    )
    if not signature:
        raise ValueError("Missing X-Hub-Signature-256 header")

    raw_body = _get_raw_body(event)
    expected = (
        "sha256="
        + hmac.new(
            WEBHOOK_SECRET.encode("utf-8"),
            raw_body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid GitHub webhook signature")


def _build_workflow(action, payload, event_ts):
    """Build a CI workflow message from a pull_request event. Returns None to skip."""
    if action not in ("opened", "synchronize", "reopened", "rerequested"):
        return None

    pr = payload.get("pull_request", {})
    return {
        "type": "pull_request",
        "action": action,
        "event_ts": event_ts,
        "pr_number": pr.get("number"),
        "head_sha": pr.get("head", {}).get("sha", ""),
        "head_ref": pr.get("head", {}).get("ref", ""),
        "base_ref": pr.get("base", {}).get("ref", ""),
        "repo": payload.get("repository", {}).get("full_name", ""),
        "sender": payload.get("sender", {}).get("login", ""),
        "title": pr.get("title", ""),
        "draft": pr.get("draft", False),
        "labels": [l.get("name", "") for l in pr.get("labels", [])],
    }


def _build_push_workflow(payload, event_ts):
    """Build a CI workflow message from a push event. Returns None to skip
    (refs that aren't branch pushes, or branches not on the allow-list)."""
    ref = payload.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return None
    branch = ref[len("refs/heads/") :]
    if branch not in ALLOWED_PUSH_BRANCHES:
        return None
    head_sha = payload.get("after") or payload.get("head_commit", {}).get("id", "")
    if not head_sha:
        return None
    return {
        "type": "push",
        "event_ts": event_ts,
        # head_ref carries the branch the push happened on. Match the
        # PR-event shape so every downstream consumer (orchestrator
        # workflow matcher, _dispatch's task builder,
        # _build_ci_environment) reads the same field name.
        "head_ref": branch,
        "head_sha": head_sha,
        "repo": payload.get("repository", {}).get("full_name", ""),
        "sender": payload.get("sender", {}).get("login", ""),
    }


def _build_rerun_workflow(check_obj, payload, event_ts):
    """Build a rerun workflow message from a check_suite or check_run payload.

    Returns (workflow_dict, pr_number) or (None, None) if the event has no
    associated PR (e.g. a push-based check suite with no PR).
    """
    prs = check_obj.get("pull_requests", [])
    if not prs:
        return None, None
    pr = prs[0]
    pr_number = pr.get("number")
    head_sha = check_obj.get("head_sha", "")
    if not pr_number or not head_sha:
        return None, None
    workflow = {
        "type": "pull_request",
        "action": "rerequested",
        "event_ts": event_ts,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "head_ref": pr.get("head", {}).get("ref", ""),
        "base_ref": pr.get("base", {}).get("ref", ""),
        "repo": payload.get("repository", {}).get("full_name", ""),
        "sender": payload.get("sender", {}).get("login", ""),
        "title": "",
        "draft": False,
        "labels": [],
    }
    return workflow, pr_number


_sqs_queue_url = None
_sqs_client = None
_s3_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_queue_url():
    global _sqs_queue_url
    if _sqs_queue_url is None:
        _sqs_queue_url = _sqs().get_queue_url(QueueName=SQS_QUEUE_NAME)["QueueUrl"]
    return _sqs_queue_url


def _cancel_run(run_id):
    """Manual UI Cancel button: write per-run cancel-request to S3.

    The orchestrator's ``sweep_cancel`` polls this key every wait() cycle,
    sets ``state.cancelled``, and the main loop drives the rest (cancel
    PENDING/RUNNING non-always_run jobs, write the runner kill flag). The
    object is small and only its presence matters; idempotent re-write on
    duplicate webhook deliveries is harmless.
    """
    if not S3_BUCKET:
        print("  [warn] S3_BUCKET not set; cannot write cancel-request")
        return
    key = f"runs/{run_id}/cancel-request"
    try:
        _s3().put_object(Bucket=S3_BUCKET, Key=key, Body=b"requested")
        print(f"CANCEL request written: s3://{S3_BUCKET}/{key}")
    except Exception as e:
        print(f"  [warn] could not write cancel-request: {e}")


def _cancel_runs_before(pr_number, event_ts):
    """New push: write ``pr/<pr>/cancel-before`` with the new event ts.

    Every still-running orchestrator for this PR with
    ``event_ts < cancel-before`` self-cancels via ``sweep_cancel``. The
    freshly enqueued run carries the same ``event_ts`` so it stays alive
    (sweep_cancel uses strict less-than).
    """
    if not S3_BUCKET:
        print("  [warn] S3_BUCKET not set; cannot write cancel-before")
        return
    key = f"pr/{pr_number}/cancel-before"
    try:
        _s3().put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps({"ts": event_ts}).encode(),
            ContentType="application/json",
        )
        print(f"CANCEL-BEFORE written: s3://{S3_BUCKET}/{key} ts={event_ts:.0f}")
    except Exception as e:
        print(f"  [warn] could not write cancel-before: {e}")


def _enqueue(workflow, delivery_id):
    """Send a CI workflow trigger to SQS."""
    _sqs().send_message(
        QueueUrl=_get_queue_url(),
        MessageBody=json.dumps(workflow),
        MessageAttributes={
            "delivery_id": {"DataType": "String", "StringValue": delivery_id},
        },
    )
    label = workflow["type"]
    if workflow.get("action"):
        label += f".{workflow['action']}"
    target = (
        f"PR#{workflow['pr_number']}"
        if workflow.get("pr_number")
        else f"branch={workflow.get('branch', '?')}"
    )
    print(f"ENQUEUED: {label} {target} delivery={delivery_id}")


def lambda_handler(event, context):
    try:
        verify_github_signature(event)
    except Exception as e:
        print(f"Signature verification failed: {e}")
        return {"statusCode": 401, "body": "unauthorized"}

    # One ts per webhook delivery: stamped on the workflow event AND used
    # as cancel-before for older runs of the same PR. Sequential operations
    # in this invocation share the same ts so the freshly enqueued run
    # (event_ts == cancel-before-ts) does not self-cancel.
    event_ts = time.time()

    headers = event.get("headers") or {}
    gh_event = headers.get("X-GitHub-Event") or headers.get("x-github-event", "unknown")
    delivery_id = headers.get("X-GitHub-Delivery") or headers.get(
        "x-github-delivery", ""
    )

    raw_body = _get_raw_body(event)
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        payload = {}

    action = payload.get("action", "")
    sender = payload.get("sender", {}).get("login", "")

    print(f"EVENT: {gh_event}.{action}  SENDER: {sender}  DELIVERY: {delivery_id}")

    if gh_event == "check_run":
        if action == "requested_action":
            identifier = payload.get("requested_action", {}).get("identifier", "")
            if identifier == "cancel":
                check_run = payload.get("check_run", {})
                head_sha = check_run.get("head_sha", "")
                prs = check_run.get("pull_requests", [])
                pr_number = prs[0].get("number") if prs else None
                # check_run.id is the orchestrator run_id (per-run S3 prefix).
                run_id = str(check_run.get("id", "")) or None
                if run_id:
                    _cancel_run(run_id)
                    print(f"MANUAL CANCEL: PR#{pr_number} run_id={run_id} sha={head_sha[:12]}")
                else:
                    print(f"SKIP: cancel action missing check_run id")
        elif action == "rerequested":
            workflow, pr_number = _build_rerun_workflow(
                payload.get("check_run", {}), payload, event_ts
            )
            if workflow and sender in ALLOWED_SENDERS:
                # No cancel: re-run always targets the same SHA, so a
                # target_sha cancel would be consumed by the new orchestrator
                # and cancel itself. The previous run for that SHA has already
                # finished — there is nothing to cancel.
                _enqueue(workflow, delivery_id)
                print(f"RERUN (check_run): PR#{pr_number} sha={workflow['head_sha'][:12]}")
            else:
                print(f"SKIP: check_run.rerequested — no PR or sender not allowed")
        else:
            print(f"SKIP: check_run.{action} not handled")
        return {"statusCode": 200, "body": "ok"}

    if gh_event == "check_suite":
        if action == "rerequested":
            workflow, pr_number = _build_rerun_workflow(
                payload.get("check_suite", {}), payload, event_ts
            )
            if workflow and sender in ALLOWED_SENDERS:
                # No cancel: re-run targets the same SHA as before but
                # spawns a new run_id (new check run), so the new run has
                # its own S3 prefix and can't conflict with any prior run.
                _enqueue(workflow, delivery_id)
                print(f"RERUN (check_suite): PR#{pr_number} sha={workflow['head_sha'][:12]}")
            else:
                print(f"SKIP: check_suite.rerequested — no PR or sender not allowed")
        else:
            print(f"SKIP: check_suite.{action} not handled")
        return {"statusCode": 200, "body": "ok"}

    if gh_event == "push":
        if sender not in ALLOWED_SENDERS:
            print(f"SKIP: push sender {sender} not in allowed list")
            return {"statusCode": 200, "body": "ok"}
        workflow = _build_push_workflow(payload, event_ts)
        if workflow:
            _enqueue(workflow, delivery_id)
            print(
                f"PUSH: branch={workflow['head_ref']} sha={workflow['head_sha'][:12]}"
            )
        else:
            ref = payload.get("ref", "")
            print(f"SKIP: push ref {ref} not on the allow-list")
        return {"statusCode": 200, "body": "ok"}

    if gh_event != "pull_request":
        print(f"SKIP: not a pull_request event")
        return {"statusCode": 200, "body": "ok"}

    if sender not in ALLOWED_SENDERS:
        print(f"SKIP: sender {sender} not in allowed list")
        return {"statusCode": 200, "body": "ok"}

    workflow = _build_workflow(action, payload, event_ts)
    if workflow:
        # On a new push (synchronize) write pr/<pr>/cancel-before with this
        # event_ts BEFORE enqueuing the new run, so older runs see the flag
        # on their next sweep_cancel and self-cancel. The freshly enqueued
        # run carries the same event_ts and stays alive.
        if action == "synchronize":
            _cancel_runs_before(workflow["pr_number"], event_ts)
        _enqueue(workflow, delivery_id)
    else:
        print(f"SKIP: action {action} does not trigger a workflow")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "ok": True,
            "event": gh_event,
            "action": action,
            "enqueued": workflow is not None,
        }),
    }
