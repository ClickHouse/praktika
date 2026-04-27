import io
import json
import re
import sys
from collections import defaultdict, deque
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Optional

from ..mangle import _get_artifact_to_providing_job_map, _get_workflows
from ..workflow import Workflow


# Map SQS event types to Workflow.Event values
_EVENT_MAP = {
    "pull_request": Workflow.Event.PULL_REQUEST,
    "push": Workflow.Event.PUSH,
}


def _branch_matches(branch, patterns):
    """Check if branch matches any of the patterns (exact or regex)."""
    for pattern in patterns:
        if pattern == branch:
            return True
        if re.fullmatch(pattern, branch):
            return True
    return False


def find_workflows_for_event(event):
    """Find all workflows matching the trigger event. Returns empty list if no match."""
    event_type = event.get("type", "")
    workflow_event = _EVENT_MAP.get(event_type)
    if not workflow_event:
        print(f"No workflow event mapping for trigger type [{event_type}]")
        return []

    if event_type == "pull_request":
        branch = event.get("base_ref", "")
    elif event_type == "push":
        branch = event.get("branch", "")
    else:
        branch = ""

    matched = []
    for wf in _get_workflows():
        if wf.engine == "GHActions":
            continue
        if wf.event != workflow_event:
            continue
        if event_type == "pull_request" and wf.base_branches:
            if _branch_matches(branch, wf.base_branches):
                matched.append(wf)
        elif event_type == "push" and wf.branches:
            if _branch_matches(branch, wf.branches):
                matched.append(wf)

    if not matched:
        print(f"No workflow found for event [{workflow_event}] branch [{branch}]")
    return matched


def build_job_dag(workflow):
    """Build the dependency DAG for a workflow's jobs.

    Returns (levels, job_deps) where:
      - levels: list of lists, each inner list is a set of jobs that can run in parallel
      - job_deps: dict mapping job name -> set of job names it depends on
    """
    jobs_by_name = {job.name: job for job in workflow.jobs}
    artifact_provider = _get_artifact_to_providing_job_map(workflow)

    # Build adjacency: job_name -> set of predecessor job names
    job_deps = defaultdict(set)
    for job in workflow.jobs:
        # Hard deps: requires -> find the providing job
        for req in job.requires:
            if req in artifact_provider:
                job_deps[job.name].add(artifact_provider[req])
            elif req in jobs_by_name:
                # requires can also reference job names directly (for report artifacts)
                job_deps[job.name].add(req)
        # Ordering deps
        for dep_name in job.run_after:
            if dep_name in jobs_by_name:
                job_deps[job.name].add(dep_name)
        # Ensure every job appears in the dict
        job_deps.setdefault(job.name, set())

    # Topological sort by levels (Kahn's algorithm)
    in_degree = {name: len(deps) for name, deps in job_deps.items()}
    # Reverse: who depends on me
    dependents = defaultdict(set)
    for name, deps in job_deps.items():
        for dep in deps:
            dependents[dep].add(name)

    levels = []
    ready = deque(sorted(n for n, d in in_degree.items() if d == 0))
    visited = set()

    while ready:
        level = list(ready)
        levels.append(level)
        ready.clear()
        for name in level:
            visited.add(name)
            for succ in sorted(dependents[name]):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    ready.append(succ)

    # Check for cycles
    if len(visited) != len(jobs_by_name):
        missing = set(jobs_by_name) - visited
        print(f"WARNING: dependency cycle detected, unreachable jobs: {missing}")

    return levels, dict(job_deps)


def print_execution_plan(workflow, levels, job_deps):
    """Print a human-readable execution plan."""
    jobs_by_name = {job.name: job for job in workflow.jobs}
    total_jobs = sum(len(lv) for lv in levels)

    print(f"\n{'='*80}")
    print(f"Execution plan for workflow [{workflow.name}]")
    print(f"Total jobs: {total_jobs}, Execution levels: {len(levels)}")
    print(f"{'='*80}")

    for i, level in enumerate(levels):
        print(f"\n--- Level {i} ({len(level)} jobs, parallel) ---")
        for name in level:
            job = jobs_by_name[name]
            deps = job_deps.get(name, set())
            runs_on = ", ".join(job.runs_on) if job.runs_on else "default"
            dep_str = f" <- [{', '.join(sorted(deps))}]" if deps else ""
            provides_str = f" -> [{', '.join(job.provides)}]" if job.provides else ""
            print(f"  {name}")
            print(f"    runner: {runs_on}{dep_str}{provides_str}")

    print(f"\n{'='*80}\n")


def _check_output(workflow, state, error=None):
    """Assemble a Check API `output` dict (title, summary, text) from the
    live ``WorkflowState``. Called on every PATCH so the top-level check's
    Markdown body tracks the current per-job table."""
    if workflow is None:
        return {
            "title": "No workflow",
            "summary": "No workflow matched this event",
            "text": "",
        }
    title = workflow.name
    if error is not None:
        summary = f"Orchestrator failed: {error}"
    elif state is None:
        summary = f"Planning `{workflow.name}`"
    elif state.cancelled:
        summary = f"Cancelled — {state.md_status_summary()}"
    else:
        summary = state.md_status_summary()
    text = state.md_status() if state is not None else ""
    if error is not None:
        text += f"\n\n### Error\n\n```\n{error}\n```"
    # Check API caps output.text at ~64 KB.
    limit = 60_000
    if len(text) > limit:
        text = text[:limit] + "\n\n... (truncated)\n"
    return {"title": title, "summary": summary, "text": text}


def _patch_top_check(check, workflow, state, error=None):
    """PATCH the top-level workflow check with the current Markdown snapshot.

    Wrapped in try/except: a stuck GitHub API call must not kill the
    orchestration loop. The error is reported but otherwise swallowed.
    """
    if check is None:
        return
    try:
        check.update(output=_check_output(workflow, state, error=error))
    except Exception as e:
        print(f"  [warn] top-level check PATCH failed: {type(e).__name__}: {e}")


def orchestrate(event, check=None, gh_token=None, run_id=None):
    """Single orchestrator entry-point used by both the SQS runner and the CLI.

    Finds all workflows matching ``event`` and runs them sequentially. Each
    matched workflow gets its own GitHub check run (named after the workflow).

    The goal of this indirection is to keep ``run.py`` stable — all orchestration
    policy lives here and ships with each PR, no user_data redeploy needed.

    Returns the process exit code (0 on success, 1 if any orchestration crashed).
    """
    print(
        f"Trigger event: {event.get('type')}.{event.get('action', '')} "
        f"repo={event.get('repo', '')} sender={event.get('sender', '')}"
    )

    if gh_token is None:
        try:
            from ..gh_auth import GHAuth
            from ..utils import Shell
            GHAuth.auth_from_settings()
            gh_token = Shell.get_output("gh auth token", strict=True)
        except Exception as e:
            print(f"  [warn] could not mint GH token: {e}")

    workflows = find_workflows_for_event(event)
    if not workflows:
        print("No matching workflows, exiting")
        if check is not None:
            try:
                check.complete("neutral", output=_check_output(None, None))
            except Exception:
                print(f"Failed to complete check run: {check}", file=sys.stderr)
        return 0

    print(f"Matched {len(workflows)} workflow(s): {[wf.name for wf in workflows]}")

    overall_rc = 0
    for workflow in workflows:
        rc = _orchestrate_single(workflow, event, gh_token=gh_token)
        if rc != 0:
            overall_rc = rc
    return overall_rc


def _orchestrate_single(workflow, event, gh_token=None):
    """Run one workflow: open a check run, execute the DAG, close the check.

    Returns 0 on success, 1 on crash.
    """
    from .check_run import CheckRun

    repo = event.get("repo", "")
    head_sha = event.get("head_sha", "")

    check = None
    if gh_token:
        try:
            check = CheckRun.start(gh_token, repo, head_sha, workflow.name)
        except Exception as e:
            print(f"  [warn] failed to open check run for [{workflow.name}]: {e}")

    run_id = str(check.id) if check is not None else None

    print(f"Matched workflow [{workflow.name}] with {len(workflow.jobs)} jobs")

    # Capture the plan + live execution output so we can attach it to the
    # check run body. The "Trigger event" / "Matched workflow" lines stay
    # outside the redirect so they land in the systemd journal for live SSM
    # debugging.
    buf = io.StringIO()
    error = None
    state = None
    try:
        with redirect_stdout(buf):
            # Local import avoids a circular dependency (state.py imports
            # build_job_dag from this module).
            from .state import WorkflowState

            state = WorkflowState(
                workflow,
                event=event,
                gh_token=gh_token,
                repo=event.get("repo", ""),
                head_sha=event.get("head_sha", ""),
                run_id=run_id,
            )
            state.print_plan()
            # Initial snapshot once the DAG exists — drops the "Planning..."
            # placeholder the top-level check started with.
            _patch_top_check(check, workflow, state)

            cancel_handled = False
            while state.not_finished():
                # A cancel signal arrived mid-run: mark every PENDING job
                # that isn't `run_unless_cancelled` as CANCELLED, then keep
                # going so the unconditional post-run jobs (Finish Workflow)
                # still get dispatched and completed through the normal
                # queue path.
                if state.cancelled and not cancel_handled:
                    state.cancel_pending_jobs()
                    cancel_handled = True
                for job in state.get_ready():
                    job.kick()
                state.wait()
                # Refresh the top-level check on every iteration — each
                # wait() return is a batch of state transitions
                # (completions + skips + filters). See PROTOCOL.md.
                _patch_top_check(check, workflow, state)

            state.print_summary()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        buf.write(f"\n\nError: {error}\n")
    finally:
        # Always delete the per-run queue, even on cancel or exception —
        # nothing else will consume it once this run exits.
        if state is not None:
            state.cleanup()

    plan_text = buf.getvalue()
    sys.stdout.write(plan_text)
    sys.stdout.flush()

    if check is not None:
        if error is not None:
            conclusion = "failure"
        elif state is not None and state.cancelled:
            conclusion = "cancelled"
        else:
            conclusion = "neutral"
        try:
            check.complete(conclusion, output=_check_output(workflow, state, error=error))
        except Exception:
            print(f"Failed to complete check run: {check}", file=sys.stderr)

    return 0 if error is None else 1


def run(event_file):
    """CLI entry-point (``python -m praktika orchestrate <event.json>``)."""
    with open(event_file) as f:
        event = json.load(f)
    sys.exit(orchestrate(event))
