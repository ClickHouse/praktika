#!/usr/bin/env python3
"""CI engine runner: polls SQS for workflow triggers, clones the repo, runs
the orchestrator, and reports status to GitHub via the Checks API.

This same file is deployed to EC2 by ``user_data_orchestrator.sh`` (the shell
script substitutes its body in at deploy time), so there is a single source
of truth for both local and production.

Usage:
    # Local test (no AWS required):
    python3 ci/praktika/orchestrator/run.py --local tmp/sandbox/test_message.json

    # EC2 mode (polls SQS):
    python3 ci/praktika/orchestrator/run.py

Environment:
    SQS_QUEUE_NAME, AWS_DEFAULT_REGION, INSTANCE_ID, WORK_DIR
    CI_ENGINE_POST_CHECKS=1  -- post GitHub check runs from --local mode
                                (always on in SQS polling mode)
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

#TODO: remove hardcoded bucket names
S3_LOG_BUCKET = "praktika-artifacts-eu-north-1"
S3_LOG_PREFIX = "/workflow-orchestrator"

GH_APP_SECRET = "praktika-gh-app"

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{INSTANCE_ID}] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("orch")


def get_github_token():
    """Mint a GitHub installation token via the praktika GitHub App."""
    import jwt
    import requests

    sm = __import__("boto3").client("secretsmanager", region_name=REGION)
    # Single JSON secret with keys: app-key (PEM), app-installation-id, app-id
    secret = json.loads(sm.get_secret_value(SecretId=GH_APP_SECRET)["SecretString"])
    app_id = secret["app-id"]
    app_key = secret["app-key"]
    installation_id = secret["app-installation-id"]

    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    jwt_token = jwt.encode(payload, app_key, algorithm="RS256")

    resp = requests.post(
        f"https://api.github.com/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["token"]


class CheckRun:
    """A GitHub check run: created with status=in_progress, completed later."""

    @staticmethod
    def _api(method, url, token, json_body=None):
        import requests

        resp = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json_body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    @classmethod
    def start(cls, token, repo, head_sha, name):
        data = cls._api(
            "POST",
            f"https://api.github.com/repos/{repo}/check-runs",
            token,
            {
                "name": name,
                "head_sha": head_sha,
                "status": "in_progress",
                "actions": [
                    {
                        "label": "Cancel",
                        "description": "Cancel this CI run",
                        "identifier": "cancel",
                    }
                ],
            },
        )
        return cls(token, repo, data["id"], name)

    def __init__(self, token, repo, id, name):
        self.token = token
        self.repo = repo
        self.id = id
        self.name = name

    def complete(self, conclusion, output=None):
        body = {"status": "completed", "conclusion": conclusion}
        if output is not None:
            body["output"] = output
        self._api(
            "PATCH",
            f"https://api.github.com/repos/{self.repo}/check-runs/{self.id}",
            self.token,
            body,
        )

    def update(self, output=None, details_url=None):
        """PATCH the check run without marking it completed.

        Used by the orchestrator to refresh the top-level workflow check's
        description on every state change, so the PR UI reflects live
        progress (per-status counts + per-job table).
        """
        body = {}
        if output is not None:
            body["output"] = output
        if details_url is not None:
            body["details_url"] = details_url
        if not body:
            return
        self._api(
            "PATCH",
            f"https://api.github.com/repos/{self.repo}/check-runs/{self.id}",
            self.token,
            body,
        )


class VisibilityHeartbeat:
    """Keep an SQS message invisible to other consumers while we process it.

    SQS makes a received message visible to other consumers after
    ``VisibilityTimeout`` seconds unless the consumer either deletes the
    message or calls ``change_message_visibility`` to extend the deadline.
    For a workflow that takes longer than the visibility timeout the message
    is silently re-delivered to another consumer mid-run — producing a
    duplicate orchestrator run for the same trigger.

    This class runs a background thread that re-extends the message's
    visibility every ``interval`` seconds (default: 60% of the visibility
    timeout, leaving margin in case a tick is delayed). The thread stops
    on ``.stop()`` or on exiting the context manager. API failures inside
    the loop are logged and swallowed — a transient network hiccup must
    not abort the heartbeat.

    Usage::

        with VisibilityHeartbeat(sqs, queue_url, receipt, visibility_timeout=600):
            handle_workflow(...)
            sqs.delete_message(...)
    """

    def __init__(self, sqs_client, queue_url, receipt_handle, visibility_timeout, interval=None):
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._receipt = receipt_handle
        self._visibility = visibility_timeout
        if interval is None:
            interval = max(30, visibility_timeout * 6 // 10)
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sqs-visibility-heartbeat"
        )
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
        # Event.wait returns True when .set() is called (stop requested),
        # False on timeout (tick). So the loop exits cleanly on stop.
        while not self._stop.wait(self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt,
                    VisibilityTimeout=self._visibility,
                )
            except Exception as e:
                log.warning(
                    f"change_message_visibility failed: {type(e).__name__}: {e}"
                )


def _git(args, cwd=None):
    """Run a git command, raising with stderr in the message on failure."""
    result = subprocess.run(
        ["git", *(["-C", cwd] if cwd else []), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result.stdout


def clone_repo(repo, head_sha, pr_number):
    """Shallow-clone the repo and checkout the PR head SHA as a local branch."""
    clone_dir = os.path.join(WORK_DIR, f"pr-{pr_number}")

    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)
    os.makedirs(clone_dir, exist_ok=True)

    token = get_github_token()
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"

    log.info(f"Cloning {repo} PR#{pr_number} at {head_sha[:12]}")

    _git(["init", clone_dir])
    _git(["remote", "add", "origin", clone_url], cwd=clone_dir)
    # Fetch into a local ref so `git checkout` has something stable to target
    # even on a fresh repo with no HEAD.
    _git(
        ["fetch", "--depth=1", "origin", f"+refs/pull/{pr_number}/head:refs/heads/pr-head"],
        cwd=clone_dir,
    )
    _git(["checkout", "pr-head"], cwd=clone_dir)

    actual_sha = _git(["rev-parse", "HEAD"], cwd=clone_dir).strip()
    log.info(f"Checked out {actual_sha[:12]} in {clone_dir}")

    return clone_dir, actual_sha


def upload_log(s3, message):
    """Upload a log entry to S3 as a timestamped file."""
    now = datetime.now(timezone.utc)
    key = f"{S3_LOG_PREFIX}/{now:%Y-%m-%d}/{INSTANCE_ID}/{now:%H-%M-%S-%f}.json"
    s3.put_object(
        Bucket=S3_LOG_BUCKET,
        Key=key,
        Body=json.dumps(message, indent=2),
        ContentType="application/json",
    )
    log.info(f"Log uploaded to s3://{S3_LOG_BUCKET}/{key}")


def _purge_praktika_modules():
    """Drop ci.*/praktika.* from sys.modules so each workflow re-imports from its fresh clone."""
    for name in list(sys.modules):
        if (
            name == "ci"
            or name.startswith("ci.")
            or name == "praktika"
            or name.startswith("praktika.")
        ):
            del sys.modules[name]


def process_workflow(repo_root, event, check=None, gh_token=None, run_id=None):
    """Set up the import path / cwd for the cloned repo and delegate to the
    orchestrator's single entry-point. Everything workflow-related (DAG build,
    plan, check completion) lives in ``ci.praktika.orchestrator.orchestrate`` so
    that the runner script stays stable across PRs — orchestrator changes ship
    with the cloned code, not with a new LT version.

    ``gh_token``, if provided, is forwarded to the orchestrator so it can open
    per-job check runs.
    """
    _purge_praktika_modules()
    sys.path[:] = [p for p in sys.path if "/work/pr-" not in p]
    sys.path.insert(0, repo_root)
    # Praktika resolves workflow configs via relative paths (e.g. `./ci/workflows`),
    # so the orchestrator must run with repo_root as its CWD.
    os.chdir(repo_root)

    event_file = os.path.join(repo_root, "ci", "tmp", "event.json")
    os.makedirs(os.path.dirname(event_file), exist_ok=True)
    with open(event_file, "w") as f:
        json.dump(event, f, indent=2)

    from ci.praktika.orchestrator import orchestrate
    return orchestrate(event, check=check, gh_token=gh_token, run_id=run_id)


# Map event type -> check-run name. We post the check before cloning
# (so the PR author sees it immediately), which means we have to pick the
# name from the event alone, not from the resolved workflow config.
_CHECK_NAME_BY_EVENT = {
    "pull_request": "PR",
}


def handle_workflow(workflow):
    """Process one SQS workflow trigger: open the check run, clone the repo,
    run the orchestrator, and close the check run."""
    wf_type = workflow.get("type", "unknown")
    log.info(f"Processing workflow: {wf_type}")

    if wf_type != "pull_request":
        log.info(f"Unknown workflow type: {wf_type}")
        return {"status": "skipped", "reason": f"unknown type: {wf_type}"}

    repo = workflow.get("repo", "")
    pr_number = workflow.get("pr_number")
    head_sha = workflow.get("head_sha", "")
    check_name = _CHECK_NAME_BY_EVENT.get(wf_type, "CI Engine")

    # Mint a GitHub App token once and reuse it for the top-level check and
    # for the per-job check runs opened inside the orchestrator.
    token = None
    check = None
    try:
        token = get_github_token()
        check = CheckRun.start(token, repo, head_sha, check_name)
        log.info(f"Check run [{check_name}] id={check.id} in_progress")
    except Exception:
        log.exception("Failed to start check run")

    # Use the GitHub check run ID as the unique run identifier so the
    # orchestrator and its runners can distinguish completions from
    # concurrent runs on the same PR (e.g. two re-runs in quick succession).
    run_id = str(check.id) if check is not None else None

    try:
        clone_dir, actual_sha = clone_repo(repo, head_sha, pr_number)
        rc = process_workflow(clone_dir, workflow, check=check, gh_token=token, run_id=run_id)
        return {
            "status": "ok" if rc == 0 else "error",
            "pr": pr_number,
            "sha": actual_sha,
            "clone_dir": clone_dir,
            "rc": rc,
        }
    except Exception as e:
        # Something blew up before/around the orchestrator (e.g. clone). Close
        # the check as failure so it doesn't dangle in_progress forever.
        if check is not None:
            try:
                check.complete("failure", output={
                    "title": check_name,
                    "summary": "Runner failure before orchestration",
                    "text": f"```\n{type(e).__name__}: {e}\n```",
                })
            except Exception:
                log.exception("Failed to complete check run after runner error")
        raise


def poll():
    """EC2 mode: poll SQS for workflow triggers."""
    import boto3

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    # Queue's configured visibility timeout — re-extended by the heartbeat
    # every cycle so a workflow longer than this value can't be re-delivered
    # to the second orchestrator in the ASG.
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
            workflow = json.loads(msg["Body"])
            log.info(f"RECEIVED: {json.dumps(workflow)}")

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                result = handle_workflow(workflow)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")

            upload_log(s3, {
                "event": "workflow_processed",
                "instance_id": INSTANCE_ID,
                "workflow": workflow,
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
            # Message will become visible again after visibility timeout


def local(event_file):
    """Local mode: read event from file, run orchestrator in current repo."""
    with open(event_file) as f:
        event = json.load(f)

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )

    log.info(
        f"Local mode: {event.get('type')}.{event.get('action', '')} "
        f"PR#{event.get('pr_number', '?')} "
        f"[{event.get('head_ref', '')} -> {event.get('base_ref', '')}]"
    )

    token = None
    check = None
    if os.environ.get("CI_ENGINE_POST_CHECKS") == "1":
        repo = event.get("repo", "")
        head_sha = event.get("head_sha", "")
        check_name = _CHECK_NAME_BY_EVENT.get(event.get("type", ""), "CI Engine")
        try:
            token = get_github_token()
            check = CheckRun.start(token, repo, head_sha, check_name)
            log.info(f"Check run [{check_name}] id={check.id} in_progress")
        except Exception:
            log.exception("Failed to start check run")

    rc = process_workflow(repo_root, event, check=check, gh_token=token)
    sys.exit(rc)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--local":
        if len(sys.argv) < 3:
            print(f"Usage: {sys.argv[0]} --local <event.json>", file=sys.stderr)
            sys.exit(1)
        local(sys.argv[2])
    else:
        poll()
