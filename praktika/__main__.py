import argparse
import datetime
import os
import sys
import textwrap

from .html_prepare import Html
from .settings import Settings
from .utils import Utils
from .validator import Validator
from .yaml_generator import YamlGenerator


_WRAP_WIDTH = 160
_TIMESTAMP_INDENT = len(
    datetime.datetime(2000, 1, 1).strftime("[%Y-%m-%d %H:%M:%S] ")
)


class _TimestampedStream:
    """Prepends a [YYYY-MM-DD HH:MM:SS] timestamp to each output line."""

    def __init__(self, stream):
        self._stream = stream
        self._at_line_start = True

    def write(self, data):
        if not data:
            return
        parts = data.split("\n")
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            if self._at_line_start and part:
                ts = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
                self._stream.write(ts + part)
            elif part:
                self._stream.write(part)
            if not is_last:
                self._stream.write("\n")
                self._at_line_start = True
            else:
                self._at_line_start = not part

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _TeeStream:
    """Writes to a terminal stream (with line wrapping) and a log file (without wrapping)."""

    def __init__(self, terminal, log, subsequent_indent=0):
        self._terminal = terminal
        self._log = log
        self._subsequent_indent = " " * subsequent_indent
        self._at_line_start = True

    def write(self, data):
        if not data:
            return
        self._log.write(data)
        parts = data.split("\n")
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            if self._at_line_start and part:
                self._terminal.write(
                    textwrap.fill(
                        part,
                        width=_WRAP_WIDTH,
                        subsequent_indent=self._subsequent_indent,
                        break_long_words=False,
                        break_on_hyphens=False,
                        expand_tabs=False,
                        replace_whitespace=False,
                        drop_whitespace=False,
                    )
                )
            elif part:
                self._terminal.write(part)
            if not is_last:
                self._terminal.write("\n")
                self._at_line_start = True
            else:
                self._at_line_start = not part

    def flush(self):
        self._terminal.flush()
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._terminal, name)


def create_parser():
    parser = argparse.ArgumentParser(
        prog="praktika",
        description=(
            "Praktika CLI: run CI jobs locally or in CI, generate YAML workflows"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    run_parser = subparsers.add_parser("run", help="Run a CI job")
    run_parser.add_argument(
        "job",
        help="Name of the job to run",
        type=str,
        nargs="?",
        default=None,
    )
    run_parser.add_argument(
        "--workflow",
        help=(
            "Workflow name to disambiguate when the job name is not unique in the config"
        ),
        type=str,
        default="",
    )
    run_parser.add_argument(
        "--no-docker",
        help=(
            "Run directly on the host even if the job is configured to use Docker (useful for local tests)"
        ),
        action="store_true",
    )
    run_parser.add_argument(
        "--docker",
        help=(
            "Override Docker image to run the job in (e.g. repo/image:tag). Only used when the job runs in Docker"
        ),
        type=str,
        default="",
    )
    run_parser.add_argument(
        "--param",
        help=(
            "Opaque string passed to the job script as --param (job script defines semantics). Useful for local tests"
        ),
        type=str,
        default=None,
    )
    run_parser.add_argument(
        "--test",
        help=(
            "One or more values passed to the job script as --test (space-separated) (job script defines semantics). Useful for selecting tests"
        ),
        nargs="+",
        type=str,
        default=[],
    )
    run_parser.add_argument(
        "--path",
        help=(
            "PATH parameter forwarded to the job as --path and mounted into Docker when applicable (job script defines semantics). Useful for local tests"
        ),
        type=str,
        default="",
    )
    run_parser.add_argument(
        "--path_1",
        help=(
            "Additional PATH parameter forwarded to the job as --path and mounted into Docker when applicable (job script defines semantics). Useful for local tests"
        ),
        type=str,
        default="",
    )
    run_parser.add_argument(
        "--count",
        help=(
            "Integer parameter forwarded to the job script (commonly used as number of reruns) (job script defines semantics). Useful for local tests"
        ),
        type=int,
        default=None,
    )
    run_parser.add_argument(
        "--debug",
        help=(
            "Enable debug mode for the job script (passed as --debug) (job script defines semantics). Useful for local tests"
        ),
        action="store_true",
        default="",
    )
    run_parser.add_argument(
        "--workers",
        help=(
            "Integer parameter forwarded to the job script (commonly used as number of parallel workers) (job script defines semantics). Useful for local tests"
        ),
        type=int,
        default=None,
    )
    run_parser.add_argument(
        "--pr",
        help=(
            "PR number to fetch required artifacts from its CI run (for local runs). Optional"
        ),
        type=int,
        default=None,
    )
    run_parser.add_argument(
        "--sha",
        help=(
            "Commit SHA whose CI artifacts should be used for required inputs (for local runs). Defaults to HEAD when not set"
        ),
        type=str,
        default=None,
    )
    run_parser.add_argument(
        "--branch",
        help=(
            "Branch name whose CI artifacts should be used for required inputs (for local runs). Defaults to the main branch when not set"
        ),
        type=str,
        default=None,
    )
    run_parser.add_argument(
        "--ci",
        help=(
            "Run in CI flag. When not set, a dummy local environment is generated (for local tests)"
        ),
        action="store_true",
        default="",
    )
    run_parser.add_argument(
        "--timestamp",
        help="Prefix each output line with a [YYYY-MM-DD HH:MM:SS] timestamp",
        action="store_true",
        default=False,
    )

    _yaml_parser = subparsers.add_parser("yaml", help="Generate YAML workflows")

    orch_parser = subparsers.add_parser(
        "orchestrate", help="Run a workflow or a single job"
    )
    orch_sub = orch_parser.add_subparsers(dest="orch_command")

    wf_parser = orch_sub.add_parser(
        "workflow", help="Orchestrate all matching workflows for a trigger event"
    )
    wf_parser.add_argument(
        "event_file", nargs="?", default=None,
        help="Path to trigger event JSON (auto-generated from git if omitted)",
    )
    wf_parser.add_argument("--event-type", default="pull_request",
        choices=["pull_request", "push"])
    wf_parser.add_argument("--repo", default=None)
    wf_parser.add_argument("--head-sha", default=None)
    wf_parser.add_argument("--head-ref", default=None)
    wf_parser.add_argument("--base-ref", default="main")
    wf_parser.add_argument("--pr-number", default=None, type=int)
    wf_parser.add_argument("--sender", default=None)
    wf_parser.add_argument("--ci", action="store_true", default=False,
        help="CI mode: authenticate to GitHub and post check runs")

    job_parser = orch_sub.add_parser(
        "job", help="Run a single job from a task JSON"
    )
    job_parser.add_argument("task_file", help="Path to task JSON file")
    job_parser.add_argument("--ci", action="store_true", default=False,
        help="CI mode: authenticate to GitHub and post check run updates")

    _infra_parser = subparsers.add_parser(
        "infrastructure", help="Manage cloud infrastructure and HTML reports"
    )
    _infra_parser.add_argument(
        "--deploy",
        help="Deploy cloud infrastructure or upload HTML report",
        action="store_true",
        default=False,
    )
    _infra_parser.add_argument(
        "--shutdown",
        help="Terminate EC2 instances and/or release Dedicated Hosts",
        action="store_true",
        default=False,
    )
    _infra_parser.add_argument(
        "--all",
        help="Deploy all configured components (used with --deploy)",
        action="store_true",
        default=False,
    )
    _infra_parser.add_argument(
        "--only",
        help=(
            "Process only specified components (e.g. html ImageBuilder LaunchTemplate AutoScalingGroup Lambda DedicatedHost EC2Instance). "
            "With --deploy: deploys only these components or uploads html report. "
            "With --shutdown: releases DedicatedHost or terminates EC2Instance."
        ),
        nargs="+",
        type=str,
        default=None,
    )
    _infra_parser.add_argument(
        "--restart-instances",
        help="Trigger an instance refresh on all ASGs, replacing all EC2 instances with the current launch template version",
        action="store_true",
        default=False,
    )
    _infra_parser.add_argument(
        "--test",
        help="Test mode for HTML upload (creates _test.html variant)",
        action="store_true",
        default=False,
    )
    return parser


def main():
    sys.path.append(".")
    parser = create_parser()
    args = parser.parse_args()

    if args.command == "yaml":
        Validator().validate()
        YamlGenerator().generate()
    elif args.command == "infrastructure":
        if not args.deploy and not args.shutdown and not args.restart_instances:
            Utils.raise_with_error(
                "infrastructure command requires --deploy, --shutdown, or --restart-instances"
            )

        if args.deploy:
            from .mangle import _get_infra_config

            _get_infra_config().deploy(
                all=args.all,
                only=args.only,
                is_test=args.test,
            )

        if args.shutdown:
            from .mangle import _get_infra_config

            _get_infra_config().shutdown(
                force=True,
                only=args.only,
            )

        if args.restart_instances:
            from .mangle import _get_infra_config

            _get_infra_config().restart_instances()
    elif args.command == "orchestrate":
        if args.orch_command == "workflow":
            from .orchestrator import run as orchestrate_run
            orchestrate_run(args.event_file, args)
        elif args.orch_command == "job":
            import json as _json
            from .orchestrator.job_runner import run_job
            from .utils import Shell
            with open(args.task_file) as f:
                task = _json.load(f)
            # `gh` CLI is already authenticated by the agent (see
            # job_agent.handle_task); pull the token out of `gh auth token`
            # for `_patch_check_run`, which uses requests directly.
            gh_token = None
            if args.ci:
                gh_token = Shell.get_output("gh auth token") or None
            sys.exit(run_job(task, gh_token=gh_token, local=not args.ci))
        else:
            orch_parser.print_help()
            sys.exit(1)
    elif args.command == "run":
        from .mangle import _get_workflows
        from .runner import Runner

        workflows = _get_workflows(
            name=args.workflow or None, default=not bool(args.workflow)
        ) # it actually returns only default workflow when there is no --workflow
        if args.job is None:
            for workflow in workflows:
                print(
                    f"Workflow [{workflow.name}] has jobs:\n"
                    "  \"" + f'"\n  "'.join([job.name for job in workflow.jobs]) + '"'
                    )
            Utils.exit_with_error("Job name is required to run a job.")

        job_workflow_pairs = []
        for workflow in workflows:
            jobs = workflow.find_jobs(args.job, lazy=True)
            if jobs:
                for job in jobs:
                    job_workflow_pairs.append((job, workflow))
        if not job_workflow_pairs:
            Utils.exit_with_error(
                f"Failed to find job [{args.job}] workflow [{args.workflow}]"
            )
        elif len(job_workflow_pairs) > 1:
            for job, wf in job_workflow_pairs:
                print(f"Job: [{job.name}], Workflow [{wf.name}]")
            Utils.exit_with_error(
                f"More than one job [{args.job}]: {[(wf.name, job.name) for job, wf in job_workflow_pairs]}"
            )
        else:
            job, workflow = job_workflow_pairs[0][0], job_workflow_pairs[0][1]
            print(f"Going to run job [{job.name}], workflow [{workflow.name}]")
            original_stdout = sys.stdout
            original_stderr = sys.stderr
            log_file = None
            try:
                log_dir = os.path.dirname(Settings.RUN_LOG)
                if log_dir:
                    os.makedirs(log_dir, exist_ok=True)
                log_file = open(Settings.RUN_LOG, "w", buffering=1)
                tee = _TeeStream(
                    original_stdout,
                    log_file,
                    subsequent_indent=_TIMESTAMP_INDENT if args.timestamp else 0,
                )
                sys.stdout = _TimestampedStream(tee) if args.timestamp else tee
                sys.stderr = sys.stdout
                Runner().run(
                    workflow=workflow,
                    job=job,
                    docker=args.docker,
                    local_job_run=not args.ci,
                    no_docker=args.no_docker,
                    param=args.param,
                    test=" ".join(args.test),
                    pr=args.pr,
                    branch=args.branch,
                    sha=args.sha,
                    count=args.count,
                    debug=args.debug,
                    path=args.path,
                    path_1=args.path_1,
                    workers=args.workers,
                )
            finally:
                sys.stdout = original_stdout
                sys.stderr = original_stderr
                if log_file is not None:
                    log_file.close()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
