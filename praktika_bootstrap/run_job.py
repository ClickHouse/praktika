#!/usr/bin/env python3
"""Job bootstrap agent with cached Praktika venv dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from praktika_bootstrap.common import (
    CancelWatchdog,
    Heartbeat,
    VisibilityHeartbeat,
    clone_repo,
    configure_logging,
    get_github_token,
    resolve_praktika_runtime,
    upload_log,
)
from praktika_bootstrap.venv_manager import (
    ensure_praktika_runtime,
    praktika_command,
    venv_env,
)

QUEUE_NAME = os.environ.get("RUNNER_QUEUE_NAME", "")
REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or ""
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/praktika/work")
S3_LOG_BUCKET = "praktika-artifacts-eu-north-1"
S3_LOG_PREFIX = "job-runner"

log = configure_logging("job-agent", INSTANCE_ID)
_get_github_token = get_github_token


def handle_task(task):
    task_type = task.get("type", "unknown")
    job_name = task.get("job_name", "?")
    log.info("Processing task: %s job=%r", task_type, job_name)

    if task_type != "job_task":
        log.info("Unknown task type: %s, skipping", task_type)
        return {"status": "skipped", "reason": f"unknown type: {task_type}"}

    repo = task.get("repo", "")
    pr_number = task.get("pr_number")
    head_sha = task.get("head_sha", "")

    gh_token = get_github_token(REGION)
    subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=gh_token,
        text=True,
        check=True,
    )

    clone_dir, actual_sha = clone_repo(
        repo,
        head_sha,
        pr_number,
        gh_token,
        work_dir=WORK_DIR,
        log=log,
    )

    base_venv, source = resolve_praktika_runtime(clone_dir, log, role="job")
    venv_dir = ensure_praktika_runtime(source or None, base_venv=base_venv, log=log)

    task_file = os.path.join(clone_dir, "ci", "tmp", "task.json")
    os.makedirs(os.path.dirname(task_file), exist_ok=True)
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2)

    cancel_s3_bucket = task.get("cancel_s3_bucket", "")
    cancel_s3_key = task.get("cancel_s3_key", "")
    heartbeat_s3_bucket = task.get("heartbeat_s3_bucket", "")
    heartbeat_s3_key = task.get("heartbeat_s3_key", "")
    heartbeat_interval_s = task.get("heartbeat_interval_s", 30)

    log.info("Running job %r for PR#%s in %s", job_name, pr_number, venv_dir)
    import boto3

    s3 = boto3.client("s3", region_name=REGION)
    proc = subprocess.Popen(
        praktika_command(venv_dir, "orchestrate", "job", task_file, "--ci"),
        cwd=clone_dir,
        env=venv_env(venv_dir),
        stderr=subprocess.PIPE,
        text=True,
    )

    cm_cancel = CancelWatchdog(s3, cancel_s3_bucket, cancel_s3_key, proc, log=log)
    cm_heartbeat = (
        Heartbeat(
            s3,
            heartbeat_s3_bucket,
            heartbeat_s3_key,
            heartbeat_interval_s,
            log=log,
        )
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
        "base_venv": base_venv,
        "source": source,
        "venv": str(venv_dir),
        "rc": rc,
        "stderr": stderr_output.strip()[:500] if stderr_output else "",
    }


def poll():
    import boto3

    if not QUEUE_NAME:
        log.error("RUNNER_QUEUE_NAME not set - cannot start runner")
        sys.exit(1)

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]
    visibility = int(
        sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )

    upload_log(
        s3,
        S3_LOG_BUCKET,
        S3_LOG_PREFIX,
        INSTANCE_ID,
        {
            "event": "startup",
            "instance_id": INSTANCE_ID,
            "queue": QUEUE_NAME,
            "time": datetime.now(timezone.utc).isoformat(),
        },
        log,
    )
    log.info("Polling %s (visibility_timeout=%ss)", queue_url, visibility)

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
            log.info("RECEIVED: %s", json.dumps(task))

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                result = handle_task(task)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")

            upload_log(
                s3,
                S3_LOG_BUCKET,
                S3_LOG_PREFIX,
                INSTANCE_ID,
                {
                    "event": "task_processed",
                    "instance_id": INSTANCE_ID,
                    "task": task,
                    "result": result,
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                log,
            )
        except Exception as e:
            log.exception("ERROR processing message")
            upload_log(
                s3,
                S3_LOG_BUCKET,
                S3_LOG_PREFIX,
                INSTANCE_ID,
                {
                    "event": "task_error",
                    "instance_id": INSTANCE_ID,
                    "error": str(e),
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                log,
            )


def main():
    poll()


if __name__ == "__main__":
    main()
