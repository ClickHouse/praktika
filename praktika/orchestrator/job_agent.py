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
REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or ""
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


class CancelWatchdog:
    """Kill a subprocess if the per-run S3 cancel flag appears."""

    def __init__(self, s3_client, bucket, key, proc, interval=10):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._proc = proc
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="cancel-watchdog")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._s3.head_object(Bucket=self._bucket, Key=self._key)
                log.info(f"Cancel flag found at s3://{self._bucket}/{self._key} — killing job")
                self._proc.kill()
                return
            except Exception:
                pass  # flag not present yet, or transient S3 error


class Heartbeat:
    """Periodically write {ts, status, step} to the per-job S3 heartbeat key.

    The orchestrator reads this key in ``WorkflowState.sweep_liveness`` to
    decide whether the runner is still alive. A missed write (transient
    S3 error) is benign as long as one lands within
    ``HEARTBEAT_DEAD_THRESHOLD_S`` on the orchestrator side.
    """

    def __init__(self, s3_client, bucket, key, interval, status="running"):
        self._s3 = s3_client
        self._bucket = bucket
        self._key = key
        self._interval = max(1, int(interval or 30))
        self._status = status
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        # Post immediately on start so the orchestrator sees a fresh ts
        # well before the dead threshold elapses on slow first-cycle paths.
        self._beat()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _beat(self):
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=json.dumps({"ts": time.time(), "status": self._status}).encode(),
                ContentType="application/json",
            )
        except Exception as e:
            log.warning(f"heartbeat put failed: {type(e).__name__}: {e}")

    def _run(self):
        while not self._stop.wait(self._interval):
            self._beat()


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
    """Read PRAKTIKA_INSTALL_SOURCE directly from settings/settings.py.

    No praktika install required — uses importlib on the raw file.
    Returns the pip install source, or None to skip the per-dispatch install.
    """
    import importlib.util
    settings_file = os.path.join(clone_dir, "ci", "settings", "settings.py")
    if not os.path.exists(settings_file):
        return None
    try:
        spec = importlib.util.spec_from_file_location("repo_settings", settings_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        src = getattr(mod, "PRAKTIKA_INSTALL_SOURCE", "") or ""
    except Exception as e:
        log.warning(f"Could not read PRAKTIKA_INSTALL_SOURCE from {settings_file}: {e}")
        return None
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


def clone_repo(repo, head_sha, pr_number, token):
    """Clone repo at head_sha into a per-task work dir.

    Job tasks carry a ``head_sha`` regardless of whether the originating
    event was a PR or a push, so fetch the SHA directly when there's no
    pr_number to drive a ``refs/pull/<n>/head`` fetch.
    """
    if pr_number:
        clone_dir = os.path.join(WORK_DIR, f"pr-{pr_number}")
    else:
        clone_dir = os.path.join(WORK_DIR, f"push-{head_sha[:12] or 'unknown'}")
    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)
    os.makedirs(clone_dir, exist_ok=True)

    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    target = f"PR#{pr_number}" if pr_number else f"sha {head_sha[:12]}"
    log.info(f"Cloning {repo} {target}")

    _git(["init", clone_dir])
    _git(["remote", "add", "origin", clone_url], cwd=clone_dir)
    if pr_number:
        _git(["fetch", "--depth=1", "origin", f"+refs/pull/{pr_number}/head:refs/heads/pr-head"], cwd=clone_dir)
        _git(["checkout", "pr-head"], cwd=clone_dir)
    else:
        _git(["fetch", "--depth=1", "origin", head_sha], cwd=clone_dir)
        _git(["checkout", head_sha], cwd=clone_dir)

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

    # Mint a fresh GitHub App installation token (≈1h validity), use it
    # for cloning, and persist it via `gh auth login --with-token` so the
    # orchestrate-job subprocess and everything it spawns inherit
    # authenticated `gh` CLI state.
    gh_token = _get_github_token()
    subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=gh_token, text=True, check=True,
    )

    clone_dir, actual_sha = clone_repo(repo, head_sha, pr_number, gh_token)

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

    cancel_s3_bucket = task.get("cancel_s3_bucket", "")
    cancel_s3_key = task.get("cancel_s3_key", "")
    heartbeat_s3_bucket = task.get("heartbeat_s3_bucket", "")
    heartbeat_s3_key = task.get("heartbeat_s3_key", "")
    heartbeat_interval_s = task.get("heartbeat_interval_s", 30)

    log.info(f"Running job {job_name!r} for PR#{pr_number}")
    import boto3
    s3 = boto3.client("s3", region_name=REGION)
    proc = subprocess.Popen(
        ["praktika", "orchestrate", "job", task_file, "--ci"],
        cwd=clone_dir,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Cancel watchdog and heartbeat run side-by-side: one signals
    # orchestrator → job (kill), the other signals job → orchestrator
    # (still alive). A missing heartbeat key in the task (older orchestrator
    # talking to a newer agent) skips the heartbeat thread silently.
    cm_cancel = CancelWatchdog(s3, cancel_s3_bucket, cancel_s3_key, proc)
    cm_heartbeat = (
        Heartbeat(s3, heartbeat_s3_bucket, heartbeat_s3_key, heartbeat_interval_s)
        if heartbeat_s3_bucket and heartbeat_s3_key
        else None
    )
    with cm_cancel:
        if cm_heartbeat is not None:
            cm_heartbeat.start()
        try:
            _, stderr_output = proc.communicate()
        finally:
            if cm_heartbeat is not None:
                cm_heartbeat.stop()
    rc = proc.returncode

    if rc != 0 and stderr_output:
        log.error(stderr_output.rstrip())

    return {
        "status": "ok" if rc == 0 else "error",
        "pr": pr_number,
        "sha": actual_sha,
        "job": job_name,
        "rc": rc,
        "stderr": stderr_output.strip()[:500] if stderr_output else "",
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
