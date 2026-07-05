#!/usr/bin/env python3
"""Unified Praktika controller for workflow-orchestrator and job-runner roles."""

from __future__ import annotations

import json
import os
import subprocess
import time

from praktika_controller.common import (
    CancelWatchdog,
    FIRST_BOOT_RESERVED_CAPACITY_LOG_INTERVAL_S,
    Heartbeat,
    LogRateLimiter,
    VisibilityHeartbeat,
    clean_work_root,
    clone_repo,
    configure_logging,
    get_github_token,
    imds_token,
    instance_tag,
    resolve_praktika_base_venv,
    terminate_instance_for_replacement,
    terminate_process_group,
    try_scale_in_if_idle,
)
from praktika_controller.venv_manager import (
    ensure_praktika_runtime,
    praktika_command,
    venv_env,
)

REGION = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or ""
INSTANCE_ID = os.environ.get("INSTANCE_ID", "local-dev")
WORK_DIR = os.environ.get("WORK_DIR", "/opt/praktika/work")
ROLE_WORKFLOW = "workflow_orchestrator"
ROLE_RUNNER = "job_runner"
SUPPORTED_ROLES = {ROLE_WORKFLOW, ROLE_RUNNER}
INFRA_FAILURE_MAX_RECEIVES = int(
    os.environ.get("PRAKTIKA_INFRA_FAILURE_MAX_RECEIVES", "3")
)
# Exit code the orchestrator uses for a startup/infra failure (workflow never
# ran) — must match praktika.orchestrator.INFRA_EXIT_CODE. Distinct from rc=1
# (the DAG ran and jobs legitimately failed) so we retry on a fresh instance
# only for genuine infra faults, not for ordinary red builds.
INFRA_EXIT_CODE = 100


class InfraOrchestrationError(RuntimeError):
    """Raised when the orchestrator subprocess exits with INFRA_EXIT_CODE, so
    the poll loop releases the message and replaces the instance for a retry."""


def _instance_runtime_tags() -> tuple[str, str]:
    token = imds_token()
    role = os.environ.get("PRAKTIKA_CONTROLLER_ROLE", "").strip() or instance_tag(
        "praktika_role", token=token
    )
    queue = instance_tag("praktika_queue", token=token)
    return role, queue


def _resolve_role_and_queue() -> tuple[str, str]:
    role, queue = _instance_runtime_tags()
    queue = os.environ.get("PRAKTIKA_CONTROLLER_QUEUE", "").strip() or queue

    if not role and queue:
        role = (
            ROLE_WORKFLOW if queue.startswith("workflow-orchestrator") else ROLE_RUNNER
        )

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


def _praktika_env(venv_dir: str, queue_name: str, attempt: str = "") -> dict[str, str]:
    env = venv_env(venv_dir)
    env["PRAKTIKA_CONTROLLER_QUEUE"] = queue_name
    if attempt:
        # Surfaced on the GitHub check so cross-instance infra retries are
        # visible (e.g. "attempt 2/3").
        env["PRAKTIKA_ATTEMPT"] = attempt
    return env


def _s3_key_exists(s3, bucket: str, key: str, log) -> bool:
    if not bucket or not key:
        return False
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if str(code) in {"404", "NoSuchKey", "NotFound"}:
            return False
        log.warning(
            "Could not check s3://%s/%s: %s: %s",
            bucket,
            key,
            type(e).__name__,
            e,
        )
        return False


def _write_infra_failure_final(task, exc: Exception, log) -> bool:
    final_bucket = task.get("final_state_s3_bucket", "")
    final_key = task.get("final_state_s3_key", "")
    if not final_bucket or not final_key:
        return False
    try:
        import boto3

        s3 = boto3.client("s3", region_name=REGION)
        body = {
            "type": "job_completion",
            "job_name": task.get("job_name"),
            "rc": 1,
            "ts": time.time(),
            "repo": task.get("repo"),
            "pr_number": task.get("pr_number"),
            "head_sha": task.get("head_sha"),
            "workflow_name": task.get("workflow_name"),
            "instance_id": INSTANCE_ID,
            "infra_error": True,
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "check_output": {
                "title": "INFRA_ERROR",
                "summary": (
                    f"Runner infrastructure failed before job start on `{INSTANCE_ID}`: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                ),
            },
        }
        s3.put_object(
            Bucket=final_bucket,
            Key=final_key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
        )
        log.info("Wrote infra failure final state s3://%s/%s", final_bucket, final_key)
        return True
    except Exception:
        log.exception(
            "Failed to write infra failure final state s3://%s/%s",
            final_bucket,
            final_key,
        )
        return False


def _prepare_runner_for_task(role: str, log) -> str:
    if role != ROLE_RUNNER:
        return ""
    try:
        clean_work_root(WORK_DIR, log)
        return ""
    except Exception as e:
        log.exception("Runner workdir cleanup failed before task")
        return f"workdir cleanup failed: {type(e).__name__}: {e}"


def handle_workflow(event, log, queue_name: str, receive_count: int = 1):
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

    attempt = f"{receive_count}/{INFRA_FAILURE_MAX_RECEIVES}"
    target = f"PR#{pr_number}" if pr_number else f"branch={branch}"
    log.info("Running orchestrator for %s in %s (attempt %s)", target, venv_dir, attempt)
    result = subprocess.run(
        praktika_command(venv_dir, "orchestrate", "workflow", event_file, "--ci"),
        cwd=clone_dir,
        env=_praktika_env(venv_dir, queue_name, attempt=attempt),
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0 and result.stderr:
        log.error(result.stderr.rstrip())

    # A startup/infra failure (workflow never ran) is retryable on a fresh
    # orchestrator: raise so the poll loop releases the message and replaces
    # this instance.
    if result.returncode == INFRA_EXIT_CODE:
        raise InfraOrchestrationError(
            f"orchestrator infra failure (rc={INFRA_EXIT_CODE}) on attempt {attempt}: "
            f"{result.stderr.strip()[:300] if result.stderr else ''}"
        )

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


def handle_task(task, log, queue_name: str):
    task_type = task.get("type", "unknown")
    job_name = task.get("job_name", "?")
    log.info("Processing task: %s job=%r", task_type, job_name)

    if task_type != "job_task":
        log.info("Unknown task type: %s, skipping", task_type)
        return {"status": "skipped", "reason": f"unknown type: {task_type}"}

    repo = task.get("repo", "")
    pr_number = task.get("pr_number")
    head_sha = task.get("head_sha", "")
    cancel_s3_bucket = task.get("cancel_s3_bucket", "")
    cancel_s3_key = task.get("cancel_s3_key", "")
    heartbeat_s3_bucket = task.get("heartbeat_s3_bucket", "")
    heartbeat_s3_key = task.get("heartbeat_s3_key", "")
    heartbeat_interval_s = task.get("heartbeat_interval_s", 30)

    import boto3

    s3 = boto3.client("s3", region_name=REGION)
    if _s3_key_exists(s3, cancel_s3_bucket, cancel_s3_key, log):
        log.info(
            "Task %r belongs to a cancelled run, skipping before clone",
            job_name,
        )
        return {"status": "skipped", "reason": "cancelled", "job": job_name}

    cm_heartbeat = (
        Heartbeat(
            s3,
            heartbeat_s3_bucket,
            heartbeat_s3_key,
            heartbeat_interval_s,
            fields={"instance_id": INSTANCE_ID, "phase": "picked_up"},
            log=log,
        )
        if heartbeat_s3_bucket and heartbeat_s3_key
        else None
    )
    if cm_heartbeat is not None:
        cm_heartbeat.start()

    proc = None
    try:
        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="authenticating")
        gh_token = get_github_token(REGION)
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=gh_token,
            text=True,
            check=True,
        )

        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="cloning")
        clone_dir, actual_sha = clone_repo(
            repo,
            head_sha,
            pr_number,
            gh_token,
            work_dir=WORK_DIR,
            clean_existing=False,
            log=log,
        )

        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="resolving_runtime")
        base_venv, venv_dir = _resolve_runtime(clone_dir, log)

        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="writing_task")
        task_file = os.path.join(clone_dir, "ci", "tmp", "task.json")
        os.makedirs(os.path.dirname(task_file), exist_ok=True)
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2)

        log.info("Running job %r for PR#%s in %s", job_name, pr_number, venv_dir)

        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="running_job")
        proc = subprocess.Popen(
            praktika_command(venv_dir, "orchestrate", "job", task_file, "--ci"),
            cwd=clone_dir,
            env=_praktika_env(venv_dir, queue_name),
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        cm_cancel = CancelWatchdog(s3, cancel_s3_bucket, cancel_s3_key, proc, log=log)
        with cm_cancel:
            _, stderr_output = proc.communicate()
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
    finally:
        if proc is not None:
            terminate_process_group(proc, log, grace_s=1)
        if cm_heartbeat is not None:
            cm_heartbeat.stop()


def poll():
    import boto3

    role, queue_name = _resolve_role_and_queue()
    log_name, _ = _role_config(role)
    log = configure_logging(log_name, INSTANCE_ID)
    log.info("Resolved controller role=%s queue=%s", role, queue_name)

    sqs = boto3.client("sqs", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    visibility = int(
        sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )
    log.info("Role=%s polling %s (visibility_timeout=%ss)", role, queue_url, visibility)

    has_received_message = False
    # SQS long polling is capped at 20s; keep polling responsive and throttle
    # the pre-first-job reserved-capacity idle log separately.
    reserved_capacity_log_limiter = LogRateLimiter(
        FIRST_BOOT_RESERVED_CAPACITY_LOG_INTERVAL_S
    )
    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = resp.get("Messages", [])
        if not messages:
            if try_scale_in_if_idle(
                sqs=sqs,
                queue_url=queue_url,
                queue_name=queue_name,
                region=REGION,
                instance_id=INSTANCE_ID,
                has_received_message=has_received_message,
                reserved_capacity_log_limiter=reserved_capacity_log_limiter,
                log=log,
            ):
                return
            continue

        has_received_message = True
        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        payload = None
        receive_count = int(
            msg.get("Attributes", {}).get("ApproximateReceiveCount") or "1"
        )
        try:
            payload = json.loads(msg["Body"])
            log.info("RECEIVED: %s", json.dumps(payload))

            with VisibilityHeartbeat(sqs, queue_url, receipt, visibility):
                cleanup_error = _prepare_runner_for_task(role, log)
                if cleanup_error:
                    try:
                        sqs.change_message_visibility(
                            QueueUrl=queue_url,
                            ReceiptHandle=receipt,
                            VisibilityTimeout=0,
                        )
                    except Exception:
                        log.exception("Failed to release task after cleanup failure")
                    terminate_instance_for_replacement(
                        region=REGION,
                        instance_id=INSTANCE_ID,
                        log=log,
                        reason=cleanup_error,
                    )
                    return

                if role == ROLE_WORKFLOW:
                    result = handle_workflow(
                        payload, log, queue_name, receive_count=receive_count
                    )
                else:
                    result = handle_task(payload, log, queue_name)
        except json.JSONDecodeError:
            log.exception("ERROR processing message: malformed JSON")
            try:
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: malformed message deleted")
            except Exception:
                log.exception("Failed to delete malformed message")
        except Exception as exc:
            log.exception("ERROR processing message: %s", type(exc).__name__)
            give_up = receive_count >= INFRA_FAILURE_MAX_RECEIVES
            if role == ROLE_WORKFLOW:
                # Workflow infra failure: the orchestrator finalizes its own
                # GitHub check on every attempt (including this one), so on
                # give-up we just drop the message. Otherwise release it for
                # redelivery and replace this instance, so a *fresh*
                # orchestrator retries — the right cure for instance-local
                # faults (stale runtime venv, corrupt clone, bad AMI) that an
                # in-process retry on the same box would just hit again.
                if give_up:
                    try:
                        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                        log.info(
                            "DONE: workflow infra failure, gave up after %s receive(s)",
                            receive_count,
                        )
                    except Exception:
                        log.exception("Failed to delete message after giving up")
                else:
                    try:
                        sqs.change_message_visibility(
                            QueueUrl=queue_url,
                            ReceiptHandle=receipt,
                            VisibilityTimeout=0,
                        )
                    except Exception:
                        log.exception("Failed to release workflow message for retry")
                    terminate_instance_for_replacement(
                        region=REGION,
                        instance_id=INSTANCE_ID,
                        log=log,
                        reason=(
                            f"workflow infra failure "
                            f"(attempt {receive_count}/{INFRA_FAILURE_MAX_RECEIVES}): {exc}"
                        ),
                    )
                    return
            elif (
                isinstance(payload, dict)
                and payload.get("type") == "job_task"
                and give_up
                and _write_infra_failure_final(payload, exc, log)
            ):
                try:
                    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                    log.info("DONE: message deleted after infra failure final state")
                except Exception:
                    log.exception(
                        "Failed to delete message after infra failure final state"
                    )
            else:
                try:
                    sqs.change_message_visibility(
                        QueueUrl=queue_url,
                        ReceiptHandle=receipt,
                        VisibilityTimeout=0,
                    )
                except Exception:
                    log.exception("Failed to release failed message for retry")
                log.info("DONE: message left for retry")
        else:
            try:
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
                log.info("DONE: message deleted")
            except Exception:
                log.exception(
                    "RESULT produced but message delete failed; message may retry"
                )
            log.info("RESULT: %s", json.dumps(result))


def main():
    if not REGION:
        raise RuntimeError("AWS_DEFAULT_REGION or AWS_REGION must be set")
    poll()


if __name__ == "__main__":
    main()
