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

The current kick/wait implementation is a stub (`kick` just announces the job
and flips it to RUNNING, `wait` immediately completes every RUNNING job as
SUCCESS). The real implementation will launch EC2 runners in `kick` and poll
SSM / SQS / an artifact bus in `wait`.
"""
import json
import os
import time
from collections import defaultdict
from enum import Enum

from . import build_job_dag


# Convention: any `runs_on` label starting with ``praktika-`` is the name of
# its SQS queue (``runs_on = "praktika-arm-2xsmall"`` -> queue
# ``praktika-arm-2xsmall``). Labels that don't match (e.g. ``self-hosted``)
# fall through to the stub — wait() immediately marks them SUCCESS, like
# before — so jobs whose runner type isn't deployed yet still flow through.
_QUEUE_PREFIX = "praktika-"


def _queue_for_runs_on(runs_on):
    """Return the SQS queue name for the first praktika-prefixed label, or None."""
    for label in runs_on or ():
        if label.startswith(_QUEUE_PREFIX):
            return label
    return None


class JobCheckRun:
    """Per-job GitHub check run.

    Lifecycle: ``queue`` creates the check as ``status=queued`` (shows up in
    the PR UI as pending) at the moment the orchestrator kicks the job,
    ``set_in_progress`` flips it once a runner picks the task up, and
    ``complete`` closes it with a conclusion
    (``success``/``failure``/``skipped``/``neutral``). For jobs dispatched
    to a real runner queue the last two transitions happen on the runner
    side via the REST API; for stub jobs the orchestrator drives the whole
    lifecycle itself.
    """

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
    def queue(cls, token, repo, head_sha, name):
        data = cls._api(
            "POST",
            f"https://api.github.com/repos/{repo}/check-runs",
            token,
            {"name": name, "head_sha": head_sha, "status": "queued"},
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
        self.dispatched = False  # True once kick() sends to a real runner queue
        self.rc = None
        self.started_at = None
        self.finished_at = None
        self.filter_reason = None  # set by .skip() when Config Workflow skips it

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
        decides to run the job, not back at workflow-start time.
        """
        if self.check is not None:
            return
        ws = self._workflow_state
        if ws is None or not ws.can_post_checks:
            return
        check_name = f"{ws.workflow.name} / {self.name}"
        try:
            self.check = JobCheckRun.queue(
                ws._gh_token, ws._repo, ws._head_sha, check_name
            )
        except Exception as e:
            print(
                f"  [warn] could not queue check for {check_name!r}: "
                f"{type(e).__name__}: {e}"
            )

    def kick(self):
        """Transition READY -> RUNNING, post the pending check, and dispatch
        to the runner queue if the job's ``runs_on`` includes a
        praktika-prefixed label.

        For dispatched jobs the runner is responsible for flipping the check
        to ``in_progress`` at pickup and for concluding it — the orchestrator
        only queues it here. For stub jobs (no real runner) the orchestrator
        drives the full lifecycle, so we flip to ``in_progress`` right away
        and ``finish`` closes the check.
        """
        if self.status != JobStatus.READY:
            return
        self.status = JobStatus.RUNNING
        self.started_at = time.time()
        runs_on = ", ".join(self.job.runs_on) if self.job.runs_on else "default"

        # Queue the check run at the moment of kick, so nothing shows up on
        # the PR until the orchestrator actually decides to run the job.
        self._create_check()

        queue = _queue_for_runs_on(self.job.runs_on)
        ws = self._workflow_state
        dispatched = False
        if ws is not None and (queue is not None or ws.local_mode):
            dispatched = ws._dispatch(self, queue or "local")

        self.dispatched = dispatched
        tag = "[KICK ]" if dispatched else "[START]"
        suffix = f"  -> {queue or 'local'}" if dispatched else ""
        print(f"{tag} {self.name:70s} runs_on={runs_on}{suffix}")

        # Only stub jobs need the orchestrator to drive the check forward —
        # the runner owns the in_progress/complete transitions otherwise.
        if not dispatched:
            self._update_check(lambda c: c.set_in_progress())

    def finish(self, success=True):
        """Transition RUNNING -> SUCCESS/FAILURE and emit a finish message.

        The check run is concluded here only for stub jobs; dispatched jobs
        have already been concluded by the runner before it posted the
        completion message that triggers this call.
        """
        if self.status != JobStatus.RUNNING:
            return
        self.status = JobStatus.SUCCESS if success else JobStatus.FAILURE
        self.finished_at = time.time()
        self.rc = 0 if success else 1
        duration = self.finished_at - (self.started_at or self.finished_at)
        tag = "[DONE ]" if success else "[FAIL ]"
        print(f"{tag} {self.name:70s} ({duration:.1f}s)")
        if not self.dispatched:
            conclusion = "success" if success else "failure"
            self._update_check(lambda c: c.complete(conclusion))

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

    def cancel(self, reason="run cancelled"):
        """Transition PENDING -> CANCELLED.

        Used for two cases that both produce a Checks API ``cancelled``
        conclusion:
          - the run itself was cancelled (``WorkflowState.cancel_pending_jobs``
            on a new-push or UI Cancel signal);
          - an upstream dep ended in FAILURE or CANCELLED, so this job
            can't run either (``get_ready`` cascade).
        Same no-check-posted rule as ``skip`` — a PENDING job has no
        check yet and we don't create one here.
        """
        if self.status != JobStatus.PENDING:
            return
        self.status = JobStatus.CANCELLED
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
        # Unique identifier for this specific orchestrator run — the GitHub
        # check run ID (string). Used as the suffix of the per-run completions
        # queue, so two concurrent runs for the same PR (e.g. re-runs) have
        # disjoint queues. Falls back to a UUID when running without a check
        # (local mode).
        import uuid
        self._run_id = str(run_id) if run_id else str(uuid.uuid4())
        self._sqs = None                    # lazy boto3 client
        self._queue_urls = {}               # cache: queue name -> URL
        self._completions_queue_url = None  # per-run completions queue
        # Last environment.json snapshot published by a finished job. Seeded
        # into every subsequent dispatched task so WORKFLOW_CONFIG (and other
        # job-side additions) flow forward the same way step outputs do in
        # GHA. Later completions overwrite earlier ones — the serialized
        # environment is already cumulative.
        self._environment = None
        self.cancelled = False              # set by wait() on cancel message

        # Create a per-run completions queue `praktika-wf-{pr}-{run_id}`.
        # One queue per run (not per PR) means there is no cross-run traffic:
        # every message on this queue is addressed to us, so wait() needs no
        # run_id filtering and cancel messages can't be stolen by a
        # concurrent run. The queue is deleted in cleanup() at end of run.
        # Only created when gh_token is set (EC2 mode); in local/dev mode
        # gh_token is absent and wait() falls back to the stub.
        pr = (event or {}).get("pr_number")
        if gh_token and pr:
            self._completions_queue_name = f"praktika-wf-{pr}-{self._run_id}"
            try:
                import boto3
                region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
                self._sqs = boto3.client("sqs", region_name=region)
                resp = self._sqs.create_queue(
                    QueueName=self._completions_queue_name,
                    Attributes={"MessageRetentionPeriod": "3600"},  # 1 h
                )
                self._completions_queue_url = resp["QueueUrl"]
                self._queue_urls[self._completions_queue_name] = self._completions_queue_url
                print(f"Using completions queue: {self._completions_queue_name}")
            except Exception as e:
                print(f"  [warn] could not create completions queue: {type(e).__name__}: {e}")
        else:
            self._completions_queue_name = None

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

    # ---------------------------------------------------------- dispatch

    def _dispatch(self, job_state, queue_name):
        """Send a ``job_task`` message to ``queue_name`` for ``job_state``.

        Returns True on success, False on any failure (missing boto3, queue
        doesn't exist, SQS error). Failures are logged — callers treat a
        non-dispatched job the same as the stub (wait() auto-completes it).
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
            "completions_queue_url": self._completions_queue_url,
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
        """Run the job synchronously as a subprocess."""
        import subprocess
        from ..settings import Settings

        task_file = os.path.join(Settings.TEMP_DIR, f"task_{job_state.name.replace(' ', '_')}.json")
        os.makedirs(Settings.TEMP_DIR, exist_ok=True)
        with open(task_file, "w") as f:
            json.dump(task, f, indent=2)

        result = subprocess.run(["praktika", "orchestrate", "job", task_file])
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

        ``run_unless_cancelled`` jobs (Finish Workflow is the only one
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
            if js.job.run_unless_cancelled:
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

    def cancel_pending_jobs(self):
        """When a cancel signal arrives mid-run, mark every PENDING job
        that isn't flagged ``run_unless_cancelled`` as CANCELLED. Leaves
        the unconditional post-run jobs (Finish Workflow) alone so they
        still fire after their deps settle.
        """
        for js in self.jobs.values():
            if js.status == JobStatus.PENDING and not js.job.run_unless_cancelled:
                js.cancel(reason="run cancelled")

    def wait(self):
        """Block until at least one RUNNING job transitions to a terminal state.

        Long-polls the per-workflow completions queue (created in __init__).
        Falls back to the stub (instant SUCCESS) when no queue is available,
        e.g. in local mode or when SQS credentials are absent.
        """
        # Always complete stub (non-dispatched) RUNNING jobs immediately —
        # they have no real runner and will never send a completion message.
        for js in list(self.jobs.values()):
            if js.status == JobStatus.RUNNING and not js.dispatched:
                js.finish(success=True)

        if not self._completions_queue_url or self._sqs is None:
            return  # no real queue; stub jobs already finished above

        # Only long-poll if there are still dispatched jobs in-flight.
        if not any(js.status == JobStatus.RUNNING and js.dispatched
                   for js in self.jobs.values()):
            return

        resp = self._sqs.receive_message(
            QueueUrl=self._completions_queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=20,
        )
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg["Body"])
                msg_type = body.get("type", "")

                # The queue is per-run, so every message on it is addressed
                # to us: cancel means cancel, job_completion means advance
                # the DAG. No run_id filtering.
                if msg_type == "cancel":
                    print(f"[CANCEL] run {self._run_id}")
                    self.cancelled = True
                elif msg_type == "job_completion":
                    job_name = body.get("job_name", "")
                    rc = body.get("rc", 1)
                    js = self.jobs.get(job_name)
                    if js and js.status == JobStatus.RUNNING:
                        env = body.get("environment")
                        if isinstance(env, dict):
                            self._environment = env
                            wc = env.get("WORKFLOW_CONFIG")
                            if isinstance(wc, dict):
                                self.apply_filtered_jobs(wc.get("filtered_jobs") or {})
                        js.finish(success=(rc == 0))
                    else:
                        print(f"  [warn] completion for unknown/non-running job {job_name!r}")
                else:
                    print(f"  [warn] unknown message type {msg_type!r}")
            except Exception as e:
                print(f"  [warn] malformed completion message: {e}")
            finally:
                self._sqs.delete_message(
                    QueueUrl=self._completions_queue_url,
                    ReceiptHandle=msg["ReceiptHandle"],
                )

    def cleanup(self):
        """Delete the per-run completions queue.

        The queue is single-use: once this run finishes (normal, cancelled,
        or errored) it has no consumers. No retention, no discovery, no
        cross-run traffic — the only lifecycle rule is "the run that created
        it deletes it". If the orchestrator instance dies before reaching
        cleanup (EC2 terminate, crash) the queue is leaked — see PROTOCOL.md
        "Limitations".
        """
        if not self._completions_queue_url or self._sqs is None:
            return
        try:
            self._sqs.delete_queue(QueueUrl=self._completions_queue_url)
            print(f"Deleted completions queue: {self._completions_queue_name}")
        except Exception as e:
            print(
                f"  [warn] could not delete completions queue "
                f"{self._completions_queue_name}: {type(e).__name__}: {e}"
            )

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
