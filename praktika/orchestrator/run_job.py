#!/usr/bin/env python3
"""CI engine job runner: polls a per-runner-type SQS queue, clones the PR,
and delegates to ``ci.praktika.orchestrator.job_runner.run_job`` which
ultimately invokes ``praktika.Runner.run`` for the requested job.

Deployed to EC2 via user_data (same gzip+base64 baking trick as ``run.py``).
Kept intentionally stable — job-level policy lives in the orchestrator
module (``job_runner.py``) and ships with each PR, so bumping that code
does not require an LT/ASG redeploy.

Env:
    RUNNER_QUEUE_NAME      SQS queue this runner polls (one per runs_on label)
    AWS_DEFAULT_REGION
    INSTANCE_ID
    WORK_DIR               where PRs are cloned (default /opt/ci-engine/work)

Usage:
    # EC2 mode (polls SQS):
    python3 ci/praktika/orchestrator/run_job.py

    # Local test (no AWS required):
    python3 ci/praktika/orchestrator/run_job.py --local tmp/sandbox/test_task.json
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

QUEUE_NAME = os.environ.get("RUNNER_QUEUE_NAME", "")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/ci-engine/work")

S3_LOG_BUCKET = "clickhouse-test-reports-private"
S3_LOG_PREFIX = "praktika/job-runner"

GH_APP_SECRET_ID = "woolenwolf_gh_app"

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{INSTANCE_ID}] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("ci-engine-runner")


def get_github_token():
    """Mint a GitHub installation token via the woolenwolf GitHub App."""
    import jwt
    import requests

    sm = __import__("boto3").client("secretsmanager", region_name=REGION)
    secret = json.loads(
        sm.get_secret_value(SecretId=GH_APP_SECRET_ID)["SecretString"]
    )
    app_id = secret["clickhouse-app-id"]
    app_key = secret["clickhouse-app-key"]
    installation_id = secret["installation_id"]

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


def clone_repo(repo, head_sha, pr_number, token):
    """Shallow-clone the repo and checkout the PR head as a local branch."""
    clone_dir = os.path.join(WORK_DIR, f"pr-{pr_number}")

    if os.path.exists(clone_dir):
        shutil.rmtree(clone_dir)
    os.makedirs(clone_dir, exist_ok=True)

    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    log.info(f"Cloning {repo} PR#{pr_number} at {head_sha[:12]}")

    _git(["init", clone_dir])
    _git(["remote", "add", "origin", clone_url], cwd=clone_dir)
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
    """Drop ci.*/praktika.* from sys.modules so each task re-imports from its fresh clone."""
    for name in list(sys.modules):
        if (
            name == "ci"
            or name.startswith("ci.")
            or name == "praktika"
            or name.startswith("praktika.")
        ):
            del sys.modules[name]


def process_job(repo_root, task, gh_token=None, local=False):
    """Set up sys.path / cwd for the cloned repo and delegate to the domain
    entry-point ``ci.praktika.orchestrator.job_runner.run_job``.
    """
    _purge_praktika_modules()
    sys.path[:] = [p for p in sys.path if "/work/pr-" not in p]
    sys.path.insert(0, repo_root)
    os.chdir(repo_root)
    # Praktika job scripts are invoked as subprocesses (`python3 -m praktika.native_jobs ...`).
    # They don't inherit our sys.path, so expose the ci/ package dir via PYTHONPATH.
    ci_dir = os.path.join(repo_root, "ci")
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{ci_dir}:{existing}" if existing else ci_dir

    # Persist the task in the cloned repo tree for downstream tools that
    # expect `ci/tmp/event.json` (matches the orchestrator's convention).
    event_file = os.path.join(repo_root, "ci", "tmp", "event.json")
    os.makedirs(os.path.dirname(event_file), exist_ok=True)
    with open(event_file, "w") as f:
        json.dump(task, f, indent=2)

    from ci.praktika.orchestrator.job_runner import run_job
    return run_job(task, gh_token=gh_token, local=local)


def handle_task(task):
    """Process one SQS job task: clone repo and run the job."""
    task_type = task.get("type", "unknown")
    job_name = task.get("job_name", "?")
    log.info(f"Processing task: {task_type} job={job_name!r}")

    if task_type != "job_task":
        log.info(f"Unknown task type: {task_type}")
        return {"status": "skipped", "reason": f"unknown type: {task_type}"}

    repo = task.get("repo", "")
    pr_number = task.get("pr_number")
    head_sha = task.get("head_sha", "")

    token = None
    try:
        token = get_github_token()
    except Exception:
        log.exception("Failed to fetch GitHub App token")

    try:
        clone_dir, actual_sha = clone_repo(repo, head_sha, pr_number, token)
        rc = process_job(clone_dir, task, gh_token=token)
        return {
            "status": "ok" if rc == 0 else "error",
            "pr": pr_number,
            "sha": actual_sha,
            "job": job_name,
            "clone_dir": clone_dir,
            "rc": rc,
        }
    except Exception as e:
        log.exception("handle_task failed")
        raise


def poll():
    """EC2 mode: poll the per-type SQS queue for job tasks."""
    import boto3

    if not QUEUE_NAME:
        log.error("RUNNER_QUEUE_NAME not set — cannot start runner")
        sys.exit(1)

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]

    upload_log(s3, {
        "event": "startup",
        "instance_id": INSTANCE_ID,
        "queue": QUEUE_NAME,
        "time": datetime.now(timezone.utc).isoformat(),
    })

    log.info(f"Polling {queue_url} ...")

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
            # Message will become visible again after the visibility timeout


def local(task_file):
    """Local mode: read a task file, run in the current repo (no clone)."""
    with open(task_file) as f:
        task = json.load(f)

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )

    log.info(
        f"Local mode: job={task.get('job_name', '?')!r} "
        f"workflow={task.get('workflow_name', '?')!r} "
        f"PR#{task.get('pr_number', '?')}"
    )

    rc = process_job(repo_root, task, local=True)
    sys.exit(rc)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--local":
        if len(sys.argv) < 3:
            print(f"Usage: {sys.argv[0]} --local <task.json>", file=sys.stderr)
            sys.exit(1)
        local(sys.argv[2])
    else:
        poll()
