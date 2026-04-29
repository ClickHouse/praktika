#!/usr/bin/env python3
"""Per-runner-pool job polling loop.

Mirrors ``workflow_agent.py`` but on the runner side: long-poll the
per-pool SQS queue (``praktika-<runs_on>``), and for every ``job_task``
message clone the PR head, install the latest praktika wheel, and run
``praktika orchestrate job <task.json> --ci`` as a subprocess. Each PR
runs in its own clone + subprocess so a `git push` to the PR is
immediately effective without restarting this daemon.

Env:
    RUNNER_QUEUE_NAME    SQS queue this runner polls (one per runs_on label)
    AWS_DEFAULT_REGION
    INSTANCE_ID
    WORK_DIR             where PRs are cloned (default /opt/praktika/work)
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

QUEUE_NAME = os.environ.get("RUNNER_QUEUE_NAME", "")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/praktika/work")

S3_LOG_BUCKET = "praktika-artifacts-eu-north-1"
S3_LOG_PREFIX = "job-runner"

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
log = logging.getLogger("job-agent")


def _get_github_token():
    """Mint a GitHub installation token for cloning."""
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


def _resolve_praktika_install_source(clone_dir):
    """Where ``pip install`` should pull praktika from for this dispatch.

    Reads ``Settings.PRAKTIKA_INSTALL_SOURCE`` from the cloned tree using
    the praktika the agent was bootstrapped with. Three cases:

      * empty / unset / older praktika without the knob → ``None``
        (skip the per-dispatch install; reuse the bootstrap wheel)
      * URL (``http://`` / ``https://``) → return as-is for pip
      * anything else → joined onto ``clone_dir`` (relative path inside
        the PR tree, e.g. ``"."`` for the praktika repo itself)
    """
    code = (
        "from praktika.settings import Settings;"
        "print(getattr(Settings, 'PRAKTIKA_INSTALL_SOURCE', '') or '', end='')"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=clone_dir, capture_output=True, text=True,
    )
    src = res.stdout.strip() if res.returncode == 0 else ""
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        return src
    return os.path.join(clone_dir, src)


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


def handle_task(task):
    task_type = task.get("type", "unknown")
    job_name = task.get("job_name", "?")
    log.info(f"Processing task: {task_type} job={job_name!r}")

    if task_type != "job_task":
        log.info(f"Unknown task type: {task_type}, skipping")
        return {"status": "skipped", "reason": f"unknown type: {task_type}"}

    repo = task.get("repo", "")
    pr_number = task.get("pr_number")
    head_sha = task.get("head_sha", "")

    clone_dir, actual_sha = clone_repo(repo, head_sha, pr_number)

    src = _resolve_praktika_install_source(clone_dir)
    if src:
        log.info(f"Installing praktika from {src}")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall",
             src, "--break-system-packages"],
            check=True,
        )
    else:
        log.info("Reusing bootstrap praktika install")

    task_file = os.path.join(clone_dir, "ci", "tmp", "task.json")
    os.makedirs(os.path.dirname(task_file), exist_ok=True)
    with open(task_file, "w") as f:
        json.dump(task, f, indent=2)

    log.info(f"Running job {job_name!r} for PR#{pr_number}")
    result = subprocess.run(
        ["praktika", "orchestrate", "job", task_file, "--ci"],
        cwd=clone_dir,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0 and result.stderr:
        log.error(result.stderr.rstrip())

    return {
        "status": "ok" if result.returncode == 0 else "error",
        "pr": pr_number,
        "sha": actual_sha,
        "job": job_name,
        "rc": result.returncode,
        "stderr": result.stderr.strip()[:500] if result.stderr else "",
    }


def poll():
    import boto3

    if not QUEUE_NAME:
        log.error("RUNNER_QUEUE_NAME not set — cannot start runner")
        sys.exit(1)

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
            task = json.loads(msg["Body"])
            log.info(f"RECEIVED: {json.dumps(task)}")

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                result = handle_task(task)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")

            upload_log(s3, {
                "event": "task_processed",
                "instance_id": INSTANCE_ID,
                "task": task,
                "result": result,
                "time": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.exception("ERROR processing message")
            upload_log(s3, {
                "event": "task_error",
                "instance_id": INSTANCE_ID,
                "error": str(e),
                "time": datetime.now(timezone.utc).isoformat(),
            })


if __name__ == "__main__":
    poll()
