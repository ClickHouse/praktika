#!/usr/bin/env python3
"""Unified Praktika controller for workflow-orchestrator and job-runner roles."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone

from praktika_controller.common import (
    CancelWatchdog,
    Heartbeat,
    VisibilityHeartbeat,
    clone_repo,
    configure_logging,
    get_github_token,
    imds_token,
    instance_tag,
    resolve_praktika_base_venv,
    try_scale_in_if_idle,
    upload_log,
)
from praktika_controller.venv_manager import (
    ensure_praktika_runtime,
    praktika_command,
    venv_env,
)

REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or ""
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/praktika/work")
S3_LOG_BUCKET = "praktika-artifacts-eu-north-1"

ROLE_WORKFLOW = "workflow_orchestrator"
ROLE_RUNNER = "job_runner"
SUPPORTED_ROLES = {ROLE_WORKFLOW, ROLE_RUNNER}


def _instance_runtime_tags() -> tuple[str, str]:
    token = imds_token()
    role = (
        os.environ.get("PRAKTIKA_CONTROLLER_ROLE", "").strip()
        or instance_tag("praktika_role", token=token)
    )
    queue = instance_tag("praktika_queue", token=token)
    return role, queue


def _resolve_role_and_queue() -> tuple[str, str]:
    role, queue = _instance_runtime_tags()
    queue = os.environ.get("PRAKTIKA_CONTROLLER_QUEUE", "").strip() or queue

    if not role and queue:
        role = ROLE_WORKFLOW if queue.startswith("workflow-orchestrator") else ROLE_RUNNER

    if role not in SUPPORTED_ROLES:
        raise RuntimeError(
            f"Could not resolve Praktika controller role from instance tags/env: {role!r}"
        )
    if not queue:
        raise RuntimeError(
            "Could not resolve Praktika controller queue from instance tags/env"
        )
    return role, queue


def _role_config(role: str) -> tuple[str, str]:
    if role == ROLE_WORKFLOW:
        return "praktika-controller", "workflow-orchestrator"
    if role == ROLE_RUNNER:
        return "praktika-controller", "job-runner"
    raise AssertionError(f"Unhandled role: {role}")


def _resolve_runtime(clone_dir: str, log):
    base_venv = resolve_praktika_base_venv(clone_dir, log)
    venv_dir = ensure_praktika_runtime(
        None,
        base_venv=base_venv,
        log=log,
    )
    return base_venv, venv_dir


def handle_workflow(event, log):
    wf_type = event.get("type", "unknown")
    log.info("Processing: %s", wf_type)

    if wf_type not in ("pull_request", "push"):
        log.info("Unknown event type: %s, skipping", wf_type)
        return {"status": "skipped", "reason": f"unknown type: {wf_type}"}

    repo = event.get("repo", "")
    pr_number = event.get("pr_number")
    head_sha = event.get("head_sha", "")
    branch = event.get("head_ref", "")

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
        branch=branch,
        log=log,
    )

    base_venv, venv_dir = _resolve_runtime(clone_dir, log)

    event_file = os.path.join(clone_dir, "ci", "tmp", "event.json")
    os.makedirs(os.path.dirname(event_file), exist_ok=True)
    with open(event_file, "w", encoding="utf-8") as f:
        json.dump(event, f, indent=2)

    target = f"PR#{pr_number}" if pr_number else f"branch={branch}"
    log.info("Running orchestrator for %s in %s", target, venv_dir)
    result = subprocess.run(
        praktika_command(venv_dir, "orchestrate", "workflow", event_file, "--ci"),
        cwd=clone_dir,
        env=venv_env(venv_dir),
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0 and result.stderr:
        log.error(result.stderr.rstrip())

    return {
        "status": "ok" if result.returncode == 0 else "error",
        "pr": pr_number,
        "branch": branch,
        "sha": actual_sha,
        "base_venv": base_venv,
        "venv": str(venv_dir),
        "rc": result.returncode,
        "stderr": result.stderr.strip()[:500] if result.stderr else "",
    }


def handle_task(task, log):
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

    base_venv, venv_dir = _resolve_runtime(clone_dir, log)

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
        "venv": str(venv_dir),
        "rc": rc,
        "stderr": stderr_output.strip()[:500] if stderr_output else "",
    }


def poll():
    import boto3

    role, queue_name = _resolve_role_and_queue()
    log_name, s3_log_prefix = _role_config(role)
    log = configure_logging(log_name, INSTANCE_ID)

    sqs = boto3.client("sqs", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    visibility = int(
        sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )

    upload_log(
        s3,
        S3_LOG_BUCKET,
        s3_log_prefix,
        INSTANCE_ID,
        {
            "event": "startup",
            "role": role,
            "instance_id": INSTANCE_ID,
            "queue": queue_name,
            "time": datetime.now(timezone.utc).isoformat(),
        },
        log,
    )
    log.info("Role=%s polling %s (visibility_timeout=%ss)", role, queue_url, visibility)

    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
        )
        messages = resp.get("Messages", [])
        if not messages:
            if try_scale_in_if_idle(
                sqs=sqs,
                queue_url=queue_url,
                queue_name=queue_name,
                region=REGION,
                instance_id=INSTANCE_ID,
                log=log,
            ):
                return
            continue

        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        try:
            payload = json.loads(msg["Body"])
            log.info("RECEIVED: %s", json.dumps(payload))

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                if role == ROLE_WORKFLOW:
                    result = handle_workflow(payload, log)
                    event_name = "workflow_processed"
                    payload_key = "trigger"
                else:
                    result = handle_task(payload, log)
                    event_name = "task_processed"
                    payload_key = "task"
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")

            upload_log(
                s3,
                S3_LOG_BUCKET,
                s3_log_prefix,
                INSTANCE_ID,
                {
                    "event": event_name,
                    "role": role,
                    "instance_id": INSTANCE_ID,
                    payload_key: payload,
                    "result": result,
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                log,
            )
        except Exception as e:
            error_event = "workflow_error" if role == ROLE_WORKFLOW else "task_error"
            log.exception("ERROR processing message")
            upload_log(
                s3,
                S3_LOG_BUCKET,
                s3_log_prefix,
                INSTANCE_ID,
                {
                    "event": error_event,
                    "role": role,
                    "instance_id": INSTANCE_ID,
                    "error": str(e),
                    "time": datetime.now(timezone.utc).isoformat(),
                },
                log,
            )


def main():
    if not REGION:
        raise RuntimeError("AWS_DEFAULT_REGION or AWS_REGION must be set")
    poll()


if __name__ == "__main__":
    main()
