"""Runtime state of a workflow execution.

`WorkflowState` is the live, mutable counterpart to the static `Workflow` config:
it owns a `JobState` per job, tracks DAG-ready jobs, and exposes a small
kick/wait interface so the orchestrator's main loop reads as:

    state = WorkflowState(workflow)
    state.print_plan()
    while state.not_finished():
        for job in state.get_ready():
            job.kick()
        state.wait()
    state.print_summary()

`kick` dispatches each job: in CI mode by sending a ``job_task`` to the
runner-specific SQS queue (the runner picks it up, executes, and posts a
``job_completion`` message back); in local mode by running ``praktika
orchestrate job`` synchronously as a subprocess. ``wait`` long-polls the
per-run completions queue in CI mode, and is a no-op in local mode (the
sync subprocess already advanced state by the time it returned).
"""
import json
import os
import time
from collections import defaultdict
from enum import Enum

from . import build_job_dag


# Convention (matches RunnerPool): a workflow's ``runs_on=[X]`` routes to
# the SQS queue ``praktika-X``. The runner pool of the same name listens
# on that queue. So a label of ``arm-2xsmall`` dispatches to queue
# ``praktika-arm-2xsmall``.
_QUEUE_PREFIX = "praktika-"

# Job liveness — S3-based heartbeat (see roadmap). The job agent posts
# ``heartbeat.json`` under ``runs/<run_id>/<job>/`` every
# ``HEARTBEAT_INTERVAL_S``. The orchestrator sweeps RUNNING jobs once per
# wait() cycle and marks them dead under two rules:
#   - never seen heartbeat AND age since kick > PICKUP_GRACE_S → never
#     started (empty pool, agent crash before first heartbeat);
#   - heartbeat seen AND age since last heartbeat > DEAD_THRESHOLD_S →
#     runner died mid-job.
# Pickup grace is generous (5 min) so cold-start clone + pip install does
# not get flagged. Dead threshold is 3× heartbeat to absorb a single miss.
HEARTBEAT_INTERVAL_S = 30
HEARTBEAT_DEAD_THRESHOLD_S = 90
HEARTBEAT_PICKUP_GRACE_S = 300

# wait() blocks for this long between S3 sweeps. Kept short so the
# orchestrator reacts quickly to cancel signals and finished jobs (no
# SQS long-poll any more).
WAIT_POLL_INTERVAL_S = 10


def _normalize_job_name_for_s3(name):
    """Turn a job name into an S3-safe path segment (mirrors job log path)."""
    return name.replace(" ", "_").replace("/", "_")


def _queue_for_runs_on(runs_on):
    """First non-empty ``runs_on`` label → ``praktika-<label>`` queue name."""
    for label in runs_on or ():
        if label:
            return f"{_QUEUE_PREFIX}{label}"
    return None


class JobCheckRun:
    """Per-job GitHub check run.

    Lifecycle: ``queue`` creates the check as ``status=queued`` (shows up in
    the PR UI as pending) at the moment the orchestrator kicks the job,
    ``set_in_progress`` flips it once a runner picks the task up, and
    ``complete`` closes it with a conclusion
    (``success``/``failure``/``skipped``/``neutral``). The
    ``set_in_progress`` and ``complete`` transitions happen on the runner
    side via the REST API once it picks up the job_task message.
    """

    @staticmethod
    def _api(method, url, token, json_body=None):
        import requests

        from .check_run import _resolve_token

        resp = requests.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {_resolve_token(token)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json_body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    @classmethod
    def queue(cls, token, repo, head_sha, name, output=None):
        body = {"name": name, "head_sha": head_sha, "status": "queued"}
        if output is not None:
            body["output"] = output
        data = cls._api(
            "POST",
            f"https://api.github.com/repos/{repo}/check-runs",
            token,
            body,
        )
        return cls(token, repo, data["id"], name)

    def __init__(self, token, repo, id, name):
        self.token = token
        self.repo = repo
        self.id = id
        self.name = name

    def set_in_progress(self):
        self._api(
            "PATCH",
            f"https://api.github.com/repos/{self.repo}/check-runs/{self.id}",
            self.token,
            {"status": "in_progress"},
        )

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


class JobStatus(Enum):
    PENDING = "pending"    # not yet runnable (deps unresolved)
    READY = "ready"        # all deps resolved, queued for kick
    RUNNING = "running"    # kicked, awaiting completion
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"    # didn't need to run — Config Workflow marked the job
                           # out (cache hit, not affected by diff, missing opt-in
                           # label). Not a failure: outputs are still reachable
                           # from S3. SUCCESS-equivalent for dep resolution.
    CANCELLED = "cancelled"  # couldn't run — the run was cancelled (user action,
                             # new push) OR an upstream dep failed (cascade).
                             # Counts as a failure for workflow-level summary.
                             # Maps 1:1 to the Checks API ``cancelled`` conclusion.


_TERMINAL = {
    JobStatus.SUCCESS,
    JobStatus.FAILURE,
    JobStatus.SKIPPED,
    JobStatus.CANCELLED,
}


class JobState:
    """Mutable runtime state for one job in a workflow run."""

    def __init__(self, job, workflow_state=None):
        self.job = job
        self.check = None  # JobCheckRun, created lazily on kick()
        self._workflow_state = workflow_state  # back-ref for SQS dispatch
        self.status = JobStatus.PENDING
        self.rc = None
        self.started_at = None
        self.finished_at = None
        self.filter_reason = None  # set by .skip() when Config Workflow skips it
        # S3-heartbeat liveness. ``last_heartbeat_ts`` stays None until the
        # orchestrator's sweep first sees a heartbeat file in S3; staying
        # None past PICKUP_GRACE_S means the runner never started the job.
        self.last_heartbeat_ts = None

    @property
    def name(self):
        return self.job.name

    def _update_check(self, transition):
        """Run a check-run API call; never let it take down the orchestrator."""
        if self.check is None:
            return
        try:
            transition(self.check)
        except Exception as e:
            print(f"  [warn] check update for {self.name!r}: {type(e).__name__}: {e}")

    def _create_check(self):
        """Queue the GitHub check run (status=queued) — called at kick time.

        Shows up in the PR as a pending check the moment the orchestrator
        decides to run the job, not back at workflow-start time. The check
        output names the target runner pool so reviewers can tell what
        kind of runner the job was dispatched to (and spot when a job is
        stuck waiting on an empty pool).
        """
        if self.check is not None:
            return
        ws = self._workflow_state
        if ws is None or not ws.can_post_checks:
            return
        check_name = f"{ws.workflow.name} / {self.name}"
        runs_on = ", ".join(self.job.runs_on) if self.job.runs_on else "default"
        output = {
            "title": f"Issued to runner: {runs_on}",
            "summary": f"Job dispatched to runner pool `{runs_on}`.",
        }
        try:
            self.check = JobCheckRun.queue(
                ws._gh_token, ws._repo, ws._head_sha, check_name, output=output
            )
        except Exception as e:
            print(
                f"  [warn] could not queue check for {check_name!r}: "
                f"{type(e).__name__}: {e}"
            )

    def kick(self):
        """Transition READY -> RUNNING, post the pending check, and dispatch
        to the runner.

        Two dispatch paths, one print:
          * local mode → ``_dispatch_local`` runs the job synchronously as a
            subprocess and calls ``finish`` before returning;
          * CI mode  → ``_dispatch`` sends a ``job_task`` to the per-runner
            SQS queue and returns immediately; the runner concludes the
            check and posts ``job_completion`` back, which ``wait()`` picks
            up to drive ``finish``.

        Either way the ``[KICK ]`` line is printed before the dispatch call
        so the local subprocess's own output (and the eventual ``[DONE ]``
        from ``finish``) appears beneath it in chronological order.
        """
        if self.status != JobStatus.READY:
            return
        self.status = JobStatus.RUNNING
        self.started_at = time.time()
        runs_on = ", ".join(self.job.runs_on) if self.job.runs_on else "default"

        # Queue the check run at the moment of kick, so nothing shows up on
        # the PR until the orchestrator actually decides to run the job.
        self._create_check()

        ws = self._workflow_state
        target = (
            "local"
            if ws is not None and ws.local_mode
            else _queue_for_runs_on(self.job.runs_on)
        )
        assert target is not None, (
            f"Job {self.name!r} has no dispatch target: runs_on={self.job.runs_on!r} "
            f"and orchestrator is not in local mode"
        )

        print(f"[KICK ] {self.name:70s} runs_on={runs_on}  -> {target}")
        if not ws._dispatch(self, target):
            # Dispatch failed (e.g. SQS error) — fail the job; nothing else
            # will ever drive it forward.
            self.finish(success=False)

    def finish(self, success=True):
        """Transition RUNNING -> SUCCESS/FAILURE and emit a finish line.

        The runner concludes the check itself before posting the completion
        message that drives this call, so the orchestrator does nothing
        check-related here.
        """
        if self.status != JobStatus.RUNNING:
            return
        self.status = JobStatus.SUCCESS if success else JobStatus.FAILURE
        self.finished_at = time.time()
        self.rc = 0 if success else 1
        duration = self.finished_at - (self.started_at or self.finished_at)
        tag = "[DONE ]" if success else "[FAIL ]"
        print(f"{tag} {self.name:70s} ({duration:.1f}s)")

    def skip(self, reason=""):
        """Transition PENDING -> SKIPPED.

        Used when the job doesn't need to run — Config Workflow marked
        it out (cache hit, not affected by diff, missing opt-in label).
        Not a failure: outputs are still reachable from S3.

        No per-job check is posted: with ~100 skipped jobs in a typical
        run the PR UI would be flooded. ``WorkflowState._post_skipped_summary``
        aggregates all skipped jobs into a single ``Skipped Jobs`` check
        with the reasons broken out in Markdown.
        """
        if self.status != JobStatus.PENDING:
            return
        self.status = JobStatus.SKIPPED
        self.filter_reason = reason
        suffix = f" ({reason})" if reason else ""
        print(f"[SKIP ] {self.name:70s}{suffix}")

    def fail_dead(self, reason):
        """Transition RUNNING -> FAILURE because the runner stopped responding.

        Triggered by the orchestrator's heartbeat sweep when the job either
        never started (no heartbeat by ``PICKUP_GRACE_S``) or stopped
        emitting heartbeats (last heartbeat older than
        ``DEAD_THRESHOLD_S``). The runner is presumed gone, so the
        orchestrator completes the check itself with ``failure`` —
        nothing else will ever drive the check forward.
        """
        if self.status != JobStatus.RUNNING:
            return
        self.status = JobStatus.FAILURE
        self.finished_at = time.time()
        self.rc = 1
        output = {"title": reason, "summary": reason}
        self._update_check(lambda c: c.complete("failure", output=output))
        duration = self.finished_at - (self.started_at or self.finished_at)
        print(f"[DEAD ] {self.name:70s} ({duration:.1f}s) {reason}")

    def cancel(self, reason="run cancelled"):
        """Transition PENDING or RUNNING -> CANCELLED.

        Used for two cases that both produce a Checks API ``cancelled``
        conclusion:
          - the run itself was cancelled (``WorkflowState.cancel_unfinished_jobs``
            on a new-push or UI Cancel signal);
          - an upstream dep ended in FAILURE or CANCELLED, so this job
            can't run either (``get_ready`` cascade).
        PENDING jobs have no check-run yet so nothing to patch.
        RUNNING jobs have an in-progress check-run; the orchestrator
        completes it here because the runner will never post back.
        """
        if self.status not in (JobStatus.PENDING, JobStatus.RUNNING):
            return
        was_running = self.status == JobStatus.RUNNING
        self.status = JobStatus.CANCELLED
        if was_running:
            self.finished_at = time.time()
            self._update_check(lambda c: c.complete("cancelled"))
        print(f"[CANCL] {self.name:70s} ({reason})")


class WorkflowState:
    """DAG-aware live state of a workflow run.

    ``event`` (the SQS workflow-trigger body) is stashed so ``JobState.kick``
    can build task messages with the full PR context (repo, pr_number,
    head_sha, head_ref).

    ``gh_token``, ``repo`` and ``head_sha`` are kept so each ``JobState`` can
    queue its own GitHub check run lazily at kick time — nothing is posted
    on the PR until the orchestrator actually decides to run a job.
    """

    def __init__(self, workflow, event=None, gh_token=None, repo=None, head_sha=None, run_id=None, local_mode=False):
        self.workflow = workflow
        self.local_mode = local_mode
        self._event = event or {}
        self._gh_token = gh_token
        self._repo = repo
        self._head_sha = head_sha
        # Event timestamp (lambda receive time). Older runs for the same PR
        # are cancelled when a new event with a larger event_ts triggers the
        # queue-scoped `pr/<pr>/cancel-before-<scope>` marker — see
        # sweep_cancel.
        self._event_ts = float(self._event.get("event_ts") or 0.0)
        self._pr_number = self._event.get("pr_number")
        # Unique identifier for this specific orchestrator run — the GitHub
        # check run ID (string), used as the suffix of the per-run S3 prefix.
        # Falls back to a UUID when running without a check (local mode).
        import uuid
        self._run_id = str(run_id) if run_id else str(uuid.uuid4())
        # Last environment.json snapshot published by a finished job. Seeded
        # into every subsequent dispatched task so WORKFLOW_CONFIG (and other
        # job-side additions) flow forward the same way step outputs do in
        # GHA. Later completions overwrite earlier ones — the serialized
        # environment is already cumulative.
        self._environment = None
        self.cancelled = False  # set by sweep_cancel() on cancel-request / cancel-before

        # S3 client used by sweep_liveness, sweep_completions, sweep_cancel,
        # and the orchestrator → runners kill flag. Only created in CI mode;
        # local mode runs jobs synchronously inside `kick` (no S3 needed).
        if not local_mode:
            import boto3
            region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
            self._s3 = boto3.client("s3", region_name=region)
        else:
            self._s3 = None

        # SQS client is still used by ``_dispatch`` for the per-runner-pool
        # job_task queues (`praktika-<label>`). Phase 2b only retired the
        # per-run completions queue.
        self._sqs = None
        self._queue_urls = {}

        # Per-run S3 prefix. Lambda → orchestrator cancel flows through S3:
        # lambda writes either runs/<run_id>/cancel-request (manual UI button)
        # or a queue-scoped pr/<pr>/cancel-before-<scope> marker (new push,
        # fan-out to older runs in the same orchestrator scope only);
        # sweep_cancel polls both. The orchestrator → runners kill flag at
        # runs/<run_id>/cancel is written by cancel_unfinished_jobs.
        from ..settings import Settings
        self._cancel_s3_bucket = Settings.S3_ARTIFACT_PATH.split("/")[0]
        self._runs_s3_prefix = f"runs/{self._run_id}"
        self._cancel_s3_key = f"{self._runs_s3_prefix}/cancel"
        self._cancel_request_s3_key = f"{self._runs_s3_prefix}/cancel-request"
        queue_name = (os.environ.get("SQS_QUEUE_NAME") or "").strip()
        cancel_scope = "base" if queue_name.endswith("-base") else "default"
        self._pr_cancel_before_s3_key = (
            f"pr/{self._pr_number}/cancel-before-{cancel_scope}"
            if self._pr_number
            else None
        )

        self.jobs = {
            job.name: JobState(job, workflow_state=self) for job in workflow.jobs
        }

        self._levels, job_deps = build_job_dag(workflow)
        self._deps = job_deps
        self._dependents = defaultdict(set)
        for name, deps in job_deps.items():
            for dep in deps:
                self._dependents[dep].add(name)

    @property
    def can_post_checks(self):
        """True iff we have everything needed to open a GitHub check run."""
        return bool(self._gh_token and self._repo and self._head_sha)

    def apply_filtered_jobs(self, filtered):
        """Mark every job Config Workflow filtered out as SKIPPED and post a
        single aggregate check summarising the skips.

        ``filtered`` is ``WORKFLOW_CONFIG.filtered_jobs`` — a
        ``{job_name: reason}`` dict populated by ``_config_workflow`` on the
        runner. Downstream jobs are still considered dispatch-eligible:
        SKIPPED is treated as SUCCESS-equivalent by ``get_ready`` because
        the skipped job's outputs are already in S3 from a prior run.

        Unknown job names are ignored so Config Workflow and the orchestrator
        don't have to agree on the exact set of workflow jobs (e.g. a job
        enabled only in the YAML but removed from the Python config).
        """
        if not filtered:
            return
        applied = []
        for name, reason in filtered.items():
            js = self.jobs.get(name)
            if js is None:
                continue
            if js.status != JobStatus.PENDING:
                continue
            reason = reason or "Filtered by Config Workflow"
            js.skip(reason)
            applied.append((name, reason))
        if applied:
            self._post_skipped_summary(applied)

    def _post_skipped_summary(self, applied):
        """Post one aggregate GitHub check run covering every filtered job.

        Showing one check per skipped job floods the PR (100+ jobs is common),
        so we collapse them into a single ``Skipped Jobs`` check whose
        ``output.text`` is a Markdown breakdown grouped by reason. The check
        is immediately PATCHed to ``completed / skipped``.
        """
        if not self.can_post_checks:
            return
        by_reason = {}
        for name, reason in applied:
            by_reason.setdefault(reason, []).append(name)

        lines = [f"**{len(applied)} jobs skipped by Config Workflow.**", ""]
        for reason in sorted(by_reason):
            names = by_reason[reason]
            lines.append(f"### {reason} — {len(names)}")
            for n in sorted(names):
                lines.append(f"- `{n}`")
            lines.append("")
        text = "\n".join(lines)

        # The Checks API caps output.text at ~64 KB.
        limit = 60_000
        if len(text) > limit:
            text = text[:limit] + "\n\n... (truncated)\n"

        summary = f"{len(applied)} job(s) skipped"
        try:
            check = JobCheckRun.queue(
                self._gh_token, self._repo, self._head_sha, f"{self.workflow.name} / Skipped Jobs"
            )
            check.complete(
                "skipped",
                output={
                    "title": summary,
                    "summary": summary,
                    "text": text,
                },
            )
        except Exception as e:
            print(
                f"  [warn] could not post aggregate skipped-jobs check: "
                f"{type(e).__name__}: {e}"
            )

    # ---------------------------------------------------------- liveness

    def _heartbeat_s3_key(self, job_name):
        return f"{self._runs_s3_prefix}/{_normalize_job_name_for_s3(job_name)}/heartbeat.json"

    def _final_state_s3_key(self, job_name):
        return f"{self._runs_s3_prefix}/{_normalize_job_name_for_s3(job_name)}/final.json"

    def sweep_cancel(self):
        """Detect lambda-driven cancel signals on S3.

        Two channels:
          - ``runs/<run_id>/cancel-request`` (manual UI cancel button) —
            lambda writes this on a check_run.requested_action=cancel
            event addressed to a specific run.
          - ``pr/<pr>/cancel-before-<scope>`` carrying ``{ts}`` (new push)
            — lambda writes this on synchronize. Every still-running
            orchestrator for the same PR and orchestrator scope with
            ``event_ts < ts`` self-cancels; the freshly enqueued run has
            ``event_ts == ts`` and stays alive.

        Both paths set ``state.cancelled = True``; the main loop handles
        that flag exactly once via ``cancel_unfinished_jobs``, so seeing
        the flag again on subsequent sweeps is a no-op.
        """
        if self._s3 is None or self.local_mode or self.cancelled:
            return
        try:
            self._s3.head_object(
                Bucket=self._cancel_s3_bucket, Key=self._cancel_request_s3_key
            )
            print(f"[CANCEL] run {self._run_id} (manual)")
            self.cancelled = True
            return
        except Exception:
            pass  # not present, or transient S3 error — try again next sweep

        if not self._pr_cancel_before_s3_key:
            return
        try:
            obj = self._s3.get_object(
                Bucket=self._cancel_s3_bucket, Key=self._pr_cancel_before_s3_key
            )
            payload = json.loads(obj["Body"].read())
            cancel_before = float(payload.get("ts", 0))
        except Exception:
            return
        if cancel_before > self._event_ts > 0:
            print(
                f"[CANCEL] run {self._run_id} (newer event {cancel_before:.0f} > "
                f"event_ts {self._event_ts:.0f})"
            )
            self.cancelled = True

    def sweep_completions(self):
        """Advance RUNNING jobs whose ``final.json`` has landed in S3.

        Replaces the SQS ``job_completion`` path: the runner writes
        ``runs/<run_id>/<job>/final.json`` with ``{rc, environment, ...}``
        on exit; we read it here and call ``js.finish``. ``finish`` is a
        no-op for non-RUNNING jobs, so seeing the same file twice (e.g.
        across orchestrator restart) is harmless. No-op in local mode and
        when no job is RUNNING.
        """
        if self._s3 is None or self.local_mode:
            return
        running = [js for js in self.jobs.values() if js.status == JobStatus.RUNNING]
        if not running:
            return
        for js in running:
            key = self._final_state_s3_key(js.name)
            try:
                obj = self._s3.get_object(Bucket=self._cancel_s3_bucket, Key=key)
                payload = json.loads(obj["Body"].read())
            except Exception:
                # Not present yet, or transient S3 error — try again next sweep.
                continue
            rc = int(payload.get("rc", 1))
            env = payload.get("environment")
            if isinstance(env, dict):
                self._environment = env
                wc = env.get("WORKFLOW_CONFIG")
                if isinstance(wc, dict):
                    self.apply_filtered_jobs(wc.get("filtered_jobs") or {})
            js.finish(success=(rc == 0))

    def sweep_liveness(self, now=None):
        """Mark RUNNING jobs whose runner stopped responding as FAILURE.

        Reads each RUNNING job's ``heartbeat.json`` from S3 and applies the
        two liveness rules (pickup grace + dead threshold). Called from
        ``wait()`` once per loop iteration. No-op in local mode (no S3
        client) and when nothing is running.
        """
        if self._s3 is None or self.local_mode:
            return
        running = [js for js in self.jobs.values() if js.status == JobStatus.RUNNING]
        if not running:
            return
        now = now if now is not None else time.time()
        for js in running:
            key = self._heartbeat_s3_key(js.name)
            try:
                obj = self._s3.get_object(Bucket=self._cancel_s3_bucket, Key=key)
                body = obj["Body"].read()
                hb = json.loads(body)
                ts = float(hb.get("ts", 0))
                if ts > 0:
                    js.last_heartbeat_ts = ts
            except Exception:
                # Heartbeat file may not exist yet (job still cloning) or S3
                # may have a transient error — both are handled by the age
                # checks below: if pickup grace expires without ever seeing
                # one, we declare the job dead.
                pass

            kicked = js.started_at or now
            age_since_kick = now - kicked
            if js.last_heartbeat_ts is None:
                if age_since_kick > HEARTBEAT_PICKUP_GRACE_S:
                    js.fail_dead(
                        f"runner never started job (no heartbeat in "
                        f"{int(age_since_kick)}s, grace={HEARTBEAT_PICKUP_GRACE_S}s)"
                    )
            else:
                age_since_hb = now - js.last_heartbeat_ts
                if age_since_hb > HEARTBEAT_DEAD_THRESHOLD_S:
                    js.fail_dead(
                        f"runner died (no heartbeat in {int(age_since_hb)}s, "
                        f"threshold={HEARTBEAT_DEAD_THRESHOLD_S}s)"
                    )

    # ---------------------------------------------------------- dispatch

    def _dispatch(self, job_state, queue_name):
        """Send a ``job_task`` message to ``queue_name`` for ``job_state``.

        Returns True on success, False on any failure (missing boto3, queue
        doesn't exist, SQS error). On failure ``kick()`` fails the job —
        nothing else will ever drive it forward.
        """
        task = {
            "type": "job_task",
            "repo": self._event.get("repo", ""),
            "pr_number": self._event.get("pr_number"),
            "head_sha": self._event.get("head_sha", ""),
            "head_ref": self._event.get("head_ref", ""),
            "base_ref": self._event.get("base_ref", ""),
            "sender": self._event.get("sender", ""),
            "title": self._event.get("title", ""),
            "labels": self._event.get("labels", []),
            "workflow_name": self.workflow.name,
            "job_name": job_state.name,
            "runs_on": list(job_state.job.runs_on) if job_state.job.runs_on else [],
            "cancel_s3_bucket": self._cancel_s3_bucket,
            "cancel_s3_key": self._cancel_s3_key,
            "heartbeat_s3_bucket": self._cancel_s3_bucket,
            "heartbeat_s3_key": self._heartbeat_s3_key(job_state.name),
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
            "final_state_s3_bucket": self._cancel_s3_bucket,
            "final_state_s3_key": self._final_state_s3_key(job_state.name),
            "check_run_id": job_state.check.id if job_state.check else None,
            "environment": self._environment,
        }

        if self.local_mode:
            return self._dispatch_local(job_state, task)

        try:
            if self._sqs is None:
                import boto3
                region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
                self._sqs = boto3.client("sqs", region_name=region)

            url = self._queue_urls.get(queue_name)
            if url is None:
                url = self._sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
                self._queue_urls[queue_name] = url

            self._sqs.send_message(QueueUrl=url, MessageBody=json.dumps(task))
            return True
        except Exception as e:
            print(
                f"  [warn] dispatch of {job_state.name!r} to {queue_name!r} failed: "
                f"{type(e).__name__}: {e}"
            )
            return False

    def _dispatch_local(self, job_state, task):
        """Run the job synchronously as a subprocess.

        After the child exits, snapshot ``environment.json`` and store it as
        ``self._environment`` so downstream jobs in the same local run inherit
        whatever the upstream job wrote (most importantly ``WORKFLOW_CONFIG``
        from Config Workflow). In SQS mode this hand-off goes through the
        ``job_completion`` message that ``wait()`` consumes; locally there is
        no message round-trip, so we read the file directly — same result.
        """
        import subprocess
        from ..settings import Settings

        task_file = os.path.join(Settings.TEMP_DIR, f"task_{job_state.name.replace(' ', '_')}.json")
        os.makedirs(Settings.TEMP_DIR, exist_ok=True)
        with open(task_file, "w") as f:
            json.dump(task, f, indent=2)

        env = {**os.environ, "PRAKTIKA_LOCAL_RUN": "1"}
        result = subprocess.run(["praktika", "orchestrate", "job", task_file], env=env)

        env_path = os.path.join(Settings.TEMP_DIR, "environment.json")
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    env_snapshot = json.load(f)
                self._environment = env_snapshot
                wc = env_snapshot.get("WORKFLOW_CONFIG")
                if isinstance(wc, dict):
                    self.apply_filtered_jobs(wc.get("filtered_jobs") or {})
            except Exception as e:
                print(f"  [warn] could not read env snapshot from {env_path}: {e}")

        job_state.finish(success=(result.returncode == 0))
        return True

    # ------------------------------------------------------------ lifecycle

    def not_finished(self):
        """True while any job is still pending / ready / running."""
        return any(j.status not in _TERMINAL for j in self.jobs.values())

    def get_ready(self):
        """Promote PENDING jobs whose deps are resolved -> READY and return them.

        Normal jobs:
          - any dep in FAILURE or CANCELLED ⇒ cascade this job to CANCELLED
            (upstream failed / upstream cancelled, this can't proceed);
          - every dep in SUCCESS or SKIPPED ⇒ promote to READY (SKIPPED
            outputs still exist in S3 from a prior run — SUCCESS-equivalent
            for dep resolution).

        ``always_run`` jobs (Finish Workflow is the only one
        today) promote to READY once every dep reaches *any* terminal
        state, regardless of success/failure/skip/cancel. That's how the
        post-run jobs (CIDB writeback, merge-ready check, Slack notify)
        fire even when the run was cancelled or the DAG failed.
        """
        ready = []
        for name, js in self.jobs.items():
            if js.status != JobStatus.PENDING:
                continue
            dep_states = [self.jobs[d].status for d in self._deps.get(name, ())]
            if js.job.always_run:
                if all(s in _TERMINAL for s in dep_states):
                    js.status = JobStatus.READY
                    ready.append(js)
                continue
            if any(s == JobStatus.FAILURE for s in dep_states):
                js.cancel(reason="upstream failed")
                continue
            if any(s == JobStatus.CANCELLED for s in dep_states):
                js.cancel(reason="upstream cancelled")
                continue
            if all(s in (JobStatus.SUCCESS, JobStatus.SKIPPED) for s in dep_states):
                js.status = JobStatus.READY
                ready.append(js)
        return ready

    def cancel_unfinished_jobs(self):
        """When a cancel signal arrives mid-run, mark every PENDING or
        RUNNING job that isn't flagged ``always_run`` as
        CANCELLED. Leaves unconditional post-run jobs (Finish Workflow)
        alone so they still fire after their deps settle.

        RUNNING jobs that are cancelled here had their task already
        dispatched to a runner. The cancel flag written to S3 signals
        those runners to tear down; the eventual completion message (if
        it still arrives) will be ignored as an unknown job.
        """
        has_running = any(
            js.status == JobStatus.RUNNING and not js.job.always_run
            for js in self.jobs.values()
        )
        for js in self.jobs.values():
            if js.status in (JobStatus.PENDING, JobStatus.RUNNING) and not js.job.always_run:
                js.cancel(reason="run cancelled")
        if has_running and self._s3 is not None:
            try:
                self._s3.put_object(
                    Bucket=self._cancel_s3_bucket,
                    Key=self._cancel_s3_key,
                    Body=b"cancelled",
                )
                print(f"  [cancel] wrote s3://{self._cancel_s3_bucket}/{self._cancel_s3_key}")
            except Exception as e:
                print(f"  [warn] could not write cancel flag: {type(e).__name__}: {e}")

    def wait(self):
        """Block briefly, then sweep S3 for heartbeats / final state / cancel.

        Local mode dispatched the job synchronously inside ``kick`` and the
        job is already in a terminal state by the time we get here; nothing
        to wait on. In CI mode there is no SQS queue any more (phase 2b
        retired the per-run completions queue): wait() simply sleeps for
        ``WAIT_POLL_INTERVAL_S`` and then sweeps the three S3 channels.
        Liveness, completion, and cancel all live under
        ``runs/<run_id>/`` (cancel-by-event-ts also reads
        ``pr/<pr>/cancel-before-<scope>``).
        """
        if self._s3 is None or self.local_mode:
            return

        # Only block if there are still dispatched jobs in-flight.
        if not any(js.status == JobStatus.RUNNING for js in self.jobs.values()):
            return

        time.sleep(WAIT_POLL_INTERVAL_S)
        self.sweep_cancel()
        self.sweep_liveness()
        self.sweep_completions()

    def cleanup(self):
        """End-of-run hook.

        Phase 2b retired the per-run SQS queue, so this is now a no-op.
        Per-run S3 objects under ``runs/<run_id>/`` (heartbeats, final
        states, cancel flags) are intentionally left in place — they are
        useful for debugging and small enough to be cleaned up by the
        bucket's lifecycle policy rather than by the orchestrator at end
        of run.
        """
        return

    # ------------------------------------------------------------ reporting

    def print_plan(self):
        """Print the static execution plan (levels + dependencies)."""
        total_jobs = sum(len(lv) for lv in self._levels)
        print(f"\n{'=' * 80}")
        print(f"Execution plan for workflow [{self.workflow.name}]")
        print(f"Total jobs: {total_jobs}, Execution levels: {len(self._levels)}")
        print(f"{'=' * 80}")
        for i, level in enumerate(self._levels):
            print(f"\n--- Level {i} ({len(level)} jobs, parallel) ---")
            for name in level:
                job = self.jobs[name].job
                deps = self._deps.get(name, set())
                runs_on = ", ".join(job.runs_on) if job.runs_on else "default"
                dep_str = f" <- [{', '.join(sorted(deps))}]" if deps else ""
                provides_str = f" -> [{', '.join(job.provides)}]" if job.provides else ""
                print(f"  {name}")
                print(f"    runner: {runs_on}{dep_str}{provides_str}")
        print(f"\n{'=' * 80}\n")

    def print_summary(self):
        """Print a per-status count of jobs at end of run."""
        counts = defaultdict(int)
        for js in self.jobs.values():
            counts[js.status] += 1
        total = sum(counts.values())
        print(f"\n{'=' * 80}")
        print(f"Workflow [{self.workflow.name}] finished — {total} jobs total")
        for status in JobStatus:
            if counts[status]:
                print(f"  {status.value:10s} {counts[status]}")
        print(f"{'=' * 80}\n")

    def any_failed(self):
        """True if any job ended in a non-success terminal state that
        indicates something actually went wrong.

        FAILURE: the job ran and exited non-zero.
        CANCELLED: the run was cancelled, or an upstream failed and this
        job couldn't run.

        SKIPPED does *not* count here — a skipped job was a deliberate
        decision by Config Workflow ("not affected by this diff" / "cache
        hit"), its outputs are already in S3, the run is healthy.
        """
        return any(
            js.status in (JobStatus.FAILURE, JobStatus.CANCELLED)
            for js in self.jobs.values()
        )

    # ------------------------------------------------------------ markdown

    def md_status_summary(self):
        """One-line summary ("2 success, 1 running, 3 pending") for the
        top-level check's `output.summary`. Uses `JobStatus` values so the
        wording matches `md_status` and `print_summary`."""
        counts = defaultdict(int)
        for js in self.jobs.values():
            counts[js.status] += 1
        bits = [f"{counts[s]} {s.value}" for s in JobStatus if counts[s]]
        return ", ".join(bits) or "no jobs"

    def md_status(self):
        """Markdown snapshot of the current run state for the top-level
        workflow check's `output.text`. Designed to be re-rendered every
        time the state changes — the orchestrator PATCHes the check with
        this on every loop iteration, so the PR UI tracks progress live."""
        event = self._event
        sha = (self._head_sha or "")[:12]
        lines = []
        lines.append(f"**Event:** `{event.get('type', '')}.{event.get('action', '')}`  ")
        if sha:
            lines.append(f"**SHA:** `{sha}`  ")
        pr = event.get("pr_number")
        if pr:
            lines.append(f"**PR:** #{pr}  ")
        lines.append("")
        lines.append(f"**Status:** {self.md_status_summary()}")
        lines.append("")
        lines.append("| Job | Status | Duration |")
        lines.append("|---|---|---|")
        now = time.time()
        for js in self.jobs.values():
            if js.started_at:
                end = js.finished_at or now
                dur = f"{int(end - js.started_at)}s"
            else:
                dur = "—"
            lines.append(f"| `{js.name}` | {js.status.value} | {dur} |")
        return "\n".join(lines)
