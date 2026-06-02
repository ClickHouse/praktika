from __future__ import annotations

import argparse
import sys

from praktika_bootstrap import run_job, run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="praktika_bootstrap",
        description="Thin bootstrap launcher for Praktika workflow and job daemons.",
    )
    parser.add_argument(
        "mode",
        choices=("workflow_orchestrator", "job_runner"),
        help="Bootstrap mode to run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "workflow_orchestrator":
        run_workflow.main()
        return 0
    if args.mode == "job_runner":
        run_job.main()
        return 0
    raise AssertionError(f"Unhandled mode: {args.mode}")


if __name__ == "__main__":
    sys.exit(main())

