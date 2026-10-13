"""Tests for the idle warm-clone adoption used by clone_repo.

An idle orchestrator pre-fetches the base branch into a warm `.git` (no checkout);
the next task's clone_repo adopts it and applies only the PR delta. Real git is
used; GitHub is not (warm_default_branch/clone_repo hardcode github.com, so we
build the warm repo the same way against a local origin and exercise the adopt
helper directly).
"""

import os
import subprocess

from praktika_controller import common


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _git(cwd, *args):
    e = dict(os.environ)
    e.update(
        {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00 +0000",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00 +0000",
        }
    )
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=e
    ).stdout.strip()


def _build_origin_and_warm(tmp_path):
    """origin: master has A (base.txt), then advances to B (adds head.txt). warm:
    built like warm_default_branch while master == A (depth-1 tip + treeless
    unshallow), so it is a stale, history-complete base; B is the 'PR head'."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "master")
    (origin / "base.txt").write_text("base\n")
    (origin / "stale.txt").write_text("v1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "A")
    a_sha = _git(origin, "rev-parse", "HEAD")

    work = tmp_path / "work"
    work.mkdir()
    warm = work / common.WARM_SUBDIR
    warm.mkdir()
    url = f"file://{origin}"
    _git(warm, "init", "-q")
    _git(warm, "remote", "add", "origin", url)
    _git(warm, "fetch", "--depth=1", "--no-tags", "origin", "master")
    _git(warm, "fetch", "--unshallow", "--filter=tree:0", "--no-tags", "origin", "master")
    (warm / common._WARM_READY).write_text("x/y\nmaster\n")

    # origin advances to B (PR head): new file + modified existing file.
    (origin / "head.txt").write_text("head\n")
    (origin / "stale.txt").write_text("v2\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "B")
    b_sha = _git(origin, "rev-parse", "HEAD")
    return origin, work, warm, a_sha, b_sha


def test_adopt_applies_pr_delta(tmp_path):
    origin, work, warm, a_sha, b_sha = _build_origin_and_warm(tmp_path)
    clone_dir = os.path.join(str(work), common.REPO_SUBDIR)
    url = f"file://{origin}"

    actual = common._adopt_warm_repo(clone_dir, str(warm), url, b_sha, _Log())

    assert actual == b_sha
    assert _git(clone_dir, "rev-parse", "HEAD") == b_sha
    # Worktree is EXACTLY the head tree: new file present, modified file updated.
    assert (tmp_path / "work" / common.REPO_SUBDIR / "head.txt").exists()
    assert open(os.path.join(clone_dir, "stale.txt")).read() == "v2\n"
    assert _git(clone_dir, "status", "--porcelain") == ""
    # Full history is present (warm unshallowed) -> merge needs no unshallow.
    assert _git(clone_dir, "rev-parse", "--is-shallow-repository") == "false"
    assert a_sha in _git(clone_dir, "rev-list", "HEAD").split()
    # The warm dir was consumed (moved into place).
    assert not os.path.exists(str(warm))


def test_adopt_noop_without_sentinel(tmp_path):
    _, work, warm, _, b_sha = _build_origin_and_warm(tmp_path)
    os.remove(os.path.join(str(warm), common._WARM_READY))  # not ready
    clone_dir = os.path.join(str(work), common.REPO_SUBDIR)
    assert common._adopt_warm_repo(clone_dir, str(warm), "file:///x", b_sha, _Log()) is None
    assert os.path.exists(str(warm))  # left intact for a still-running warm


def test_adopt_noop_when_warm_dir_absent(tmp_path):
    clone_dir = os.path.join(str(tmp_path), common.REPO_SUBDIR)
    missing = os.path.join(str(tmp_path), common.WARM_SUBDIR)
    assert common._adopt_warm_repo(clone_dir, missing, "file:///x", "deadbeef", _Log()) is None
