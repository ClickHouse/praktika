import base64
import hashlib
import hmac
import json
import os

import boto3

WEBHOOK_SECRET = os.environ.get("GH_WEBHOOK_SECRET", "")
SQS_QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "praktika-workflows")

# Only process events from these senders (for PoC)
ALLOWED_SENDERS = {"maxknv"}


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


def _build_workflow(action, payload):
    """Build a CI workflow message from a pull_request event. Returns None to skip."""
    if action not in ("opened", "synchronize", "reopened", "rerequested"):
        return None

    pr = payload.get("pull_request", {})
    return {
        "type": "pull_request",
        "action": action,
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


def _build_rerun_workflow(check_obj, payload):
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


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_queue_url():
    global _sqs_queue_url
    if _sqs_queue_url is None:
        _sqs_queue_url = _sqs().get_queue_url(QueueName=SQS_QUEUE_NAME)["QueueUrl"]
    return _sqs_queue_url


def _run_queue_prefix(pr_number):
    return f"praktika-wf-{pr_number}-"


def _cancel_run(pr_number, run_id):
    """Send a cancel to exactly one run via its per-run queue.

    Used for the UI Cancel button: the webhook payload carries the check
    run ID, which is the run_id and the queue suffix, so we can address
    the run directly — no filtering needed on the orchestrator side.
    """
    queue_name = f"{_run_queue_prefix(pr_number)}{run_id}"
    try:
        url = _sqs().get_queue_url(QueueName=queue_name)["QueueUrl"]
    except Exception as e:
        if "NonExistentQueue" in str(e):
            print(f"  [skip] run queue {queue_name} does not exist; nothing to cancel")
        else:
            print(f"  [warn] could not find run queue {queue_name}: {e}")
        return
    try:
        _sqs().send_message(QueueUrl=url, MessageBody=json.dumps({"type": "cancel"}))
        print(f"CANCEL sent to {queue_name}")
    except Exception as e:
        print(f"  [warn] could not send cancel to {queue_name}: {e}")


def _cancel_all_runs(pr_number):
    """Fan out a cancel to every live per-run queue for this PR.

    Used on ``synchronize``: we don't know the old run_ids, but every live
    orchestrator for this PR owns exactly one queue matching the per-PR
    prefix, so listing by prefix enumerates the cancel targets. A freshly
    pushed run hasn't created its queue yet (it's enqueued after this call),
    so it is naturally excluded from the fan-out.
    """
    prefix = _run_queue_prefix(pr_number)
    try:
        resp = _sqs().list_queues(QueueNamePrefix=prefix)
    except Exception as e:
        print(f"  [warn] list_queues({prefix}) failed: {e}")
        return
    urls = resp.get("QueueUrls", [])
    if not urls:
        print(f"PR#{pr_number}: no live runs to cancel")
        return
    for url in urls:
        queue_name = url.rsplit("/", 1)[-1]
        try:
            _sqs().send_message(QueueUrl=url, MessageBody=json.dumps({"type": "cancel"}))
            print(f"CANCEL sent to {queue_name}")
        except Exception as e:
            print(f"  [warn] could not send cancel to {queue_name}: {e}")


def _enqueue(workflow, delivery_id):
    """Send a CI workflow trigger to SQS."""
    _sqs().send_message(
        QueueUrl=_get_queue_url(),
        MessageBody=json.dumps(workflow),
        MessageAttributes={
            "delivery_id": {"DataType": "String", "StringValue": delivery_id},
        },
    )
    print(f"ENQUEUED: {workflow['type']}.{workflow['action']} PR#{workflow['pr_number']} delivery={delivery_id}")


def lambda_handler(event, context):
    try:
        verify_github_signature(event)
    except Exception as e:
        print(f"Signature verification failed: {e}")
        return {"statusCode": 401, "body": "unauthorized"}

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
                # The check run ID is the run_id and the per-run queue
                # suffix — address the run's queue directly.
                run_id = str(check_run.get("id", "")) or None
                if pr_number and run_id:
                    _cancel_run(pr_number, run_id)
                    print(f"MANUAL CANCEL: PR#{pr_number} run_id={run_id} sha={head_sha[:12]}")
                else:
                    print(f"SKIP: cancel action missing pr_number or check_run id")
        elif action == "rerequested":
            workflow, pr_number = _build_rerun_workflow(
                payload.get("check_run", {}), payload
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
                payload.get("check_suite", {}), payload
            )
            if workflow and sender in ALLOWED_SENDERS:
                # No cancel: re-run targets the same SHA as before but
                # spawns a new run_id (new check run), so the new run has
                # its own queue and can't conflict with any prior run.
                _enqueue(workflow, delivery_id)
                print(f"RERUN (check_suite): PR#{pr_number} sha={workflow['head_sha'][:12]}")
            else:
                print(f"SKIP: check_suite.rerequested — no PR or sender not allowed")
        else:
            print(f"SKIP: check_suite.{action} not handled")
        return {"statusCode": 200, "body": "ok"}

    if gh_event != "pull_request":
        print(f"SKIP: not a pull_request event")
        return {"statusCode": 200, "body": "ok"}

    if sender not in ALLOWED_SENDERS:
        print(f"SKIP: sender {sender} not in allowed list")
        return {"statusCode": 200, "body": "ok"}

    workflow = _build_workflow(action, payload)
    if workflow:
        # On a new push to an existing PR fan out a cancel to every live
        # per-run queue for the PR before enqueuing the new run. The new
        # run hasn't created its queue yet, so it isn't hit by this fan-out.
        if action == "synchronize":
            _cancel_all_runs(workflow["pr_number"])
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
