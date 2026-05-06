import json
import re
import sys
from collections import defaultdict, deque
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
        branch = event.get("head_ref", "")
    else:
        branch = ""

    matched = []
    for wf in _get_workflows():
        if wf.engine == Workflow.Engine.GH_ACTIONS:
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


def _patch_top_check(check, workflow, state, error=None, details_url=None):
    """PATCH the top-level workflow check with the current Markdown snapshot.

    Wrapped in try/except: a stuck GitHub API call must not kill the
    orchestration loop. The error is reported but otherwise swallowed.
    """
    if check is None:
        return
    try:
        check.update(output=_check_output(workflow, state, error=error), details_url=details_url)
    except Exception as e:
        print(f"  [warn] top-level check PATCH failed: {type(e).__name__}: {e}")


def orchestrate(event, check=None, gh_token=None, run_id=None, ci=True):
    """Single orchestrator entry-point used by both the SQS runner and the CLI.

    Finds all workflows matching ``event`` and runs them sequentially. Each
    matched workflow gets its own GitHub check run (named after the workflow).

    ``ci=False``: local/dry-run mode — no GH auth, no check runs, no buffering.

    Returns the process exit code (0 on success, 1 if any orchestration crashed).
    """
    print(
        f"Trigger event: {event.get('type')}.{event.get('action', '')} "
        f"repo={event.get('repo', '')} sender={event.get('sender', '')}"
    )

    if ci and gh_token is None:
        # `gh` CLI is already authenticated by the agent (workflow_agent
        # runs `gh auth login --with-token` before invoking this
        # subprocess); just extract the token. Falls back to
        # GHAuth.auth_from_settings for the local-dev case where a user
        # runs `praktika orchestrate workflow --ci` directly.
        from ..utils import Shell
        try:
            gh_token = Shell.get_output("gh auth token", strict=True)
        except Exception:
            try:
                from ..gh_auth import GHAuth
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
        rc = _orchestrate_single(workflow, event, gh_token=gh_token, local_mode=not ci)
        if rc != 0:
            overall_rc = rc
    return overall_rc


def _orchestrate_single(workflow, event, gh_token=None, local_mode=False):
    """Run one workflow: open a check run, execute the DAG, close the check.

    Returns 0 on success, 1 on crash.
    """
    from .check_run import CheckRun

    repo = event.get("repo", "")
    head_sha = event.get("head_sha", "")

    report_url = None
    if workflow.enable_report:
        from ..info import Info
        report_url = Info.get_specific_report_url_static(
            pr_number=event.get("pr_number") or 0,
            branch=event.get("head_ref", ""),
            sha=head_sha,
            job_name="",
            workflow_name=workflow.name,
        )

    check = None
    if gh_token:
        try:
            check = CheckRun.start(gh_token, repo, head_sha, workflow.name, details_url=report_url)
        except Exception as e:
            print(f"  [warn] failed to open check run for [{workflow.name}]: {e}")

    run_id = str(check.id) if check is not None else None

    print(f"Matched workflow [{workflow.name}] with {len(workflow.jobs)} jobs")

    from .state import WorkflowState

    # The top-level check body is rendered from `state.md_status()` (a live
    # per-job table) by `_check_output`, not from this stream — so we print
    # straight to stdout. Lets the user (or `journalctl -fu workflow-agent`)
    # see progress in real time, especially important when the loop hangs.
    error = None
    state = None
    try:
        state = WorkflowState(
            workflow,
            event=event,
            gh_token=gh_token,
            repo=event.get("repo", ""),
            head_sha=event.get("head_sha", ""),
            run_id=run_id,
            local_mode=local_mode,
        )
        state.print_plan()
        _patch_top_check(check, workflow, state, details_url=report_url)

        cancel_handled = False
        while state.not_finished():
            if state.cancelled and not cancel_handled:
                state.cancel_unfinished_jobs()
                cancel_handled = True
            for job in state.get_ready():
                job.kick()
            state.wait()
            _patch_top_check(check, workflow, state, details_url=report_url)

        state.print_summary()
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"\n\nError: {error}")
    finally:
        if state is not None:
            state.cleanup()

    if check is not None:
        if error is not None:
            conclusion = "failure"
        elif state is not None and state.cancelled:
            conclusion = "cancelled"
        else:
            conclusion = "neutral"
        try:
            check.complete(conclusion, output=_check_output(workflow, state, error=error), details_url=report_url)
        except Exception:
            print(f"Failed to complete check run: {check}", file=sys.stderr)

    return 0 if error is None else 1


def _git_output(*args):
    import subprocess
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _build_event(args):
    """Build a trigger event dict from CLI args + current git state."""
    import re
    import subprocess

    repo = args.repo
    if not repo:
        remote = _git_output("remote", "get-url", "origin")
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
        repo = m.group(1) if m else ""

    head_sha = args.head_sha or _git_output("rev-parse", "HEAD")
    head_ref = args.head_ref or _git_output("branch", "--show-current")
    sender = args.sender or _git_output("config", "user.name")

    pr_number = args.pr_number
    if pr_number is None:
        r = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number"],
            capture_output=True, text=True,
        )
        try:
            pr_number = int(r.stdout.strip()) if r.returncode == 0 else 0
        except ValueError:
            pr_number = 0

    event = {
        "type": args.event_type,
        "action": "synchronize",
        "repo": repo,
        "head_sha": head_sha,
        "head_ref": head_ref,
        "base_ref": args.base_ref,
        "pr_number": pr_number,
        "sender": sender,
        "title": "",
        "draft": False,
        "labels": [],
    }
    print(
        f"Event: {event['type']}.{event['action']} "
        f"PR#{event['pr_number']} [{event['head_ref']} -> {event['base_ref']}] "
        f"sha={event['head_sha'][:12] if event['head_sha'] else ''}"
    )
    return event


def run(event_file=None, args=None):
    """CLI entry-point (``praktika orchestrate workflow [event.json]``)."""
    if event_file:
        with open(event_file) as f:
            event = json.load(f)
    else:
        event = _build_event(args)
    ci = getattr(args, "ci", False)
    sys.exit(orchestrate(event, ci=ci))
