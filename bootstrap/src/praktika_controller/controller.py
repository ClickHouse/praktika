#!/usr/bin/env python3
"""Unified Praktika controller for workflow-orchestrator and job-runner roles."""

from __future__ import annotations

import json
import os
import subprocess
import threading
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
    finalize_check,
    get_github_token,
    head_commit_info,
    imds_token,
    load_ci_config,
    post_early_check,
    instance_tag,
    profile_step,
    resolve_praktika_base_venv,
    restore_repo_snapshot,
    TaskLogCapture,
    terminate_if_auto_scaled,
    terminate_instance_for_replacement,
    terminate_process_group,
    try_scale_in_if_idle,
    warm_branches,
    warm_repo_dir,
)
from praktika_controller.merge import (
    compute_run_git_metadata,
    MergeConflict,
    MULTIPART_MAX_CONCURRENCY,
    prepare_repo_snapshot,
    read_repo_settings,
)
from praktika_controller.self_update import maybe_self_update, update_pending
from praktika_controller.venv_manager import (
    is_passthrough,
    praktika_command,
    resolve_praktika_runtime,
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
# Provisional name for the check run opened before the clone (the real
# workflow name isn't known until the repo config is read). The orchestrator
# renames it to the matched workflow's name once it takes over.
EARLY_CHECK_NAME = "CI"


def _s3_client():
    """S3 client whose connection pool matches the snapshot multipart upload
    concurrency. The default botocore pool is 10; the parallel part uploads
    (MULTIPART_MAX_CONCURRENCY) would otherwise exhaust it and make urllib3 warn
    and churn sockets ("Connection pool is full, discarding connection")."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=REGION,
        config=Config(max_pool_connections=MULTIPART_MAX_CONCURRENCY),
    )


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


def _ci_config_for_message(role: str, payload, log) -> dict:
    """The ci_config snapshot for this message, read **once**.

    The orchestrator (workflow role) reads it from SSM for a fresh run — the same
    single read that drives both the controller self-update decision and the value
    frozen into run metadata by ``handle_workflow`` (pass this snapshot to it, do
    NOT re-read SSM, or a mid-message parameter change could make the orchestrator
    run one controller version while freezing another into the run). A rerun reuses
    the frozen copy on the event; a job runner reads the frozen copy from the task
    (never SSM). See ci-config.md."""
    if not isinstance(payload, dict):
        return {}
    if role == ROLE_WORKFLOW and payload.get("type") != "rerun":
        return load_ci_config(region=REGION, log=log)
    return payload.get("ci_config") or {}


def _controller_version_pin(ci_config) -> str:
    value = (ci_config or {}).get("praktika_controller_version", "")
    return value.strip() if isinstance(value, str) else ""


def _warm_clone_config(ci_config):
    """Opt-in warm-clone target, configured entirely in ci_config (SSM-tunable, no
    pool redeploy). Both keys are required and together enable warm:
    ``ci_config["repo"]`` (this project's ``owner/name``) and
    ``ci_config["warm_branches"]`` (a non-empty list of concrete names or globs like
    release/2*). Returns ``(repo, [patterns])`` or None when either is missing.
    warm_branches resolves the patterns against the remote's heads, so globs expand
    and missing names are skipped. (The repo lives in ci_config because the
    controller has no reliable owner/name source at boot — Settings.PROJECT_NAME is
    only the bare name.)"""
    cfg = ci_config or {}
    repo = str(cfg.get("repo", "")).strip()
    raw = cfg.get("warm_branches")
    patterns = [str(b).strip() for b in raw if str(b).strip()] if isinstance(raw, list) else []
    if not repo or not patterns:
        return None
    return (repo, patterns)


def _start_warm_repo(warm_cfg, log):
    """Background, best-effort warm of the PR base branch(es) into WORK_DIR while
    the orchestrator is idle, so the next task's clone adopts it. Non-blocking: the
    poll loop keeps running, and a task that arrives before it finishes just
    cold-clones. Daemon thread, so it dies with the process on scale-in."""
    repo, branches = warm_cfg

    def _run():
        try:
            token = get_github_token(REGION)
        except Exception as e:  # noqa: BLE001
            log.warning("Warm clone skipped (no GitHub token): %s", e)
            return
        warm_branches(repo, branches, token, WORK_DIR, log=log)

    threading.Thread(target=_run, name="warm-clone", daemon=True).start()
    log.info("Started background warm clone of %s %s", repo, branches)


def _resolve_runtime_source(clone_dir: str, log, ci_config=None):
    """Optional Praktika runtime source override. Two sources, pin first:

    1. ``ci_config['praktika_version']`` — a per-run version pin read once from
       SSM by the orchestrator and frozen into run metadata (see ci-config.md), so
       every job runner installs the SAME Praktika from run metadata, not SSM. It
       supports the three pip forms: a version spec (``praktika==0.1.9``), a wheel
       URL (``https://…whl``), or a repo/filesystem path (``.``). It takes
       precedence over the per-pool tag below — an operator pinning a version wins
       over a dev pool's checkout tag.
    2. The per-pool ``praktika_runtime_source`` instance tag (set from the pool's
       ``ext['runtime_source']``): a filesystem path installed on every task so
       the pool always runs the current checkout.

    When set (by either), the controller does NOT use the Praktika baked into the
    AMI; it installs Praktika from the resolved source on every task. Relative
    filesystem paths resolve against the cloned repo (so ``.`` installs Praktika
    from the checked-out repo itself); URLs / version specs are handed to pip
    verbatim. Returns ``None`` when neither is set (the baked venv is used).

    A failure to *read* the tag (transient IMDS/metadata error) is NOT treated as
    "unset": it propagates, so a pool configured to test the checkout fails the
    task (and the infra retry re-runs it) instead of silently passing on the
    baked Praktika. A genuinely absent tag returns "" from ``instance_tag`` (404)
    and is handled as unset below."""
    pin = (ci_config or {}).get("praktika_version", "")
    pin = pin.strip() if isinstance(pin, str) else ""
    if pin:
        if is_passthrough(pin):
            # Wheel URL or version spec: a pip target in its own right.
            log.info("Praktika runtime source: ci_config pin %s", pin)
            return pin
        # Repo-path form: resolve like the per-pool tag (relative to the clone).
        resolved = pin if os.path.isabs(pin) else os.path.join(clone_dir, pin)
        log.info("Praktika runtime source: ci_config pin path %s", resolved)
        return resolved

    source = instance_tag("praktika_runtime_source")
    source = (source or "").strip()
    if not source:
        return None
    if "://" in source:
        # Only filesystem paths are supported; a URL would otherwise be joined
        # onto clone_dir and fail confusingly at pip time. Fail loudly instead.
        raise ValueError(
            f"praktika_runtime_source must be a filesystem path, got {source!r}"
        )
    if not os.path.isabs(source):
        source = os.path.join(clone_dir, source)
    log.info("Praktika runtime source: per-pool source %s", source)
    return source


def _resolve_runtime(clone_dir: str, log, ci_config=None):
    base_venv = resolve_praktika_base_venv(clone_dir, log)
    source = _resolve_runtime_source(clone_dir, log, ci_config=ci_config)
    # A local-checkout source runs straight from the tree via PYTHONPATH (no per-task
    # install); a URL/version pin or no source resolves to an installed/baked venv.
    # See venv_manager.resolve_praktika_runtime.
    venv_dir, pythonpath = resolve_praktika_runtime(
        source,
        base_venv=base_venv,
        log=log,
    )
    return base_venv, venv_dir, pythonpath


def _praktika_env(
    venv_dir: str,
    queue_name: str,
    attempt: str = "",
    bootstrap_check_id=None,
    snapshot=None,
    ci_config=None,
    pythonpath=None,
) -> dict[str, str]:
    env = venv_env(venv_dir)
    if pythonpath:
        # Local-checkout runtime (see venv_manager.resolve_praktika_runtime): import
        # Praktika straight from the checkout instead of an installed copy. Prepend
        # so it wins over anything already on PYTHONPATH.
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{pythonpath}{os.pathsep}{existing}" if existing else pythonpath
        )
    # Stream the orchestrator/runner subprocess stdout live to CloudWatch. Python
    # block-buffers stdout when it isn't a TTY, so without this the progress lines
    # (Trigger/KICK/DONE) only flush at process exit — and are LOST if the process
    # is killed mid-run (e.g. the autoscaler scaling the ASG in while a message is
    # in-flight). Unbuffered output survives the kill, so we can see how far the
    # orchestrator got.
    env["PYTHONUNBUFFERED"] = "1"
    env["PRAKTIKA_CONTROLLER_QUEUE"] = queue_name
    if attempt:
        # Surfaced on the GitHub check so cross-instance infra retries are
        # visible (e.g. "attempt 2/3").
        env["PRAKTIKA_ATTEMPT"] = attempt
    if bootstrap_check_id:
        # The orchestrator adopts this pre-clone check run instead of opening
        # a fresh one.
        env["PRAKTIKA_BOOTSTRAP_CHECK_RUN_ID"] = str(bootstrap_check_id)
    if snapshot:
        # The controller establishes the run's single commit (the ephemeral PR
        # merge, or the plain head) and publishes its snapshot BEFORE praktika is
        # reinstalled from the checkout. Hand the pinned identity to the
        # orchestrator so it seeds run state before dispatching any job — the
        # Config job and every downstream job then restore this exact tree, and
        # the DAG matches execution. See merge.prepare_repo_snapshot.
        base_sha, snapshot_sha, repo_snapshot_key = snapshot
        if snapshot_sha:
            env["PRAKTIKA_SNAPSHOT_SHA"] = snapshot_sha
        if repo_snapshot_key:
            env["PRAKTIKA_REPO_SNAPSHOT_KEY"] = repo_snapshot_key
        if base_sha:
            env["PRAKTIKA_BASE_SHA"] = base_sha
    if ci_config:
        # Out-of-repo CI config, resolved once from SSM here and handed to the
        # orchestrator so it freezes it into run state and every job reads it from
        # the task rather than re-reading SSM. See praktika/docs/ci-config.md.
        env["PRAKTIKA_CI_CONFIG"] = json.dumps(ci_config)
    return env


def _run_is_finalized(s3, bucket: str, key: str, log) -> bool:
    """True if the run's state snapshot marks it finalized. A finalized run has
    no live orchestrator to consume this job's result, so running it is wasted
    work (stale/redundant dispatch). Fail open (return False) on any error or a
    missing snapshot so normal/first-run jobs are never blocked."""
    if not bucket or not key:
        return False
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return bool(json.loads(obj["Body"].read()).get("finalized"))
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if str(code) not in {"404", "NoSuchKey", "NotFound"}:
            log.warning(
                "Could not read run state s3://%s/%s: %s: %s",
                bucket, key, type(e).__name__, e,
            )
        return False


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
        s3 = _s3_client()
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


def handle_workflow(
    event, log, queue_name: str, receive_count: int = 1, ci_config=None
):
    wf_type = event.get("type", "unknown")
    log.info("Processing: %s", wf_type)

    # "rerun" resumes a finished run to re-run a failed job (+ its downstream);
    # it reopens the run's existing top-level check itself, so it takes the same
    # clone -> `orchestrate workflow` path but without a fresh bootstrap check.
    # schedule/dispatch are fired by GH_IGNITION workflows via the gh-trigger
    # lambda; they take the same clone -> `orchestrate workflow` path as push
    # (branch in head_ref, no pr_number).
    if wf_type not in ("pull_request", "push", "rerun", "schedule", "dispatch"):
        log.info("Unknown event type: %s, skipping", wf_type)
        return {"status": "skipped", "reason": f"unknown type: {wf_type}"}
    is_resume = wf_type == "rerun"

    repo = event.get("repo", "")
    pr_number = event.get("pr_number")
    head_sha = event.get("head_sha", "")
    branch = event.get("head_ref", "")

    # Capture the controller's FULL handling of this workflow (auth, clone,
    # stale-head guard, runtime resolve) so praktika_debug can attach it to the
    # top-level result. Flushed into the clone dir before launching orchestrate;
    # the orchestrate process (which knows the S3 report prefix) uploads and links
    # it. The event doesn't carry the debug flag, so we always capture (cheap,
    # local file) and let the orchestrate side decide whether to upload. The outer
    # try/finally guarantees the root-logger handler + temp file are cleaned up on
    # every exit — including an auth failure before the clone.
    log_capture = TaskLogCapture(INSTANCE_ID).start()
    try:
        gh_token = get_github_token(REGION)
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=gh_token,
            text=True,
            check=True,
        )

        # Open a check run *before* the clone so the PR shows CI immediately and an
        # interrupted clone still leaves a signal. The orchestrator subprocess
        # adopts this id and renames it to the matched workflow.
        early_check_id = None
        if head_sha and not is_resume:
            # Give the in-progress check a summary so the PR surfaces what the
            # controller is doing (preparing the runtime, and for a PR running
            # the ephemeral merge to check mergeability) and which orchestrator
            # owns the run, before the clone finishes. The orchestrator replaces
            # this once it adopts the check and knows the workflow name.
            prep = "Preparing CI runtime and checking mergeability" if pr_number \
                else "Preparing CI runtime"
            early_summary = f"{prep}…\n\n**Orchestrator instance:** `{INSTANCE_ID}`"
            early_check_id = post_early_check(
                repo, head_sha, gh_token, EARLY_CHECK_NAME, log=log,
                title=prep, summary=early_summary,
            )

        # The run's single commit (base_sha, snapshot_sha, repo_snapshot_key),
        # pinned by the controller before praktika is reinstalled from the
        # checkout. Established below: computed for a fresh PR run (the ephemeral
        # merge) or inherited from run state on a resume. Handed to the
        # orchestrator so it seeds run state before dispatching any job.
        snapshot = None
        # Out-of-repo CI config. The poll loop already read it ONCE for this message
        # (SSM for a fresh run, the frozen copy on the event for a resume) and passed
        # it in, so the self-update decision and the value frozen into run metadata
        # come from the SAME snapshot — never re-read SSM here (a mid-message change
        # would split them). Fall back to reading it only if a caller didn't provide
        # one (e.g. a direct/test call). It gates force_merge_commit
        # (read_repo_settings) and is handed to the orchestrator (PRAKTIKA_CI_CONFIG),
        # which freezes it into run state so every job reads the same values. See
        # ci-config.md.
        if ci_config is None:
            ci_config = (
                (event.get("ci_config") or {})
                if is_resume
                else load_ci_config(region=REGION, log=log)
            )
        resume_snapshot_key = event.get("repo_snapshot_key", "") if is_resume else ""
        resume_snapshot_sha = event.get("snapshot_sha", "") if is_resume else ""
        # Fresh-base resume (per-job "Rerun w/ fresh base" button): re-run the
        # selected job(s) on a NEW ephemeral merge with the CURRENT base tip.
        # Only honored for a finished run — this resume path — the live path never
        # sets it (an in-progress run keeps its existing snapshot).
        fresh_base = bool(event.get("fresh_base")) if is_resume else False

        def _restore_original():
            """Restore the run's ORIGINAL published snapshot (reuse mode)."""
            s3 = _s3_client()
            cd, sha = restore_repo_snapshot(
                s3,
                resume_snapshot_key,
                resume_snapshot_sha,
                pr_number,
                work_dir=WORK_DIR,
                branch=branch,
                log=log,
            )
            return cd, sha, (
                event.get("base_sha", ""),
                resume_snapshot_sha,
                resume_snapshot_key,
            )

        try:
            if is_resume and fresh_base:
                # Re-clone the head and re-run the ephemeral merge against the
                # CURRENT base tip, publishing a NEW snapshot the resumed
                # orchestrator pins (override_repo_snapshot). If the moved base no
                # longer merges cleanly, fall back to the original snapshot so the
                # re-run still proceeds on the base the run was built against.
                clone_dir, actual_sha = clone_repo(
                    repo, head_sha, pr_number, gh_token,
                    work_dir=WORK_DIR, branch=branch, log=log,
                )
                head_commit = head_commit_info(clone_dir, log)
                event["commit_message"] = head_commit["message"]
                event["commit_authors"] = head_commit["authors"]

                # Base-branch history (from the PR merge-base back) and the full PR
                # author set, computed here on the head clone BEFORE any ephemeral
                # merge rewrites HEAD. The controller is the only layer with the
                # repo's real git history in every mode — the snapshot each job
                # restores is history-free — so jobs read these from the run
                # (Info.base_git_history / Info.commit_authors) instead of deriving
                # them locally. Works with snapshot/merge on or off.
                event["commit_authors"], event["base_git_history"] = (
                    compute_run_git_metadata(clone_dir, event, log)
                )
                try:
                    settings = read_repo_settings(clone_dir, log, ci_config=ci_config)
                    if settings.get("ENABLE_S3_REPO_SNAPSHOT"):
                        s3 = _s3_client()
                        snapshot = prepare_repo_snapshot(
                            clone_dir, event, settings, s3, log
                        )
                    # If snapshots are disabled, fresh_base is a no-op: proceed on
                    # the head clone (snapshot stays None), same as a plain resume.
                except MergeConflict as mc:
                    # The PR head no longer merges into the moved base. Do NOT fall
                    # back to the old snapshot — there is no point re-running against
                    # a base the PR can't merge into. Fail the run's top-level check
                    # (run_id is its check-run id) and stop.
                    log.warning("Fresh-base resume merge conflict: %s", mc)
                    files = f"\n{mc.files}" if mc.files else ""
                    finalize_check(
                        repo, event.get("run_id"), gh_token, "failure",
                        "Merge conflict",
                        f"{mc}{files}",
                        log=log,
                    )
                    return {
                        "status": "skipped",
                        "reason": "merge conflict",
                        "pr": pr_number,
                    }
            elif resume_snapshot_key and resume_snapshot_sha:
                # A finished-run resume re-drives the SAME merged tree the original
                # run pinned. Restore it (pure S3, HEAD verified) instead of cloning
                # head, so the reinstalled praktika runtime and the reloaded DAG
                # both match the original merge rather than the current head.
                clone_dir, actual_sha, snapshot = _restore_original()
            else:
                with profile_step(log, "clone: total"):
                    clone_dir, actual_sha = clone_repo(
                        repo,
                        head_sha,
                        pr_number,
                        gh_token,
                        work_dir=WORK_DIR,
                        branch=branch,
                        log=log,
                        # Adopt an idle-warmed base branch if present (no-op cold
                        # clone otherwise); the per-task clone then only applies
                        # the PR delta and the merge needs no unshallow.
                        warm_dir=warm_repo_dir(WORK_DIR),
                    )

                # Stale-head guard (TOCTOU): clone_repo fetches the live
                # refs/pull/N/head, which can have advanced since the lambda verified
                # the head. Running the requested workflow/checks against a different
                # (possibly unapproved fork) commit is unsafe, so abort when the
                # checked-out sha isn't the one the event asked for. Only for PR runs
                # where we have a specific head_sha.
                if pr_number and head_sha and actual_sha and actual_sha != head_sha:
                    log.warning(
                        "PR head advanced (checked out %s != requested %s); aborting to "
                        "avoid running unintended code",
                        actual_sha, head_sha,
                    )
                    finalize_check(
                        repo, early_check_id, gh_token, "cancelled",
                        "Head advanced",
                        f"The PR head moved to {actual_sha[:12]} after this run was "
                        f"requested for {head_sha[:12]}; skipping to avoid running the "
                        f"wrong commit. A run for the new head will proceed.",
                        log=log,
                    )
                    return {"status": "skipped", "reason": "stale head", "sha": actual_sha}

                # Capture the PR head's commit subject + author NOW, while HEAD is
                # the branch head — before the ephemeral merge below rewrites HEAD to
                # the synthetic merge commit. The merged snapshot every job restores
                # is history-free, so the head commit is unreachable downstream; carry
                # these on the event so the report header and every job env show the
                # branch head, not "Merge … into …". Overrides any stale value.
                head_commit = head_commit_info(clone_dir, log)
                event["commit_message"] = head_commit["message"]
                event["commit_authors"] = head_commit["authors"]

                # Base-branch history (from the PR merge-base back) and the full PR
                # author set, computed here on the head clone BEFORE any ephemeral
                # merge rewrites HEAD. The controller is the only layer with the
                # repo's real git history in every mode — the snapshot each job
                # restores is history-free — so jobs read these from the run
                # (Info.base_git_history / Info.commit_authors) instead of deriving
                # them locally. Works with snapshot/merge on or off.
                event["commit_authors"], event["base_git_history"] = (
                    compute_run_git_metadata(clone_dir, event, log)
                )

                # Establish the run's single commit before praktika is reinstalled
                # from the checkout: compute the ephemeral PR merge (or plain head)
                # in place and publish its snapshot. The merge mutates clone_dir, so
                # _resolve_runtime below installs praktika from the MERGED tree, and
                # the orchestrator's DAG (built from the same tree) matches execution.
                try:
                    settings = read_repo_settings(clone_dir, log, ci_config=ci_config)
                    if settings.get("ENABLE_S3_REPO_SNAPSHOT"):
                        s3 = _s3_client()
                        snapshot = prepare_repo_snapshot(
                            clone_dir, event, settings, s3, log
                        )
                except MergeConflict as mc:
                    # A conflict is a deterministic red result, not an infra fault:
                    # finalize the pre-clone check as failed (report ownership stays
                    # here, before the orchestrator exists) and stop — no retry.
                    log.warning("Ephemeral merge conflict: %s", mc)
                    files = f"\n{mc.files}" if mc.files else ""
                    finalize_check(
                        repo, early_check_id, gh_token, "failure",
                        "Merge conflict",
                        f"{mc}{files}",
                        log=log,
                    )
                    return {
                        "status": "skipped",
                        "reason": "merge conflict",
                        "pr": pr_number,
                    }

            with profile_step(log, "runtime: resolve venv + install praktika"):
                base_venv, venv_dir, runtime_pythonpath = _resolve_runtime(
                    clone_dir, log, ci_config=ci_config
                )

            event_file = os.path.join(clone_dir, "ci", "tmp", "event.json")
            os.makedirs(os.path.dirname(event_file), exist_ok=True)
            with open(event_file, "w", encoding="utf-8") as f:
                json.dump(event, f, indent=2)

            # Flush the captured controller log (everything up to here) into the
            # clone's ci/tmp so the orchestrate process can upload it. Capture
            # keeps running until the finally; only these pre-launch lines are
            # uploaded.
            log_capture.save_to(
                os.path.join(clone_dir, "ci", "tmp", "praktika_controller.log")
            )
        except BaseException as e:
            # Failure before the orchestrator subprocess takes over the check
            # (clone, runtime resolution, disk). Finalize the early check so the PR
            # shows the failure rather than a check stuck in_progress. The poll loop
            # still handles retry/replacement as before.
            finalize_check(
                repo,
                early_check_id,
                gh_token,
                "failure",
                "CI failed to start",
                f"Orchestrator could not start the workflow before cloning: {e}",
                log=log,
            )
            raise

        attempt = f"{receive_count}/{INFRA_FAILURE_MAX_RECEIVES}"
        target = f"PR#{pr_number}" if pr_number else f"branch={branch}"
        log.info(
            "Running orchestrator for %s in %s (attempt %s)", target, venv_dir, attempt
        )
        result = subprocess.run(
            praktika_command(venv_dir, "orchestrate", "workflow", event_file, "--ci"),
            cwd=clone_dir,
            env=_praktika_env(
                venv_dir,
                queue_name,
                attempt=attempt,
                bootstrap_check_id=early_check_id,
                snapshot=snapshot,
                ci_config=ci_config,
                pythonpath=runtime_pythonpath,
            ),
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
                f"orchestrator infra failure (rc={INFRA_EXIT_CODE}) on attempt "
                f"{attempt}: {result.stderr.strip()[:300] if result.stderr else ''}"
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
    finally:
        log_capture.stop()
        log_capture.cleanup()


def handle_task(task, log, queue_name: str, receive_count: int = 1):
    task_type = task.get("type", "unknown")
    job_name = task.get("job_name", "?")
    log.info("Processing task: %s job=%r", task_type, job_name)

    if task_type != "job_task":
        log.info("Unknown task type: %s, skipping", task_type)
        return {"status": "skipped", "reason": f"unknown type: {task_type}"}

    repo = task.get("repo", "")
    pr_number = task.get("pr_number")
    head_sha = task.get("head_sha", "")
    # Repo-snapshot mode: set by the orchestrator once the Config Workflow has
    # published the snapshot. Empty when disabled and for the Config Workflow's
    # own task (which clones the head and builds the snapshot).
    snapshot_sha = task.get("snapshot_sha", "")
    repo_snapshot_key = task.get("repo_snapshot_key", "")
    always_run = bool(task.get("always_run", False))
    cancel_s3_bucket = task.get("cancel_s3_bucket", "")
    cancel_s3_key = task.get("cancel_s3_key", "")
    heartbeat_s3_bucket = task.get("heartbeat_s3_bucket", "")
    heartbeat_s3_key = task.get("heartbeat_s3_key", "")
    heartbeat_interval_s = task.get("heartbeat_interval_s", 30)

    s3 = _s3_client()
    if not always_run and _s3_key_exists(s3, cancel_s3_bucket, cancel_s3_key, log):
        log.info(
            "Task %r belongs to a cancelled run, skipping before clone",
            job_name,
        )
        return {"status": "skipped", "reason": "cancelled", "job": job_name}

    # A finalized run has no orchestrator left to consume the result, so a stale
    # or redundant dispatch (e.g. a leftover job_task) would just do pointless
    # work. Skip it. An active re-run writes finalized=false before dispatching,
    # so this never blocks a legitimate resume; missing snapshot -> run (fail open).
    state_s3_key = task.get("state_s3_key", "")
    if _run_is_finalized(s3, cancel_s3_bucket, state_s3_key, log):
        log.info("Task %r belongs to a finalized run, skipping before clone", job_name)
        return {"status": "skipped", "reason": "run finalized", "job": job_name}

    cm_heartbeat = (
        Heartbeat(
            s3,
            heartbeat_s3_bucket,
            heartbeat_s3_key,
            heartbeat_interval_s,
            fields={
                "instance_id": INSTANCE_ID,
                "phase": "picked_up",
                "attempt": receive_count,
            },
            log=log,
        )
        if heartbeat_s3_bucket and heartbeat_s3_key
        else None
    )
    if cm_heartbeat is not None:
        cm_heartbeat.start()

    # When enabled, capture this task's full controller log and upload it next to
    # final.json in the finally below; the job result links to it. Distinct from
    # the runner's task["debug"] (which appends --debug to the job command).
    praktika_debug = bool(task.get("praktika_debug"))
    log_capture = TaskLogCapture(INSTANCE_ID).start() if praktika_debug else None

    proc = None
    try:
        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="cloning")
        if repo_snapshot_key and snapshot_sha:
            # Snapshot restore is pure S3 — no GitHub interaction. Skip
            # authentication so a GitHub token/login outage doesn't fail a job
            # that a valid snapshot could satisfy. Jobs that need `gh` authenticate
            # themselves via GHAuth (enable_gh_auth), independent of this.
            with profile_step(log, "restore: download + verify + unpack snapshot"):
                clone_dir, actual_sha = restore_repo_snapshot(
                    s3,
                    repo_snapshot_key,
                    snapshot_sha,
                    pr_number,
                    work_dir=WORK_DIR,
                    branch=task.get("head_ref", ""),
                    log=log,
                )
        else:
            # Cloning the head needs an authenticated remote.
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
                clean_existing=False,
                log=log,
            )

        if cm_heartbeat is not None:
            cm_heartbeat.update(phase="resolving_runtime")
        with profile_step(log, "runtime: resolve venv + install praktika"):
            # ci_config was frozen into run metadata by the orchestrator and rides
            # on the task, so the job runner installs the pinned Praktika from run
            # metadata, not SSM (see ci-config.md).
            base_venv, venv_dir, runtime_pythonpath = _resolve_runtime(
                clone_dir, log, ci_config=task.get("ci_config") or {}
            )

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
            env=_praktika_env(venv_dir, queue_name, pythonpath=runtime_pythonpath),
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
        if log_capture is not None:
            log_capture.stop()
            fb = task.get("final_state_s3_bucket", "")
            fk = task.get("final_state_s3_key", "")
            if fb and fk:
                controller_key = fk.rsplit("/", 1)[0] + "/praktika_controller.log"
                try:
                    import gzip

                    with open(log_capture.path, "rb") as f:
                        body = gzip.compress(f.read())
                    # text/plain + inline so it opens in a browser rather than
                    # downloading; gzip (with ContentEncoding) so the browser
                    # transparently decompresses. Uploaded via put_object because
                    # upload_file can't set ContentEncoding.
                    s3.put_object(
                        Bucket=fb,
                        Key=controller_key,
                        Body=body,
                        ContentType="text/plain; charset=utf-8",
                        ContentEncoding="gzip",
                        ContentDisposition="inline",
                    )
                    log.info(
                        "Uploaded controller job log to s3://%s/%s", fb, controller_key
                    )
                except Exception as e:
                    log.warning("Failed to upload controller job log: %s", e)
            log_capture.cleanup()


# How long to wait for the ASG to tear the box down before forcing a local
# shutdown as a fallback, and how often to re-check afterwards.
TERMINATION_GRACE_S = 120
TERMINATION_WAIT_POLL_S = 30


def _await_termination(log):
    """Stop polling and block until this instance is terminated.

    We call this right after requesting self-termination. Returning from
    ``poll()`` instead would let the ``Restart=always`` controller unit relaunch
    within seconds — long before the async ASG termination tears the box down —
    and the relaunched controller would poll again and re-receive the message,
    starting a second attempt on an instance that is already going away
    (terminating AND retrying at once). Blocking here keeps us from polling
    until the OS kills the process.

    ASG termination is asynchronous and, rarely, may not take effect (throttled
    API, hung lifecycle hook). After a grace period, force a local shutdown as a
    safety net so a wedged instance never lingers polling; shutdown is itself
    async, so keep blocking afterwards.
    """
    log.info("Termination requested; controller stopped polling, waiting to be terminated")
    time.sleep(TERMINATION_GRACE_S)
    log.warning(
        "Still alive %ss after termination request; forcing local shutdown",
        TERMINATION_GRACE_S,
    )
    try:
        subprocess.Popen(["/sbin/shutdown", "-h", "now"])
    except Exception:
        log.exception("Failed to force local shutdown")
    while True:
        time.sleep(TERMINATION_WAIT_POLL_S)


def poll():
    import boto3

    role, queue_name = _resolve_role_and_queue()
    log_name, _ = _role_config(role)
    log = configure_logging(
        log_name, INSTANCE_ID, os.path.join(WORK_DIR, "praktika-controller.log")
    )
    log.info("Resolved controller role=%s queue=%s", role, queue_name)

    # Converge to the pinned controller BEFORE polling, so a self-update never
    # consumes an SQS delivery (attempt N/3 == ApproximateReceiveCount; the old
    # per-message self-update released the message and restarted, silently burning
    # one delivery on every fresh instance). Only the ORCHESTRATOR reads SSM; job
    # runners receive the pin frozen into their task and converge on the task path
    # in the loop below. Because runners boot from the same AMI as the
    # orchestrator, when the orchestrator needs no self-update a same-version
    # runner won't either.
    warm_cfg = None
    if role == ROLE_WORKFLOW:
        boot_ci_config = load_ci_config(region=REGION, log=log)
        boot_pin = _controller_version_pin(boot_ci_config)
        if boot_pin and maybe_self_update(boot_pin, log):
            log.info("Controller self-update complete at boot; exiting to restart")
            return
        warm_cfg = _warm_clone_config(boot_ci_config)

    sqs = boto3.client("sqs", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    visibility = int(
        sqs.get_queue_attributes(
            QueueUrl=queue_url, AttributeNames=["VisibilityTimeout"]
        )["Attributes"]["VisibilityTimeout"]
    )
    log.info("Role=%s polling %s (visibility_timeout=%ss)", role, queue_url, visibility)

    has_received_message = False
    warmed = False
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
            # Still here = a reserved idle instance. Warm the base branch once (in
            # the background) so the first task's clone only applies the PR delta.
            if warm_cfg and not warmed:
                _start_warm_repo(warm_cfg, log)
                warmed = True
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

            # Read ci_config ONCE for this message: the same snapshot drives the
            # controller self-update decision here and (for a workflow) the value
            # handle_workflow freezes into run metadata — never re-read SSM, or a
            # mid-message change could split the two.
            ci_config = _ci_config_for_message(role, payload, log)

            # Idle boundary: converge to the pinned controller version BEFORE doing
            # any work for this message. When the running controller is NOT the pin,
            # release this task back to the queue FIRST — before the (possibly slow:
            # network download + pip) install — so a controller already on the pinned
            # version can pick it up immediately instead of waiting out our install +
            # restart. Then self-update in place and exit so the Restart=always
            # systemd unit relaunches into the new controller, which resumes normal
            # polling; we deliberately do NOT reclaim this specific task afterwards.
            # (Releasing first also means a crash mid-install can't strand the task
            # until the visibility timeout lapses — the old code held it through the
            # whole install.) No run is interrupted mid-flight. See self_update /
            # ci-config.md.
            pin = _controller_version_pin(ci_config)
            if pin and update_pending(pin):
                try:
                    sqs.change_message_visibility(
                        QueueUrl=queue_url, ReceiptHandle=receipt, VisibilityTimeout=0
                    )
                except Exception:
                    log.exception("Failed to release message before self-update")
                maybe_self_update(pin, log)  # fail hard on a bad pin
                log.info("Controller self-update complete; exiting to restart")
                return

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
                    _await_termination(log)

                if role == ROLE_WORKFLOW:
                    result = handle_workflow(
                        payload,
                        log,
                        queue_name,
                        receive_count=receive_count,
                        ci_config=ci_config,
                    )
                else:
                    result = handle_task(
                        payload, log, queue_name, receive_count=receive_count
                    )
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
                    _await_termination(log)
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

        # One workflow per orchestrator: once its single message is finalized
        # (success, permanent give-up, or malformed), an auto-scaled orchestrator
        # terminates immediately instead of polling for more work or waiting for
        # an idle scale-in. _await_termination (not a bare return) so the
        # Restart=always unit can't relaunch and re-receive the next message.
        # A pinned/dev box (praktika_scaling != "auto") keeps polling.
        if role == ROLE_WORKFLOW and terminate_if_auto_scaled(
            region=REGION, instance_id=INSTANCE_ID, log=log
        ):
            _await_termination(log)


def main():
    if not REGION:
        raise RuntimeError("AWS_DEFAULT_REGION or AWS_REGION must be set")
    poll()


if __name__ == "__main__":
    main()
