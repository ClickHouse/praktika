"""
Check CI runtime.

Asserts the per-run metadata the controller resolves from the repo's real git
history is present and correct inside a job — regardless of whether the S3 repo
snapshot and the ephemeral PR merge are enabled (the tree a job restores in
snapshot mode is history-free, so the job cannot derive this itself).

Each check returns a praktika Result; add new ones to CHECKS. The job fails if
any check fails.
"""
from praktika.info import Info
from praktika.result import Result
from praktika.utils import Utils


def _check_base_git_history():
    """Info().base_git_history() exposes the base branch's recent commits (from
    the PR merge-base back), and the PR head sha is NOT among them (it lives on
    the PR branch, not the base branch)."""
    sw = Utils.Stopwatch()
    info = Info()
    history = info.base_git_history()
    sha = info.sha
    problems = []
    if len(history) <= 10:
        problems.append(
            f"expected > 10 base-branch commits, got {len(history)}"
        )
    if sha in history:
        problems.append(
            f"head sha {sha[:12]} must not appear in base_git_history"
        )
    return Result(
        name="base_git_history",
        status=Result.Status.FAIL if problems else Result.Status.OK,
        start_time=sw.start_time,
        duration=sw.duration,
        info=(
            "; ".join(problems)
            or f"{len(history)} base commits; head {sha[:12]} not present"
        ),
    )


CHECKS = [
    _check_base_git_history,
]


def main():
    children = [check() for check in CHECKS]
    overall = (
        Result.Status.OK
        if all(c.is_ok() for c in children)
        else Result.Status.FAIL
    )
    Result.get().set_results(results=children).set_status(overall).complete_job()


if __name__ == "__main__":
    main()
