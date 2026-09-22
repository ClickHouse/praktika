"""Tests for the controller-side ephemeral merge + repo snapshot (merge.py).

These exercise the logic that moved out of praktika.native_jobs into the baked
praktika-controller: the deterministic PR merge, the plain-head path, conflict
detection, settings reading, and the history-free snapshot publish. Real git +
zstd are used (they must be on PATH); S3 is faked.
"""

import os
import subprocess
import textwrap

import pytest

from praktika_controller import common, merge


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeS3:
    """Minimal boto3-S3 stand-in for _build_and_publish_snapshot / sticky pin."""

    def __init__(self):
        self.objects = {}  # (bucket, key) -> bytes

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey")
        return {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            err = RuntimeError("PreconditionFailed")
            err.response = {"Error": {"Code": "PreconditionFailed"}}
            raise err
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("NoSuchKey")

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[(Bucket, Key)])}


def _git(cwd, *args, env=None):
    e = dict(os.environ)
    # Deterministic base/head commits so snapshot_sha is stable across runs.
    e.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_AUTHOR_DATE": "2001-01-01T00:00:00 +0000",
            "GIT_COMMITTER_DATE": "2001-01-01T00:00:00 +0000",
        }
    )
    if env:
        e.update(env)
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=e,
    ).stdout.strip()


def _write(path, name, content):
    with open(os.path.join(path, name), "w") as f:
        f.write(content)


def _make_origin(tmp_path, *, base_second_file="added_by_base.txt", pr_file_content="pr\n", base_conflict=None):
    """Build an origin repo: main has B0 then B1 (adds a file, or edits the
    shared file when base_conflict is set); a PR head branches from B0 and adds
    pr.txt (or edits the shared file). Returns (origin_dir, head_sha, b1_sha)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _write(origin, "shared.txt", "line0\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "B0")
    b0 = _git(origin, "rev-parse", "HEAD")

    # PR head branches from B0.
    _git(origin, "checkout", "-q", "-b", "pr", b0)
    if base_conflict is not None:
        _write(origin, "shared.txt", "head-change\n")
    else:
        _write(origin, "pr.txt", pr_file_content)
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "H")
    head_sha = _git(origin, "rev-parse", "HEAD")

    # Advance main to B1.
    _git(origin, "checkout", "-q", "main")
    if base_conflict is not None:
        _write(origin, "shared.txt", base_conflict)
    else:
        _write(origin, base_second_file, "base\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "B1")
    b1 = _git(origin, "rev-parse", "HEAD")
    return origin, head_sha, b1


def _make_clone(tmp_path, origin, head_sha, name="clone"):
    """Mirror controller.common.clone_repo: init + fetch head sha + checkout."""
    clone = tmp_path / name
    clone.mkdir()
    _git(clone, "init", "-q")
    _git(clone, "remote", "add", "origin", str(origin))
    _git(clone, "fetch", "-q", "--depth=1", "origin", head_sha)
    _git(clone, "checkout", "-q", head_sha)
    return clone


PR_MERGE_SETTINGS = {
    "ENABLE_S3_REPO_SNAPSHOT": True,
    "ENABLE_PR_EPHEMERAL_MERGE_COMMIT": True,
    "STICKY_MERGE_BASE_HOURS": 0,
    "S3_ARTIFACT_BUCKET": "mybucket/prefix",
}


def test_prepare_merges_base_and_head(tmp_path):
    origin, head_sha, b1 = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, head_sha)
    s3 = _FakeS3()
    event = {"type": "pull_request", "pr_number": 7, "base_ref": "main"}

    base_sha, snapshot_sha, key = merge.prepare_repo_snapshot(
        str(clone), event, PR_MERGE_SETTINGS, s3, _Log()
    )

    assert base_sha == b1  # pinned to the live tip
    # HEAD is now the merge commit; the working tree carries BOTH sides.
    assert _git(clone, "rev-parse", "HEAD") == snapshot_sha
    assert snapshot_sha not in (head_sha, b1)
    assert os.path.exists(clone / "pr.txt")  # from head
    assert os.path.exists(clone / "added_by_base.txt")  # from base (was dropped before this fix)
    # The merge has two parents: base first (GitHub's refs/pull/N/merge order).
    parents = _git(clone, "rev-list", "--parents", "-n", "1", snapshot_sha).split()[1:]
    assert parents == [b1, head_sha]
    # Published to the PRs/ tier, content-addressed, and actually uploaded.
    assert key.startswith("mybucket/prefix/repo-snapshots/v1/PRs/")
    assert ("mybucket", key.split("/", 1)[1]) in s3.objects


def test_merge_is_deterministic(tmp_path):
    origin, head_sha, _ = _make_origin(tmp_path)
    s3 = _FakeS3()
    event = {"type": "pull_request", "pr_number": 7, "base_ref": "main"}

    clone1 = _make_clone(tmp_path, origin, head_sha, name="c1")
    _, sha1, _ = merge.prepare_repo_snapshot(str(clone1), event, PR_MERGE_SETTINGS, s3, _Log())
    clone2 = _make_clone(tmp_path, origin, head_sha, name="c2")
    _, sha2, _ = merge.prepare_repo_snapshot(str(clone2), event, PR_MERGE_SETTINGS, s3, _Log())

    # Fixed identity/dates => same base+head => byte-identical MERGE COMMIT. (The
    # archive content-hash key is not required to match: it is a content-addressed,
    # write-once integrity key, and tar/zstd/.git packing varies run-to-run.)
    assert sha1 == sha2


def test_merge_conflict_raises(tmp_path):
    origin, head_sha, _ = _make_origin(tmp_path, base_conflict="base-change\n")
    clone = _make_clone(tmp_path, origin, head_sha)
    s3 = _FakeS3()
    event = {"type": "pull_request", "pr_number": 7, "base_ref": "main"}

    with pytest.raises(merge.MergeConflict) as ei:
        merge.prepare_repo_snapshot(str(clone), event, PR_MERGE_SETTINGS, s3, _Log())
    assert "shared.txt" in ei.value.files


def test_push_plain_head_snapshot(tmp_path):
    # Reuse the origin but treat it as a push of the head (no merge).
    origin, head_sha, _ = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, head_sha)
    s3 = _FakeS3()
    event = {"type": "push", "head_ref": "main"}

    base_sha, snapshot_sha, key = merge.prepare_repo_snapshot(
        str(clone), event, PR_MERGE_SETTINGS, s3, _Log()
    )

    assert base_sha == ""
    assert snapshot_sha == head_sha  # no merge
    assert _git(clone, "rev-parse", "HEAD") == head_sha
    assert key.startswith("mybucket/prefix/repo-snapshots/v1/REFs/")  # trusted tier


def test_disabled_returns_none(tmp_path):
    origin, head_sha, _ = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, head_sha)
    settings = dict(PR_MERGE_SETTINGS, ENABLE_S3_REPO_SNAPSHOT=False)
    event = {"type": "pull_request", "pr_number": 7, "base_ref": "main"}
    assert merge.prepare_repo_snapshot(str(clone), event, settings, _FakeS3(), _Log()) is None


def test_pr_without_merge_flag_is_plain_head(tmp_path):
    origin, head_sha, _ = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, head_sha)
    settings = dict(PR_MERGE_SETTINGS, ENABLE_PR_EPHEMERAL_MERGE_COMMIT=False)
    event = {"type": "pull_request", "pr_number": 7, "base_ref": "main"}
    base_sha, snapshot_sha, key = merge.prepare_repo_snapshot(
        str(clone), event, settings, _FakeS3(), _Log()
    )
    assert base_sha == ""
    assert snapshot_sha == head_sha
    assert key.startswith("mybucket/prefix/repo-snapshots/v1/PRs/")  # still a PR event


def test_read_repo_settings(tmp_path):
    settings_dir = tmp_path / "ci" / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.py").write_text(
        textwrap.dedent(
            """
            ENABLE_S3_REPO_SNAPSHOT = True
            ENABLE_PR_EPHEMERAL_MERGE_COMMIT = True
            STICKY_MERGE_BASE_HOURS = 6
            S3_ARTIFACT_BUCKET = "bkt/pfx"
            """
        )
    )
    vals = merge.read_repo_settings(str(tmp_path), _Log())
    assert vals["ENABLE_S3_REPO_SNAPSHOT"] is True
    assert vals["ENABLE_PR_EPHEMERAL_MERGE_COMMIT"] is True
    assert vals["STICKY_MERGE_BASE_HOURS"] == 6
    assert vals["S3_ARTIFACT_BUCKET"] == "bkt/pfx"


def test_read_repo_settings_defaults_when_absent(tmp_path):
    # No ci/settings/settings.py -> baked defaults (feature off).
    vals = merge.read_repo_settings(str(tmp_path), _Log())
    assert vals["ENABLE_S3_REPO_SNAPSHOT"] is False
    assert vals["ENABLE_PR_EPHEMERAL_MERGE_COMMIT"] is False
    assert vals["S3_ARTIFACT_BUCKET"] == ""


def test_head_commit_info_reads_head_before_merge(tmp_path):
    # The controller captures the branch-head subject/author while HEAD is the
    # head, before the ephemeral merge rewrites it.
    _, head_sha, _ = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, tmp_path / "origin", head_sha)
    info = common.head_commit_info(str(clone))
    assert info["message"] == "H"  # the PR head commit subject, not a merge commit
    assert info["authors"] == ["t@t"]


def test_split_artifact_bucket():
    assert merge._split_artifact_bucket("bucket", "a/b") == ("bucket", "a/b")
    assert merge._split_artifact_bucket("bucket/prefix", "a/b") == ("bucket", "prefix/a/b")
    assert merge._split_artifact_bucket("s3://bucket/pfx/", "x") == ("bucket", "pfx/x")
