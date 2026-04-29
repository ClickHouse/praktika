#!/usr/bin/env python3
"""Orchestrator polling loop.

Deployed to EC2 via user_data_orchestrator.sh. For each SQS message:
  1. Clone the PR head
  2. pip install --force-reinstall praktika (picks up latest package)
  3. Run `praktika orchestrate workflow <event.json> --ci` as a subprocess

Local use (no SQS): run step 3 directly:
    praktika orchestrate workflow path/to/event.json --ci
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

QUEUE_NAME = os.environ.get("SQS_QUEUE_NAME", "praktika-workflows")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/praktika/work")

S3_LOG_BUCKET = "praktika-artifacts-eu-north-1"
S3_LOG_PREFIX = "/workflow-orchestrator"

PRAKTIKA_WHL = (
    "https://praktika-artifacts-eu-north-1.s3.amazonaws.com"
    "/packages/praktika-0.1-py3-none-any.whl"
)

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{INSTANCE_ID}] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("orch")


def _get_github_token():
    """Mint a GitHub installation token for cloning (uses boto3 directly)."""
    import jwt
    import requests as _requests

    sm = __import__("boto3").client("secretsmanager", region_name=REGION)
    secret = json.loads(
        sm.get_secret_value(SecretId="praktika-gh-app")["SecretString"]
    )
    app_id = secret["app-id"]
    app_key = secret["app-key"]
    installation_id = secret["app-installation-id"]

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    jwt_token = jwt.encode(payload, app_key, algorithm="RS256")

    resp = _requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


class VisibilityHeartbeat:
    """Extend SQS message visibility while we process it."""

    def __init__(self, sqs_client, queue_url, receipt_handle, visibility_timeout, interval=None):
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._receipt = receipt_handle
        self._visibility = visibility_timeout
        self._interval = interval or max(30, visibility_timeout * 6 // 10)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="sqs-heartbeat")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt,
                    VisibilityTimeout=self._visibility,
                )
            except Exception as e:
                log.warning(f"change_message_visibility failed: {type(e).__name__}: {e}")


def _git(args, cwd=None):
    result = subprocess.run(
        ["git", *(["-C", cwd] if cwd else []), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


def clone_repo(repo, head_sha, pr_number):
    clone_dir = os.path.join(WORK_DIR, f"pr-{pr_number}")
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)
    os.makedirs(clone_dir, exist_ok=True)

    token = _get_github_token()
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    log.info(f"Cloning {repo} PR#{pr_number} at {head_sha[:12]}")

    _git(["init", clone_dir])
    _git(["remote", "add", "origin", clone_url], cwd=clone_dir)
    _git(["fetch", "--depth=1", "origin", f"+refs/pull/{pr_number}/head:refs/heads/pr-head"], cwd=clone_dir)
    _git(["checkout", "pr-head"], cwd=clone_dir)

    actual_sha = _git(["rev-parse", "HEAD"], cwd=clone_dir).strip()
    log.info(f"Checked out {actual_sha[:12]} in {clone_dir}")
    return clone_dir, actual_sha


def upload_log(s3, message):
    now = datetime.now(timezone.utc)
    key = f"{S3_LOG_PREFIX}/{now:%Y-%m-%d}/{INSTANCE_ID}/{now:%H-%M-%S-%f}.json"
    s3.put_object(
        Bucket=S3_LOG_BUCKET,
        Key=key,
        Body=json.dumps(message, indent=2),
        ContentType="application/json",
    )
    log.info(f"Log uploaded to s3://{S3_LOG_BUCKET}/{key}")


def handle_workflow(event):
    wf_type = event.get("type", "unknown")
    log.info(f"Processing: {wf_type}")

    if wf_type != "pull_request":
        log.info(f"Unknown event type: {wf_type}, skipping")
        return {"status": "skipped", "reason": f"unknown type: {wf_type}"}

    repo = event.get("repo", "")
    pr_number = event.get("pr_number")
    head_sha = event.get("head_sha", "")

    clone_dir, actual_sha = clone_repo(repo, head_sha, pr_number)

    log.info(f"Installing praktika from {PRAKTIKA_WHL}")
    subprocess.run(
        ["python3.12", "-m", "pip", "install", "--force-reinstall",
         PRAKTIKA_WHL, "--break-system-packages"],
        check=True,
    )

    event_file = os.path.join(clone_dir, "ci", "tmp", "event.json")
    os.makedirs(os.path.dirname(event_file), exist_ok=True)
    with open(event_file, "w") as f:
        json.dump(event, f, indent=2)

    log.info(f"Running orchestrator for PR#{pr_number}")
    result = subprocess.run(
        ["praktika", "orchestrate", "workflow", event_file, "--ci"],
        cwd=clone_dir,
    )

    return {
        "status": "ok" if result.returncode == 0 else "error",
        "pr": pr_number,
        "sha": actual_sha,
        "rc": result.returncode,
    }


def poll():
    import boto3

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    visibility = int(
        sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )

    upload_log(s3, {
        "event": "startup",
        "instance_id": INSTANCE_ID,
        "queue": QUEUE_NAME,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    log.info(f"Polling {queue_url} (visibility_timeout={visibility}s)")

    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
        )
        messages = resp.get("Messages", [])
        if not messages:
            continue

        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        try:
            event = json.loads(msg["Body"])
            log.info(f"RECEIVED: {json.dumps(event)}")

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                result = handle_workflow(event)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")

            upload_log(s3, {
                "event": "workflow_processed",
                "instance_id": INSTANCE_ID,
                "trigger": event,
                "result": result,
                "time": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.exception("ERROR processing message")
            upload_log(s3, {
                "event": "workflow_error",
                "instance_id": INSTANCE_ID,
                "error": str(e),
                "time": datetime.now(timezone.utc).isoformat(),
            })


if __name__ == "__main__":
    poll()
