"""
Gate: require a version bump when a package's source changes in a PR.

For each tracked package (``praktika`` and ``praktika-controller``) we look at
the PR's changed files. If any file under the package's watched paths changed,
the package's ``pyproject.toml`` version must be strictly greater than the base
branch's version.

The CI checkout is a history-free ephemeral-merge snapshot (see
``praktika/docs/repo-git-management.md``), so the base branch is not available
locally. We read the head version from the working tree and fetch the base
version from the GitHub API.
"""
import base64
import time
from pathlib import Path

from praktika.gh import GH
from praktika.info import Info
from praktika.result import Result
from praktika.utils import Shell, Utils
from praktika.version import _version_from_pyproject, version_key

# name -> (pyproject path, watched source paths). A trailing "/" is a directory
# prefix; anything else is matched exactly. A change to any watched path
# requires the pyproject version to be bumped.
PACKAGES = {
    "praktika": {
        "pyproject": "pyproject.toml",
        "watch": ["praktika/", "pyproject.toml"],
    },
    "praktika-controller": {
        "pyproject": "bootstrap/pyproject.toml",
        "watch": ["bootstrap/src/", "bootstrap/pyproject.toml"],
    },
}


def _matches(path, watch):
    for w in watch:
        if w.endswith("/"):
            if path.startswith(w):
                return True
        elif path == w:
            return True
    return False


def _base_version(repo, base_branch, pyproject_path, tmp_name):
    """Version string of pyproject_path on base_branch, or "" if it does not
    exist there (a brand-new package). Raises on a persistent API failure so a
    missed bump is never silently let through."""
    cmd = (
        f"gh api repos/{repo}/contents/{pyproject_path}?ref={base_branch} "
        f"--jq .content"
    )
    attempts = 5
    err = ""
    for attempt in range(attempts):
        code, out, err = Shell.get_res_stdout_stderr(cmd)
        if code == 0:
            content = base64.b64decode(out).decode("utf-8")
            tmp = Path(f"./ci/tmp/{tmp_name}")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content, encoding="utf-8")
            return _version_from_pyproject(tmp)
        if "404" in err or "Not Found" in err:
            # File is absent on the base branch -> package is new in this PR.
            return ""
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    raise RuntimeError(
        f"Failed to fetch base pyproject [{pyproject_path}] from "
        f"[{base_branch}] after {attempts} attempts; stderr: {err}"
    )


def _check_package(name, cfg, changed, repo, base_branch, sw):
    touched = sorted(p for p in changed if _matches(p, cfg["watch"]))
    if not touched:
        return Result(
            name=name,
            status=Result.Status.OK,
            start_time=sw.start_time,
            duration=sw.duration,
            info="no source changes; version bump not required",
        )

    head = _version_from_pyproject(Path(cfg["pyproject"]))
    base = _base_version(
        repo, base_branch, cfg["pyproject"], f"base_{name}_pyproject.toml"
    )

    bumped = not base or version_key(head) > version_key(base)
    status = Result.Status.OK if bumped else Result.Status.FAIL
    if bumped:
        info = f"{name}: version bumped {base or '(new)'} -> {head}"
    else:
        info = (
            f"{name}: source changed but version not bumped "
            f"(base={base}, head={head}). Bump `version` in {cfg['pyproject']}. "
            f"Changed: {', '.join(touched)}"
        )
    return Result(
        name=name,
        status=status,
        start_time=sw.start_time,
        duration=sw.duration,
        info=info,
    )


def main():
    sw = Utils.Stopwatch()
    info = Info()

    if info.pr_number <= 0:
        Result.get().set_results(
            results=[
                Result(
                    name="version bump check",
                    status=Result.Status.SKIPPED,
                    info="not a pull_request run",
                )
            ]
        ).set_success().complete_job()
        return

    changed = info.get_changed_files()
    if changed is None:
        changed = GH.get_changed_files(strict=True)

    repo = info.repo_name
    base_branch = info.base_branch

    children = [
        _check_package(name, cfg, changed, repo, base_branch, sw)
        for name, cfg in PACKAGES.items()
    ]

    overall = (
        Result.Status.OK
        if all(c.is_ok() for c in children)
        else Result.Status.FAIL
    )
    Result.get().set_results(results=children).set_status(overall).complete_job()


if __name__ == "__main__":
    main()
