"""Domain entry-point for running a single praktika job on a runner EC2.

``job_agent.py`` (baked into EC2 user_data) handles the stable
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


def _patch_check_run(token, repo, check_id, body):
    """PATCH a GitHub check run. Returns True on success, False on failure
    (never raises — a broken check update must not kill the job)."""
    import requests

    try:
        resp = requests.patch(
            f"https://api.github.com/repos/{repo}/check-runs/{check_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [warn] check run {check_id} PATCH failed: {type(e).__name__}: {e}")
        return False


def _build_check_output(job_name, rc):
    """Load the job's dumped Result from TEMP_DIR and render it as the
    ``output`` dict for a completion PATCH. Returns None on any failure
    so the caller can fall back to a bodyless completion."""
    try:
        from ..result import Result

        result = Result.from_fs(job_name)
        text = result.to_markdown()
        # Check API caps output.text at ~64 KB.
        limit = 60_000
        if len(text) > limit:
            text = text[:limit] + "\n\n_… (truncated)_\n"
        dur = f" in {int(result.duration)}s" if result.duration else ""
        summary = f"{result.status}{dur}"
        return {"title": job_name, "summary": summary, "text": text}
    except Exception as e:
        print(f"  [warn] could not render job Result as MD: {type(e).__name__}: {e}")
        return None


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
        FORK_NAME     — real fork repo for cross-fork PRs (we default to REPOSITORY)
    """
    from .. import Workflow
    from .._environment import _Environment
    from ..settings import Settings
    from ..utils import Shell

    repo = task.get("repo", "")
    pr_number = task.get("pr_number") or -1
    sha = task.get("head_sha", "")

    base_url = f"https://github.com/{repo}"
    change_url = f"{base_url}/pull/{pr_number}" if pr_number > 0 else ""
    commit_url = f"{base_url}/commit/{sha}" if sha else ""

    commit_message = Shell.get_output("git log -1 --pretty=%s HEAD") or ""
    commit_authors = [
        e for e in (Shell.get_output("git log -1 --pretty=%ae HEAD") or "").splitlines()
        if "@" in e
    ]

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
    }

    carried = task.get("environment")
    if isinstance(carried, dict):
        # Hand-off from an upstream job: start from its serialized env and
        # swap in this runner's per-invocation fields.
        env_dict = dict(carried)
        env_dict.update(per_runner)
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
            EVENT_TYPE=Workflow.Event.PULL_REQUEST,
            EVENT_TIME="",
            JOB_OUTPUT_STREAM=job_output,
            EVENT_FILE_PATH=f"{Settings.TEMP_DIR}/event.json",
            CHANGE_URL=change_url,
            COMMIT_URL=commit_url,
            RUN_ID=run_id,
            RUN_URL=change_url,
            INSTANCE_TYPE=instance_type,
            INSTANCE_ID=instance_id,
            INSTANCE_LIFE_CYCLE=instance_life_cycle,
            PR_BODY="",
            PR_TITLE=task.get("title", ""),
            USER_LOGIN=task.get("sender", ""),
            FORK_NAME=repo,
            COMMIT_MESSAGE=commit_message,
            PR_LABELS=task.get("labels", []),
            COMMIT_AUTHORS=commit_authors,
            # TODO: runner.py reads commit_authors via info.get_kv_data("commit_authors")
            # instead of directly from env.COMMIT_AUTHORS — fix that in runner.py so
            # JOB_KV_DATA doesn't need to mirror COMMIT_AUTHORS.
            JOB_KV_DATA={"commit_authors": commit_authors},
            WORKFLOW_CONFIG=None,
            LOCAL_RUN=bool(local_run),
        )
    env.dump()
    return env


def run_job(task, gh_token=None, local=False):
    """Resolve the praktika Workflow + Job from ``task`` and invoke
    ``Runner.run``. Returns the job exit code (0 = success).

    ``gh_token`` is used to drive the GitHub check run the orchestrator
    queued for this job: the runner flips it to ``in_progress`` as soon as
    it picks the task up, and PATCHes it to ``completed`` with the matching
    conclusion once the job finishes.

    ``local=True`` runs the job in dev-sandbox mode (``local_run=True``,
    hooks off). In EC2 polling mode the runner calls with ``local=False``
    so jobs go through the full CI setup/post-run steps.
    """
    workflow_name = task.get("workflow_name", "")
    job_name = task.get("job_name", "")

    if not workflow_name or not job_name:
        print(f"Invalid task: missing workflow_name or job_name: {task}")
        return 1

    # Mark the pending check as in_progress before any real work starts, so
    # the PR UI reflects that a runner has picked the job up.
    check_run_id = task.get("check_run_id")
    repo = task.get("repo", "")
    post_check_updates = bool(check_run_id and gh_token and repo and not local)
    if post_check_updates:
        _patch_check_run(gh_token, repo, check_run_id, {"status": "in_progress"})

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

    jobs = workflow.find_jobs(job_name, lazy=True)
    if not jobs:
        print(f"Job [{job_name}] not found in workflow [{workflow_name}]")
        return 1
    if len(jobs) > 1:
        print(f"Ambiguous job name [{job_name}] in workflow [{workflow_name}]: "
              f"{[j.name for j in jobs]}")
        return 1
    job = jobs[0]

    print(f"Running job [{job.name}] in workflow [{workflow.name}]")

    # Forward optional fields so the orchestrator can parameterize jobs
    # without runner-side changes. Runner asserts XOR(pr, branch), so pass
    # branch only when there's no pr_number (push-triggered workflows).
    pr = task.get("pr_number")
    branch = None if pr else task.get("head_ref")
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

    # Conclude the GitHub check before reporting back to the orchestrator so
    # the PR UI lands on a final state without waiting for the next
    # orchestrator poll. Render the job Result (dumped by runner.py during
    # execution) as Markdown and attach it as output.text so the check
    # displays the per-step/per-test breakdown; fall back silently if the
    # result file isn't on disk (e.g. runner crashed before `result.dump`).
    if post_check_updates:
        body = {
            "status": "completed",
            "conclusion": "success" if rc == 0 else "failure",
        }
        output = _build_check_output(job_name, rc)
        if output is not None:
            body["output"] = output
        _patch_check_run(gh_token, repo, check_run_id, body)

    # Snapshot whatever the job wrote into environment.json and ship it back
    # to the orchestrator. Config Workflow drops a RunConfig in there as
    # WORKFLOW_CONFIG; other jobs may mutate JOB_KV_DATA, REPORT_MESSAGES,
    # COMMIT_AUTHORS, etc. The orchestrator relays this payload into every
    # subsequent job's task so each runner starts from the same environment
    # GHA would have assembled from step outputs.
    env_snapshot = _read_env_file()

    # Report the real result back to the orchestrator so wait() can flip the
    # job to SUCCESS or FAILURE and advance the DAG.
    completions_url = task.get("completions_queue_url")
    if completions_url and not local:
        try:
            import boto3
            sqs = boto3.client("sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            body = {
                "type": "job_completion",
                "job_name": task.get("job_name"),
                "rc": rc,
                "repo": task.get("repo"),
                "pr_number": task.get("pr_number"),
                "head_sha": task.get("head_sha"),
                "workflow_name": task.get("workflow_name"),
            }
            if env_snapshot is not None:
                body["environment"] = env_snapshot
            sqs.send_message(QueueUrl=completions_url, MessageBody=json.dumps(body))
            print(
                f"Sent completion: {task.get('job_name')!r} rc={rc}"
                f"{' +env' if env_snapshot is not None else ''}"
            )
        except Exception as e:
            print(f"  [warn] failed to send completion: {type(e).__name__}: {e}")

    return rc
