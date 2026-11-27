"""Domain entry-point for running a single praktika job on a runner EC2.

``praktika-controller`` handles the stable
infrastructure — SQS poll, clone, GH App token, S3 logs — and then invokes
``praktika orchestrate job task.json --ci`` which lands here. Keeping this
module in the orchestrator package means job-execution policy ships with
each PR: tweaking how a job is looked up or invoked requires only a plain
``git push``, no LT/ASG redeploy.

Expected task fields (SQS message body):

    type:           "job_task"
    repo:           "ClickHouse/clickhouse-private"
    pr_number:      int
    head_sha:       str
    workflow_name:  str   -- praktika workflow the job belongs to (e.g. "PR")
    job_name:       str   -- praktika job name, matched via workflow.find_jobs
    runs_on:        str   -- informational; the queue already enforces routing
    param, test, docker, debug, workers, ...
                    -- optional; forwarded verbatim to Runner.run
"""
import json
import os
import time
from pathlib import Path

from ..mangle import _get_workflows
from ..runner import Runner


def _read_env_file():
    """Return parsed ci/tmp/environment.json as a dict, or None if missing/unreadable."""
    from ..settings import Settings

    path = f"{Settings.TEMP_DIR}/environment.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"  [warn] could not read {path}: {type(e).__name__}: {e}")
        return None


def _current_instance_id():
    return (os.environ.get("INSTANCE_ID") or "").strip()


def _read_job_result(job_name):
    """Load the job's dumped Result from TEMP_DIR and return it as a plain
    dict for the completion payload. The orchestrator reconstructs it to
    render the check-run output and retains the raw structure for AI
    observation. Returns None on any failure so the caller can still
    report rc."""
    try:
        from ..result import Result

        result = Result.from_fs(job_name)
        return Result.to_dict(result)
    except Exception as e:
        print(f"  [warn] could not read job Result: {type(e).__name__}: {e}")
        return None


def _record_job_error(job_name, error_text):
    """Ensure the job's Result reflects a runner-side crash.

    A crash in the run pipeline (e.g. inside ``_post_run``, before it uploads
    the log and finalizes the Result) would otherwise leave ``final.json`` with
    a stale/empty Result — so the orchestrator's check-run and the report show
    a bare failure with no explanation. Load-or-create the Result, mark it
    ERROR, and attach the traceback so the error survives into the check.
    """
    try:
        from ..result import Result

        try:
            result = Result.from_fs(job_name)
        except Exception:
            result = Result.create_new(job_name, Result.Status.ERROR)
        result.set_status(Result.Status.ERROR)
        result.set_info(f"Runner crashed before completing the job:\n{error_text}")
        result.dump()
    except Exception as e:
        print(
            f"  [warn] could not record job error into Result: "
            f"{type(e).__name__}: {e}"
        )


def _build_ci_environment(task, job_name=None, job=None, local_run=False):
    """Construct a `_Environment` from our SQS task and dump it to
    ``ci/tmp/environment.json`` so that ``_Environment.get()`` returns it
    instead of falling through to ``from_env()`` (which would produce a
    dummy environment because GHA env vars are absent).

    If ``task`` carries a full ``environment`` dict (the serialized
    environment.json from a previous job in the same workflow run), that
    payload is used as the base so downstream jobs see everything the
    upstream jobs wrote — WORKFLOW_CONFIG, COMMIT_AUTHORS, JOB_KV_DATA, etc.
    Per-runner fields (JOB_NAME, INSTANCE_*, RUN_ID, ...) are then overwritten
    from the current task / host because they must reflect this invocation,
    not the one that produced the file. When no ``environment`` is carried
    (first job in the run, typically Config Workflow), we build one from the
    task fields alone.

    Fields filled from task:
        WORKFLOW_NAME, JOB_NAME, REPOSITORY, BRANCH, BASE_BRANCH, SHA,
        PR_NUMBER, EVENT_TYPE, USER_LOGIN, PR_TITLE, PR_LABELS, FORK_NAME,
        CHANGE_URL, COMMIT_URL

    Fields derived at runtime (clone is already on disk):
        COMMIT_MESSAGE, COMMIT_AUTHORS  — from git log
        INSTANCE_ID, INSTANCE_TYPE, INSTANCE_LIFE_CYCLE  — from IMDS / env

    TODO — missing, need Lambda / webhook enhancement:
        PR_BODY       — PR description body (not in our webhook payload)
        EVENT_TIME    — PR updated_at timestamp (not captured by Lambda)
    """
    from .. import Workflow
    from .._environment import _Environment
    from ..settings import Settings
    from ..utils import Shell

    repo = task.get("repo", "")
    # GitHub event type ("pull_request", "push", ...). Required: do not guess a
    # default — mislabeling a push as a PR (or vice versa) silently skips
    # push-only steps like image publishing and corrupts CIDB records.
    event_type = task.get("event_type", "")
    assert event_type, f"task is missing 'event_type': {task}"
    if event_type == Workflow.Event.PULL_REQUEST:
        # Real head repo for the PR. Required and never defaulted to the base
        # repo: doing so would make a fork PR look like an internal one and
        # bypass the trust checks that key off FORK_NAME == REPOSITORY.
        head_repo = task.get("head_repo", "")
        assert head_repo, (
            f"pull_request task is missing 'head_repo'; refusing to treat it as "
            f"an internal PR: {task}"
        )
    else:
        # push / dispatch / cron: the ref lives in the base repo itself, so
        # there is no separate head repo.
        head_repo = repo
    pr_number = task.get("pr_number") or 0  # set to 0 for non-pr event (push, dispatch, cron)
    sha = task.get("head_sha", "")

    base_url = f"https://github.com/{repo}"
    change_url = f"{base_url}/pull/{pr_number}" if pr_number > 0 else ""
    commit_url = f"{base_url}/commit/{sha}" if sha else ""

    commit_message = Shell.get_output("git log -1 --pretty=%s HEAD") or ""
    commit_authors = [
        e for e in (Shell.get_output("git log -1 --pretty=%ae HEAD") or "").splitlines()
        if "@" in e
    ]
    # Prefer the PR branch-head commit captured by the controller before the
    # ephemeral merge. When repo snapshots are on, HEAD here is the history-free
    # merge commit, so the git values above describe the synthetic "Merge …" commit
    # (author praktika@localhost) rather than the branch head; the task carries the
    # real head values. Empty when snapshots/merge are disabled -> keep git HEAD.
    commit_message = task.get("commit_message") or commit_message
    commit_authors = task.get("commit_authors") or commit_authors
    # Base-branch history (PR merge-base back, newest first) computed by the
    # controller; empty when unavailable (e.g. a push with no base). Jobs read it
    # via Info.base_git_history.
    base_git_history = task.get("base_git_history") or []

    # For push and merge-queue events there is no PR_NUMBER, but workflow hooks
    # may need the number of the PR this ref corresponds to. Mirror
    # _Environment.from_env: for a push, parse it from the merge commit subject
    # ("Merge pull request #<N> from ..."); for a merge-queue run, parse it from
    # the merge group head ref (".../pr-<N>-...").
    linked_pr_number = 0
    if event_type == Workflow.Event.PUSH:
        parts = commit_message.split("pull request #")
        if len(parts) >= 2:
            pr_str = parts[1].split()[0]
            linked_pr_number = int(pr_str) if pr_str.isdigit() else 0
    elif event_type == Workflow.Event.MERGE_QUEUE:
        head_ref = task.get("head_ref", "")
        if "/pr-" in head_ref:
            pr_number_part = head_ref.split("/pr-")[1].split("-")[0]
            linked_pr_number = int(pr_number_part) if pr_number_part.isdigit() else 0

    instance_id = (
        os.environ.get("INSTANCE_ID")
        or Shell.get_output("curl -sf http://169.254.169.254/latest/meta-data/instance-id")
        or ""
    )
    instance_type = (
        os.environ.get("INSTANCE_TYPE")
        or Shell.get_output("curl -sf http://169.254.169.254/latest/meta-data/instance-type")
        or ""
    )
    instance_life_cycle = (
        os.environ.get("INSTANCE_LIFE_CYCLE")
        or Shell.get_output("curl -sf http://169.254.169.254/latest/meta-data/instance-life-cycle")
        or ""
    )

    job_output = f"{Settings.TEMP_DIR}/job_output"
    Path(Settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
    Path(job_output).touch()

    jname = job_name or (job.name if job else "")
    run_id = (
        f"{instance_id}-{int(time.time())}" if instance_id else str(int(time.time()))
    )

    # Per-runner overrides: everything that depends on WHICH runner is
    # executing the job — never inherited from an upstream job's dump.
    per_runner = {
        "WORKFLOW_NAME": task.get("workflow_name", ""),
        "JOB_NAME": jname,
        "JOB_OUTPUT_STREAM": job_output,
        "RUN_ID": run_id,
        "INSTANCE_ID": instance_id,
        "INSTANCE_TYPE": instance_type,
        "INSTANCE_LIFE_CYCLE": instance_life_cycle,
        "TRACEBACKS": [],
        "LOCAL_RUN": bool(local_run),
        # Attempt number for this run (1-based; 1 = first attempt) = per-job
        # re-run count + 1, plus the attempt's start time for a re-run so test
        # selection can pin its CIDB cutoff. Per-runner: never inherited from an
        # upstream job's dump.
        "RUN_ATTEMPT": int(task.get("rerun_count") or 0) + 1,
        "RUN_ATTEMPT_STARTED_AT": float(task.get("run_attempt_started_at") or 0.0),
        # This run is orchestrator-driven, so the orchestrator is the sole writer
        # of the workflow report summary — job-side report writers stand down (see
        # orchestrator/REPORT_OWNERSHIP.md). False for local runs.
        "ORCHESTRATOR_OWNS_REPORT": not bool(local_run),
        # Repo-snapshot mode: the run's single commit + the snapshot S3 key the
        # controller published, threaded from the task so a job can verify it runs
        # the pinned tree. Empty when snapshots are disabled (jobs clone the head).
        "SNAPSHOT_SHA": task.get("snapshot_sha", ""),
        "REPO_SNAPSHOT_KEY": task.get("repo_snapshot_key", ""),
        # Frozen out-of-repo CI config, threaded from the task (resolved once by
        # the controller). Authoritative from the task on every dispatch, not
        # re-read from SSM. See praktika/docs/ci-config.md.
        "CI_CONFIG": task.get("ci_config") or {},
        # This job's own GitHub check-run id, so the job Result's "Run" link
        # (Info.get_job_url) points at the job's check rather than the workflow's.
        # Per-runner: it identifies THIS job, never inherited from an upstream
        # job's env dump (which would carry the Config job's check id).
        "WORKFLOW_JOB_DATA": {"check_run_id": task.get("check_run_id")},
    }

    carried = task.get("environment")
    if isinstance(carried, dict):
        # Hand-off from an upstream job: start from its serialized env and
        # swap in this runner's per-invocation fields.
        env_dict = dict(carried)
        env_dict.update(per_runner)
        # A fresh-base rerun puts newly-computed run-wide metadata on the task that
        # differs from the original run's serialized env; prefer the task's value
        # when present, else keep the carried (upstream hand-off) value. Guard on
        # task presence: a normal hand-off carries none of these, and an
        # unconditional override would wipe the correct carried values with [].
        if task.get("base_git_history"):
            env_dict["BASE_GIT_HISTORY"] = base_git_history
        if task.get("commit_authors"):
            env_dict["COMMIT_AUTHORS"] = commit_authors
        if task.get("commit_message"):
            env_dict["COMMIT_MESSAGE"] = commit_message
        env = _Environment.from_dict(env_dict)
    else:
        env = _Environment(
            WORKFLOW_NAME=task.get("workflow_name", ""),
            JOB_NAME=jname,
            REPOSITORY=repo,
            BRANCH=task.get("head_ref", ""),
            BASE_BRANCH=task.get("base_ref", ""),
            SHA=sha,
            PR_NUMBER=pr_number,
            EVENT_TYPE=event_type,
            EVENT_TIME="",
            JOB_OUTPUT_STREAM=job_output,
            EVENT_FILE_PATH=f"{Settings.TEMP_DIR}/event.json",
            CHANGE_URL=change_url,
            COMMIT_URL=commit_url,
            RUN_ID=run_id,
            RUN_URL=change_url,
            WORKFLOW_JOB_DATA={"check_run_id": task.get("check_run_id")},
            INSTANCE_TYPE=instance_type,
            INSTANCE_ID=instance_id,
            INSTANCE_LIFE_CYCLE=instance_life_cycle,
            PR_BODY="",
            PR_TITLE=task.get("title", ""),
            PR_IS_DRAFT=bool(task.get("draft", False)),
            USER_LOGIN=task.get("sender", ""),
            FORK_NAME=head_repo,
            COMMIT_MESSAGE=commit_message,
            PR_LABELS=task.get("labels", []),
            COMMIT_AUTHORS=commit_authors,
            LINKED_PR_NUMBER=linked_pr_number,
            # TODO: runner.py reads commit_authors via info.get_kv_data("commit_authors")
            # instead of directly from env.COMMIT_AUTHORS — fix that in runner.py so
            # JOB_KV_DATA doesn't need to mirror COMMIT_AUTHORS.
            JOB_KV_DATA={
                "commit_authors": commit_authors,
                # Initial parent PR inference; can be overridden later via
                # workflow hooks (see Info.set_parent_pr_number).
                "parent_pr_number": linked_pr_number,
            },
            WORKFLOW_CONFIG=None,
            LOCAL_RUN=bool(local_run),
            RUN_ATTEMPT=int(task.get("rerun_count") or 0) + 1,
            RUN_ATTEMPT_STARTED_AT=float(task.get("run_attempt_started_at") or 0.0),
            ORCHESTRATOR_OWNS_REPORT=not bool(local_run),
            SNAPSHOT_SHA=task.get("snapshot_sha", ""),
            REPO_SNAPSHOT_KEY=task.get("repo_snapshot_key", ""),
            CI_CONFIG=task.get("ci_config") or {},
            BASE_GIT_HISTORY=base_git_history,
        )
    env.dump()
    return env


def _resolve_job(workflow, job_name):
    """Find the single Job.Config for ``job_name`` in ``workflow`` (lazy match,
    as the controller passes the dispatched name). Returns the job or None."""
    jobs = workflow.find_jobs(job_name, lazy=True)
    if not jobs:
        print(f"Job [{job_name}] not found in workflow [{workflow.name}]")
        return None
    if len(jobs) > 1:
        print(
            f"Ambiguous job name [{job_name}] in workflow [{workflow.name}]: "
            f"{[j.name for j in jobs]}"
        )
        return None
    return jobs[0]


def run_job(task, gh_token=None, local=False, timestamp=False):
    """Resolve the praktika Workflow + Job from ``task`` and invoke
    ``Runner.run``. Returns the job exit code (0 = success).

    If the resolved job is the head of a cortage (chained jobs on one runner,
    see docs/cortage-jobs.md), this drives the whole chain in-process:
    run the head, then each attached member in order (resume-skipping already
    finalized members, block-skipping members whose data deps failed, stalling on
    external deps), reusing the same checkout/ci_tmp with no clean in between.

    ``gh_token`` is accepted for compatibility with the controller call site,
    but the runner does not mutate GitHub checks directly. It publishes final
    state to S3; the orchestrator owns check transitions.

    ``local=True`` runs the job in dev-sandbox mode (``local_run=True``,
    hooks off). In EC2 polling mode the runner calls with ``local=False``
    so jobs go through the full CI setup/post-run steps.

    ``timestamp=True`` prefixes every job.log line with a wall-clock timestamp
    (the controller passes it so CI job logs carry per-line timing).
    """
    # Log the praktika version that actually runs the job (the installed wheel in
    # this base venv). Printed first, from the runner process itself — which IS
    # that praktika — so it lands in the controller log even if the job fails
    # immediately. Without it there is no way to tell from the logs which praktika
    # a runner executed, e.g. a stale AMI whose baked venv predates a just-shipped
    # wheel.
    try:
        from ..version import current_praktika_version

        print(f"Praktika runtime version: {current_praktika_version()}")
    except Exception as e:
        print(f"Praktika runtime version: unknown ({type(e).__name__}: {e})")

    workflow_name = task.get("workflow_name", "")
    job_name = task.get("job_name", "")

    if not workflow_name or not job_name:
        print(f"Invalid task: missing workflow_name or job_name: {task}")
        return 1

    # Pre-populate ci/tmp/environment.json BEFORE calling _get_workflows(), because
    # _get_workflows() triggers Info() -> _Environment.get() and would fall back to
    # the dummy from_env() path if the file doesn't exist yet.
    _build_ci_environment(task, job_name=job_name, local_run=local)
    # Make sure modules that key off of the env var (e.g. praktika.s3) see local
    # mode regardless of whether they look at the env var or the dumped env.
    if local:
        os.environ["PRAKTIKA_LOCAL_RUN"] = "1"

    workflows = _get_workflows(name=workflow_name)
    if not workflows:
        print(f"Workflow [{workflow_name}] not found")
        return 1
    workflow = workflows[0]

    # Cortage (CI mode only): drive the chain from the dispatched job through the
    # tail in this one invocation. The dispatched job is the head on the normal
    # path, or a later member when the head was cache/filter-skipped and that member
    # became the chain entry. In local mode the orchestrator dispatches each job as
    # its own subprocess, so there is no chain to walk here.
    if not local and len(workflow.get_cortage_chain(workflow.cortage_head_name(job_name))) > 1:
        return _run_cortage_chain(
            task, workflow, job_name, gh_token=gh_token, timestamp=timestamp
        )

    job = _resolve_job(workflow, job_name)
    if job is None:
        return 1
    return _run_single(
        task, workflow, job, gh_token=gh_token, local=local, timestamp=timestamp
    )


def _run_single(task, workflow, job, gh_token=None, local=False, timestamp=False):
    """Run one resolved job through ``Runner.run`` and publish its ``final.json``.
    Returns the job exit code (0 = success)."""
    # Rebuild environment.json for THIS job (JOB_NAME etc.) — cheap and idempotent;
    # necessary per-member when driving a cortage chain.
    _build_ci_environment(task, job_name=job.name, local_run=local)
    if local:
        os.environ["PRAKTIKA_LOCAL_RUN"] = "1"

    instance_id = _current_instance_id()

    # _build_ci_environment dumped environment.json before the job was resolved,
    # so it carries no per-job Job.Config. Set it now (the local_orchestrator_run
    # path in Runner.run skips _setup_env, which is where the GHA path sets
    # env.JOB_CONFIG = job) so job code can read Info().job_config — e.g.
    # stateful_prep_step_timeout, which hard-fails when the job timeout is missing.
    from .._environment import _Environment

    env = _Environment.get()
    env.JOB_CONFIG = job
    env.dump()

    print(f"Running job [{job.name}] in workflow [{workflow.name}]")

    # Forward optional fields so the orchestrator can parameterize jobs
    # without runner-side changes. Runner asserts XOR(pr, branch), so pass
    # branch only when there's no pr_number (push-triggered workflows).
    pr = task.get("pr_number")
    branch = None if pr else task.get("head_ref")
    job_name = job.name
    # Orchestrator-dispatched jobs always go through the full pipeline
    # (pre-run, hooks, post-run, artifact upload) — what differs between
    # `--ci` and local mode is the S3 backend, not the runner shape.
    kwargs = {
        "workflow": workflow,
        "job": job,
        "local_orchestrator_run": True,
        "docker": task.get("docker", ""),
        "no_docker": task.get("no_docker", False),
        "param": task.get("param"),
        "test": task.get("test", ""),
        "pr": pr,
        "sha": task.get("head_sha"),
        "branch": branch,
        "count": task.get("count"),
        "debug": task.get("debug", False),
        "path": task.get("path", ""),
        "path_1": task.get("path_1", ""),
        "workers": task.get("workers"),
        # workflow_dispatch inputs (GH_IGNITION native run) as a dict; Runner.run
        # writes them to WORKFLOW_INPUTS_FILE. None for events without inputs, so
        # the runner clears any stale file instead.
        "workflow_input": task.get("inputs") or None,
        "timestamp": timestamp,
    }

    # Runner.run prints results and sys.exit(1) on failure; a clean return
    # (None) means success.
    import traceback

    rc = 0
    try:
        Runner().run(**kwargs)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
        print(f"Runner.run exited with code {rc}")
    except Exception as e:
        rc = 1
        print(f"Runner.run raised: {type(e).__name__}: {e}")
        traceback.print_exc()
        # Surface the crash in the report / GH check: without this the failure
        # is only in stderr and the orchestrator completes the check with no
        # explanation (the job's Result was never finalized/dumped).
        _record_job_error(job_name, traceback.format_exc())

    try:
        from ..info import Info
        report_url = Info().get_job_report_url()
    except Exception:
        report_url = ""
    result_dict = _read_job_result(job_name)

    # Snapshot whatever the job wrote into environment.json and ship it back
    # to the orchestrator. Config Workflow drops a RunConfig in there as
    # WORKFLOW_CONFIG; other jobs may mutate JOB_KV_DATA, REPORT_MESSAGES,
    # COMMIT_AUTHORS, etc. The orchestrator relays this payload into every
    # subsequent job's task so each runner starts from the same environment
    # GHA would have assembled from step outputs.
    env_snapshot = _read_env_file()

    # Report the final state to the orchestrator via S3: the orchestrator's
    # wait() polls runs/<run_id>/<job>/final.json and advances the DAG from
    # there. S3 is durable, so a restart of the orchestrator after this write
    # still picks the result up.
    final_bucket = task.get("final_state_s3_bucket", "")
    final_key = task.get("final_state_s3_key", "")

    # Link the controller's full per-job log (clone/restore/dispatch/teardown),
    # which the controller uploads next to final.json after this process exits.
    # Same S3 coordinates as final.json, so no extra plumbing.
    if (
        task.get("praktika_debug")
        and result_dict is not None
        and final_bucket
        and final_key
    ):
        try:
            from ..settings import Settings

            controller_key = final_key.rsplit("/", 1)[0] + "/praktika_controller.log"
            endpoint = (Settings.S3_BUCKET_TO_HTTP_ENDPOINT or {}).get(
                final_bucket, f"{final_bucket}.s3.amazonaws.com"
            )
            controller_url = f"https://{endpoint}/{controller_key}"
            links = result_dict.setdefault("links", [])
            if controller_url not in links:
                links.append(controller_url)
        except Exception as e:
            print(
                f"  [warn] failed to add controller-log link: {type(e).__name__}: {e}"
            )

    if final_bucket and final_key and not local:
        import boto3

        from ..settings import Settings

        # Resolve region like S3._get_boto3_client: Settings.AWS_REGION when set,
        # else None so boto3 picks it up from the environment / instance metadata.
        # Never assume a hardcoded region.
        s3 = boto3.client("s3", region_name=Settings.AWS_REGION or None)
        body = {
            "type": "job_completion",
            "job_name": task.get("job_name"),
            "rc": rc,
            "ts": time.time(),
            "repo": task.get("repo"),
            "pr_number": task.get("pr_number"),
            "head_sha": task.get("head_sha"),
            "workflow_name": task.get("workflow_name"),
            # Which re-run attempt produced this result (0 = first run). Lets
            # a CIDB consumer tell a clean run's usage row from one whose
            # totals were affected by re-runs.
            "rerun_count": int(task.get("rerun_count") or 0),
        }
        if env_snapshot is not None:
            body["environment"] = env_snapshot
        if instance_id:
            body["instance_id"] = instance_id
        if result_dict is not None:
            body["result"] = result_dict
        if report_url:
            body["details_url"] = report_url
        # final.json is the SOLE completion signal the orchestrator's
        # sweep_completions reads, and this job's SQS task is acked once run_job
        # returns — there is no redelivery. A transient put failure must be
        # retried, or the orchestrator waits for a result that never lands and
        # only declares the (already succeeded) job dead at the liveness timeout.
        attempts = 5
        last_err = None
        for attempt in range(attempts):
            try:
                s3.put_object(
                    Bucket=final_bucket,
                    Key=final_key,
                    Body=json.dumps(body).encode(),
                    ContentType="application/json",
                )
                print(
                    f"Wrote final state s3://{final_bucket}/{final_key} "
                    f"rc={rc}{' +env' if env_snapshot is not None else ''}"
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(
                    f"  [warn] failed to write final state "
                    f"(attempt {attempt + 1}/{attempts}): {type(e).__name__}: {e}"
                )
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 10))
        if last_err is not None:
            print(
                f"  [error] final state write failed after {attempts} attempts: "
                f"{type(last_err).__name__}: {last_err}"
            )

    return rc


# --------------------------------------------------------------------------
# Cortage chain driver (see docs/cortage-jobs.md)
# --------------------------------------------------------------------------

# Filename (in the shared ci/tmp) of the heartbeat pointer the controller's
# Heartbeat follows. Must match controller.py's set_pointer_file path.
_HB_POINTER_NAME = "cortage_heartbeat.json"
_DEP_SUCCESS_STATUSES = {"success", "skipped"}
_DEP_FAIL_STATUSES = {"failure", "cancelled", "error", "dropped"}


def _normalize_job_name_for_s3(name):
    """S3-safe path segment for a job name. Mirrors the orchestrator's
    _normalize_job_name_for_s3 (state.py) so member keys match byte-for-byte."""
    return name.replace(" ", "_").replace("/", "_")


def _s3_client():
    import boto3

    from ..settings import Settings

    return boto3.client("s3", region_name=Settings.AWS_REGION or None)


def _load_final(s3, bucket, key):
    """Return the ``final.json`` payload if the member already reached a TERMINAL
    state on a prior runner (success OR failure), or None only when the object is
    CONFIRMED ABSENT (the member has not finalized). On resume we must not re-run
    EITHER a success or a failure: re-running repeats side effects and can flip a
    finalized failure to success while the orchestrator already cancelled its
    dependents. A re-run (not a resume) deletes final.json via _reset_job, so reset
    members correctly read as absent and do re-run.

    A transient/permission S3 error is NOT "absent": returning None on those would
    re-run an already-finalized member. Retry a few times and, if the error
    persists, RAISE so the chain aborts rather than silently re-running — far safer
    than repeating a finalized member's side effects."""
    from .state import _is_missing_s3_key_error

    attempts = 5
    last_err = None
    for attempt in range(attempts):
        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            return json.loads(obj["Body"].read())
        except Exception as e:
            if _is_missing_s3_key_error(e):
                return None  # confirmed not finalized -> the member should run
            last_err = e
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(
        f"could not read cortage member final state s3://{bucket}/{key} after "
        f"{attempts} attempts; aborting the chain instead of risking a re-run of an "
        f"already-finalized member: {type(last_err).__name__}: {last_err}"
    )


def _payload_is_blocking_failure(payload):
    """True if a final.json payload is a BLOCKING failure (rc != 0 and its Result is
    not do_not_block_pipeline_on_failure). Used on resume so an already-finalized
    blocking failure still block-skips its dependents, matching the orchestrator."""
    try:
        if int(payload.get("rc", 1)) == 0:
            return False
        from copy import deepcopy

        from ..result import Result

        rd = payload.get("result")
        if not isinstance(rd, dict):
            return True
        return not Result.from_dict(deepcopy(rd)).do_not_block_pipeline_on_failure()
    except Exception:
        return True


def _is_blocking_failure(job_name):
    """True if the member's dumped Result represents a BLOCKING failure. A Result
    with do_not_block_pipeline_on_failure() is advisory (success-equivalent for
    dependency resolution, as in WorkflowState.get_ready), so it must NOT block
    downstream members. An unreadable Result defaults to blocking (safe)."""
    try:
        from ..result import Result

        return not Result.from_fs(job_name).do_not_block_pipeline_on_failure()
    except Exception:
        return True


def _write_terminal_failure(s3, member_task, info):
    """Publish a terminal FAILURE ``final.json`` for a member the runner won't run
    (e.g. an external-dep stall that timed out). Without it the orchestrator — which
    never cancelled the member — would wait out the much longer liveness timeout."""
    bucket = member_task.get("final_state_s3_bucket") or ""
    key = member_task.get("final_state_s3_key") or ""
    name = member_task.get("job_name") or ""
    if not (bucket and key):
        return
    try:
        from ..result import Result

        res = Result.create_new(name, Result.Status.ERROR, info=info)
        body = {
            "type": "job_completion",
            "job_name": name,
            "rc": 1,
            "ts": time.time(),
            "result": Result.to_dict(res),
        }
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(body).encode(),
            ContentType="application/json",
        )
        print(f"Cortage: wrote terminal failure for [{name}] (s3://{bucket}/{key}): {info}")
    except Exception as e:
        print(
            f"  [warn] could not write terminal failure for [{name}]: "
            f"{type(e).__name__}: {e}"
        )


def _load_state_jobs(s3, bucket, key):
    """Return the ``jobs`` map from runs/<run_id>/state.json, or {} if unreadable."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return (json.loads(obj["Body"].read()) or {}).get("jobs", {}) or {}
    except Exception:
        return {}


def _config_skipped_names(carried_env):
    """Job names Config Workflow marked cache-hit/filtered, read from the carried
    WORKFLOW_CONFIG. Authoritative the instant Config Workflow finishes (it is often
    the cortage entry), without waiting for the orchestrator to apply it to
    state.json. See apply_workflow_config in orchestrator/state.py."""
    names = set()
    wc = carried_env.get("WORKFLOW_CONFIG") if isinstance(carried_env, dict) else None
    if isinstance(wc, dict):
        names |= set(wc.get("filtered_jobs") or {})
        names |= set(wc.get("cache_success") or [])
    return names


def _artifact_provider_map(workflow):
    provider = {}
    for job in workflow.jobs:
        for artifact_name in getattr(job, "provides", None) or []:
            provider[artifact_name] = job.name
    return provider


def _dep_job_names(job, provider_map, all_job_names):
    """Resolve a job's requires/run_after to the set of job names it depends on
    (requires may name an artifact → its producing job, or a job directly)."""
    names = set()
    for req in getattr(job, "requires", None) or []:
        if req in provider_map:
            names.add(provider_map[req])
        elif req in all_job_names:
            names.add(req)
    for ra in getattr(job, "run_after", None) or []:
        if ra in all_job_names:
            names.add(ra)
    return names


def _write_hb_pointer(heartbeat_s3_key, phase):
    """Point the controller's Heartbeat at the current member's key + phase."""
    from ..settings import Settings

    try:
        Path(Settings.TEMP_DIR).mkdir(parents=True, exist_ok=True)
        with open(
            Path(Settings.TEMP_DIR) / _HB_POINTER_NAME, "w", encoding="utf-8"
        ) as f:
            json.dump({"heartbeat_s3_key": heartbeat_s3_key, "phase": phase}, f)
    except Exception as e:
        print(f"  [warn] could not write heartbeat pointer: {type(e).__name__}: {e}")


def _clear_hb_pointer():
    from ..settings import Settings

    try:
        (Path(Settings.TEMP_DIR) / _HB_POINTER_NAME).unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [warn] could not clear heartbeat pointer: {type(e).__name__}: {e}")


def _wait_external_deps(
    s3, bucket, state_key, external_deps, member_name, hb_key, always_run=False
):
    """Block until every external (non-cortage) dep of a member reaches a terminal
    state, polling runs/<run_id>/state.json. Returns one of:
      "ok"         — all deps success/skipped (a FAILURE marked non_blocking counts
                     as success-equivalent, matching WorkflowState.get_ready);
      "dep_failed" — a dep ended in a blocking failure/cancelled: the orchestrator
                     cancels this member via cascade, so the caller just skips it;
      "timeout"    — the stall exceeded Settings.CORTAGE_MAX_STALL_SEC with deps
                     still pending: the orchestrator has NOT cancelled it, so the
                     caller must publish a terminal failure for it.
    No-op → "ok" when there are no external deps. While stalled it keeps the
    heartbeat pointer on this member with a phase naming what it waits on, so the
    controller's heartbeat surfaces the stall.

    `always_run` members (e.g. Finish Workflow) must run regardless of how the
    DAG ends: a failed/cancelled dep no longer yields "dep_failed" (it counts as
    settled), and the stall never times out — the member waits until every dep is
    terminal, then runs. This matches the orchestrator never cancelling an
    always_run job.
    """
    if not external_deps:
        return "ok"
    from ..settings import Settings

    deadline = time.time() + max(1, int(Settings.CORTAGE_MAX_STALL_SEC))
    # Same handoff-reactivity knob the orchestrator's wait() uses: when an
    # external dep finalizes mid-interval the member resumes within one poll.
    # Each poll is a single GET of the run's state.json, so a shorter interval
    # is a cost (not rate-limit) lever even with many heads stalling at once.
    poll_interval = int(getattr(Settings, "WAIT_POLL_INTERVAL_S", 2) or 2)
    while True:
        jobs = _load_state_jobs(s3, bucket, state_key)
        pending = []
        for dep in external_deps:
            info = jobs.get(dep) or {}
            status = str(info.get("status", "")).lower()
            if status in _DEP_SUCCESS_STATUSES:
                continue
            # An advisory failure (do_not_block_pipeline_on_failure) is treated as
            # success-equivalent for dep resolution, exactly like get_ready does.
            if status == "failure" and info.get("non_blocking"):
                continue
            if status in _DEP_FAIL_STATUSES:
                if always_run:
                    # A dep ended in failure/cancelled, but this member runs
                    # regardless — count the dep as settled and keep waiting on
                    # the rest.
                    continue
                print(
                    f"Cortage: member [{member_name}] external dep [{dep}] is "
                    f"[{status}] — the cortage cannot proceed"
                )
                return "dep_failed"
            pending.append(dep)
        if not pending:
            return "ok"
        if not always_run and time.time() >= deadline:
            print(
                f"Cortage: member [{member_name}] stall timed out after "
                f"{Settings.CORTAGE_MAX_STALL_SEC}s waiting on {pending}"
            )
            return "timeout"
        waited = int(time.time() - (deadline - Settings.CORTAGE_MAX_STALL_SEC))
        print(
            f"Cortage: member [{member_name}] stalling on {pending} "
            f"({waited}s elapsed)"
        )
        _write_hb_pointer(
            hb_key, phase=f"stalling: waiting on {','.join(pending)} ({waited}s)"
        )
        time.sleep(poll_interval)


def _member_task(task, member_name, run_id, bucket, carried_env):
    """A per-member copy of the head task: same event/ci_config/snapshot, but the
    member's own job_name, deterministic heartbeat/final keys, and the environment
    handed off from the previous member. check_run_id is dropped (the orchestrator
    owns the member's check; the job just loses a self-link)."""
    norm = _normalize_job_name_for_s3(member_name)
    member_task = dict(task)
    member_task["job_name"] = member_name
    member_task["heartbeat_s3_bucket"] = bucket
    member_task["heartbeat_s3_key"] = f"runs/{run_id}/{norm}/heartbeat.json"
    member_task["final_state_s3_bucket"] = bucket
    member_task["final_state_s3_key"] = f"runs/{run_id}/{norm}/final.json"
    member_task["check_run_id"] = None
    if carried_env is not None:
        member_task["environment"] = carried_env
    return member_task


def _run_cortage_chain(task, workflow, start_name, gh_token=None, timestamp=False):
    """Run a cortage from the dispatched entry job (``start_name``) through the tail
    on this one runner, reusing the shared checkout/ci_tmp with no clean in between.
    ``start_name`` is the head on the normal path, or a later member when the head
    was cache/filter-skipped and that member became the chain entry. Returns the max
    exit code across the members that actually ran."""
    from ..settings import Settings

    full_chain = workflow.get_cortage_chain(workflow.cortage_head_name(start_name))
    chain_set = set(full_chain)
    start_idx = full_chain.index(start_name) if start_name in full_chain else 0
    members = full_chain[start_idx:]
    print(
        f"Cortage entry [{start_name}] driving members {' -> '.join(members)} "
        f"(full chain: {' -> '.join(full_chain)})"
    )

    run_id = task.get("run_id") or ""
    bucket = (
        task.get("final_state_s3_bucket")
        or task.get("cancel_s3_bucket")
        or Settings.S3_ARTIFACT_BUCKET
    )
    state_key = task.get("state_s3_key") or f"runs/{run_id}/state.json"
    s3 = _s3_client()
    provider_map = _artifact_provider_map(workflow)
    all_job_names = {j.name for j in workflow.jobs}

    failed_or_skipped = set()
    overall_rc = 0
    carried_env = task.get("environment")

    for member_name in members:
        is_entry = member_name == start_name
        member = _resolve_job(workflow, member_name)
        if member is None:
            print(f"ERROR: cortage member [{member_name}] not found — aborting chain")
            overall_rc = 1
            break
        member_task = (
            task
            if is_entry
            else _member_task(task, member_name, run_id, bucket, carried_env)
        )

        # Resume: a member already finalized on a prior runner (infra redelivery
        # re-runs the entry task). Do NOT re-run it — whether it succeeded OR failed
        # — and restore its environment so the next member inherits
        # WORKFLOW_CONFIG/JOB_KV_DATA/etc. If it was a BLOCKING failure, record it so
        # its data-dependents are block-skipped, exactly as the orchestrator cascades
        # (re-running it could flip it to success and wrongly unblock them). A
        # re-run instead deletes final.json (_reset_job), so reset members are not
        # seen here and do re-run.
        done = _load_final(s3, bucket, member_task["final_state_s3_key"])
        if done is not None:
            env = done.get("environment")
            if isinstance(env, dict):
                carried_env = env
            if _payload_is_blocking_failure(done):
                failed_or_skipped.add(member_name)
                print(
                    f"Cortage: member [{member_name}] already finalized as a "
                    f"blocking failure — not re-running; its dependents will be skipped"
                )
            else:
                print(
                    f"Cortage: member [{member_name}] already finalized "
                    f"(rc={int(done.get('rc', 1))}) — skipping"
                )
            continue

        # Config Workflow marked this member cache-hit/filtered: do NOT run it. Its
        # outputs (for a cache hit) live in S3, so downstream members fetch them via
        # the normal artifact path. Two authoritative sources, checked per member:
        #   - the freshly carried WORKFLOW_CONFIG (available the instant Config
        #     Workflow — possibly the cortage entry — finishes, with no orchestrator
        #     lag); and
        #   - state.json reloaded each iteration (skips the orchestrator has applied).
        state_jobs = _load_state_jobs(s3, bucket, state_key)
        config_skipped = _config_skipped_names(carried_env)
        if (
            member_name in config_skipped
            or str(state_jobs.get(member_name, {}).get("status", "")).lower()
            == "skipped"
        ):
            print(
                f"Cortage: member [{member_name}] is SKIPPED (cache/filter) — not running"
            )
            continue

        # An always_run member (e.g. Finish Workflow chained as the cortage tail)
        # runs regardless of how the DAG ends — it is never blocked by a failed
        # dep and never skipped, mirroring the orchestrator never cancelling it.
        always_run = getattr(member, "always_run", False)

        deps = _dep_job_names(member, provider_map, all_job_names)
        # Req #6: a member whose DATA dep (an earlier member) failed/was-skipped is
        # blocked; the orchestrator cancels it via cascade, so we just skip it (and
        # mark it so its own dependents skip too). Members not data-blocked run.
        blocked_by = (deps & chain_set) & failed_or_skipped
        if blocked_by and not always_run:
            print(
                f"Cortage: member [{member_name}] blocked by failed member(s) "
                f"{sorted(blocked_by)} — skipping (orchestrator cancels it)"
            )
            failed_or_skipped.add(member_name)
            continue

        if not is_entry:
            # External (non-cortage) deps weren't a dispatch gate — stall until they
            # resolve.
            _write_hb_pointer(member_task["heartbeat_s3_key"], phase="queued")
            verdict = _wait_external_deps(
                s3,
                bucket,
                state_key,
                sorted(deps - chain_set),
                member_name,
                member_task["heartbeat_s3_key"],
                always_run=always_run,
            )
            if verdict == "dep_failed":
                # The orchestrator cancels this member via cascade — just skip it.
                failed_or_skipped.add(member_name)
                continue
            if verdict == "timeout":
                # The dep is still pending, so the orchestrator has NOT cancelled the
                # member: publish a terminal failure so it doesn't hang on the
                # liveness timeout, and fail the chain for its dependents.
                _write_terminal_failure(
                    s3,
                    member_task,
                    f"Cortage member stalled longer than "
                    f"{Settings.CORTAGE_MAX_STALL_SEC}s waiting on external "
                    f"dependencies {sorted(deps - chain_set)}",
                )
                failed_or_skipped.add(member_name)
                overall_rc = max(overall_rc, 1)
                continue
            _write_hb_pointer(member_task["heartbeat_s3_key"], phase="running_job")

        rc = _run_single(
            member_task, workflow, member, gh_token=gh_token, local=False, timestamp=timestamp
        )
        overall_rc = max(overall_rc, rc)
        # Only a BLOCKING failure blocks downstream members: a member whose Result
        # sets do_not_block_pipeline_on_failure is success-equivalent to the
        # orchestrator (get_ready), which makes its dependents ready — so we must
        # not block-skip them. Match that here (an unreadable Result → treat as
        # blocking, the safe default).
        if rc != 0 and _is_blocking_failure(member_name):
            failed_or_skipped.add(member_name)
        # Hand this member's environment.json off to the next member (WORKFLOW_CONFIG,
        # JOB_KV_DATA, ...), mirroring how the orchestrator relays env between jobs.
        snapshot = _read_env_file()
        if snapshot is not None:
            carried_env = snapshot

    _clear_hb_pointer()
    return overall_rc
