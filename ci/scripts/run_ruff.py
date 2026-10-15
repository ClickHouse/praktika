#!/usr/bin/env python3
"""Style Check job: run ruff and emit a praktika Result.

Wraps ``ruff check .`` so the job publishes a structured Result instead of a bare
exit code: a single ``ruff`` sub-result whose ``info`` carries all of ruff's
findings. That surfaces the findings in the report (and in the AI Assist digest)
rather than hiding them behind "Job exited with code [1]".
"""
import subprocess

from praktika.result import Result
from praktika.utils import Utils


def main():
    sw = Utils.Stopwatch()
    proc = subprocess.run(
        ["ruff", "check", "."], capture_output=True, text=True
    )
    findings = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0

    ruff = Result.create_from(
        name="ruff",
        status=Result.Status.OK if ok else Result.Status.FAIL,
        info=findings or ("All checks passed" if ok else f"ruff exited {proc.returncode}"),
    )
    # No explicit status: the parent inherits FAIL from the ruff sub-result.
    Result.create_from(name="Style Check", results=[ruff], stopwatch=sw).complete_job()


if __name__ == "__main__":
    main()
