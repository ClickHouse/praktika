"""Ephemeral PR merge + repo snapshot, computed in the baked controller.

For a ``pull_request`` run with ``ENABLE_PR_EPHEMERAL_MERGE_COMMIT`` the whole run
must derive from ONE commit: the ephemeral merge of the PR head into the
target-branch tip (GitHub Actions style). This has to happen in the controller
because the controller runs *before* praktika is reinstalled from the checkout
and before any PR-influenced code executes — so the clone it hands to the runtime
reinstall, the orchestrator's DAG, and every job's tree all anchor to the same
merge. Doing it in praktika (which is itself reinstalled from the — possibly
PR-tampered — checkout) would run head's driver code against the merged tree.

Git merge is data-only (no PR code runs), so merging untrusted content here is
safe; the head is covered by the external-PR approval gate and the base is
trusted.

This module deliberately does NOT import praktika: the merge logic must live in
the baked package, not in the checkout-reinstalled runtime. It reuses only
``subprocess`` git and ``boto3``. It mirrors, and replaces,
``praktika.native_jobs._prepare_repo_snapshot`` / ``_resolve_sticky_base``; the
determinism there (fixed identity/dates) guarantees the controller's merge SHA
equals what the Config job would have produced from the same base + head.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from praktika_controller.common import load_ci_config

# Deterministic identity/dates so the merge sha depends only on the two parents
# and the resulting tree, not on wall-clock or runner identity. Must stay
# byte-identical to praktika.native_jobs so a later fail-closed verify (and any
# reproduction) yields the same commit.
_MERGE_IDENTITY = {
    "GIT_AUTHOR_NAME": "praktika",
    "GIT_AUTHOR_EMAIL": "praktika@localhost",
    "GIT_COMMITTER_NAME": "praktika",
    "GIT_COMMITTER_EMAIL": "praktika@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
}

# A git commit id (sha1 = 40 hex, sha256 = 64 hex). Used to validate the
# untrusted, cross-PR-writable sticky-base pin before it is interpolated into any
# git command.
_COMMIT_SHA_RE = re.compile(r"[0-9a-fA-F]{40,64}")

# Short-lived local tag used only to advertise the snapshot commit to the shallow
# local fetch that builds the archive.
_REPO_SNAPSHOT_TAG = "_praktika_repo_snapshot"

# Defaults mirror praktika/settings.py:_Settings for the fields the merge needs.
_SETTING_DEFAULTS = {
    "ENABLE_S3_REPO_SNAPSHOT": False,
    "ENABLE_PR_EPHEMERAL_MERGE_COMMIT": False,
    "STICKY_MERGE_BASE_HOURS": 0.0,
    "S3_ARTIFACT_BUCKET": "",
}


class MergeConflict(RuntimeError):
    """The PR head does not cleanly merge into the (pinned or live) base tip.

    ``files`` holds the conflicting paths (or diagnostic output) for the check.
    Raised so the caller can finalize the pre-clone bootstrap check as a failure
    before the orchestrator exists, rather than crashing the run.
    """

    def __init__(self, message: str, files: str = "") -> None:
        super().__init__(message)
        self.files = files


def _git(args, cwd, *, env=None, check=True):
    """Run ``git -C cwd <args>``; raise on nonzero unless ``check=False``."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result


def _git_out(args, cwd) -> str:
    return _git(args, cwd).stdout.strip()


def read_repo_settings(clone_dir, log, ci_config=None) -> dict:
    """Read the merge-relevant settings from the repo checkout.

    Mirrors ``common.resolve_praktika_base_venv``: import the project's
    ``ci/settings/settings.py`` (plus any ``*_overrides.py``, applied in sorted
    order, matching ``praktika.settings._get_settings``) and read the handful of
    fields the merge needs, falling back to the ``_Settings`` defaults for
    anything absent. Reading these values is data, not the merge logic, so it is
    safe here; a PR can at most disable its own merge (→ plain-head behavior),
    which is no worse than not having this feature.
    """
    values = dict(_SETTING_DEFAULTS)
    settings_dir = Path(clone_dir) / "ci" / "settings"

    def _apply(path: Path) -> None:
        try:
            spec = importlib.util.spec_from_file_location(path.stem, str(path))
            mod = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            for name in _SETTING_DEFAULTS:
                if hasattr(mod, name):
                    values[name] = getattr(mod, name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read repo settings from %s: %s", path, e)

    primary = settings_dir / "settings.py"
    if primary.is_file():
        _apply(primary)
    for override in sorted(settings_dir.glob("*_overrides.py")):
        _apply(override)

    # Per-project migration override (out-of-repo). ``force_merge_commit`` in the
    # project's CI config ({slug}-ci-config, see common.load_ci_config) forces the
    # ephemeral PR merge + repo snapshot ON even for old branches whose checkout
    # predates these settings (or has them off), so their CI runs the current
    # praktika code against the merge with the target tip instead of the stale
    # head. Applied last so it wins over whatever the checked-out repo says. This
    # gate only decides whether to merge; once merged, the run reinstalls praktika
    # from the merged tree (which carries the target branch's own True settings),
    # so the rest of the pipeline stays consistent.
    #
    # ``ci_config`` may be passed pre-resolved by the caller (the controller reads
    # it once per run and also freezes it into run metadata); fall back to reading
    # it here when called standalone.
    if ci_config is None:
        ci_config = load_ci_config(log=log)
    if bool(ci_config.get("force_merge_commit")):
        values["ENABLE_S3_REPO_SNAPSHOT"] = True
        values["ENABLE_PR_EPHEMERAL_MERGE_COMMIT"] = True
        log.info(
            "force_merge_commit set in controller config: forcing "
            "ENABLE_S3_REPO_SNAPSHOT and ENABLE_PR_EPHEMERAL_MERGE_COMMIT on"
        )
    return values


def _split_artifact_bucket(artifact_bucket: str, subkey: str) -> tuple[str, str]:
    """Split ``S3_ARTIFACT_BUCKET`` (``"bucket"`` or ``"bucket/prefix"``) into a
    ``(bucket, key)`` pair for boto3, appending ``subkey``. Mirrors the ``bucket,
    key = cleaned.split("/", 1)`` convention used by ``common.restore_repo_snapshot``.
    """
    cleaned = artifact_bucket.removeprefix("s3://").strip("/")
    parts = cleaned.split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    key = f"{prefix}/{subkey}" if prefix else subkey
    return bucket, key


def _resolve_sticky_base(
    s3, artifact_bucket, pr_number, base_branch, live_base_sha, sticky_hours, clone_dir, log
) -> str:
    """Sticky merge base (per PR). Port of ``native_jobs._resolve_sticky_base``.

    Reuse the previously pinned target-branch commit when the new run starts
    within ``sticky_hours`` of this PR's previous run; otherwise reset to the live
    tip. The pin (``{bucket}/pr/<pr>/merge-base-pin.json``) is untrusted,
    cross-PR-writable input, so a pinned sha is accepted only if it (1) matches a
    plain commit id — it is interpolated into git commands — and (2) is an
    ancestor of the live tip. Either failure falls back to the live tip.
    Best-effort throughout: any error uses the live tip.
    """
    bucket, pin_key = _split_artifact_bucket(
        artifact_bucket, f"pr/{pr_number}/merge-base-pin.json"
    )
    now = time.time()
    base_sha = live_base_sha
    reason = "reset to live tip (no prior pin)"
    try:
        pinned = None
        try:
            obj = s3.get_object(Bucket=bucket, Key=pin_key)
            pinned = json.loads(obj["Body"].read())
        except Exception:  # noqa: BLE001 - missing/unreadable pin -> live tip
            pinned = None
        if isinstance(pinned, dict):
            p_base = str(pinned.get("base_sha") or "")
            p_ts = float(pinned.get("pinned_ts") or 0)
            p_branch = str(pinned.get("base_branch") or "")
            age_h = (now - p_ts) / 3600
            if not p_base or p_branch != base_branch:
                reason = "reset to live tip (no usable pin)"
            elif not _COMMIT_SHA_RE.fullmatch(p_base):
                reason = "reset to live tip (pinned base_sha is not a commit id)"
            elif age_h > sticky_hours:
                reason = (
                    f"reset to live tip (previous run {age_h:.1f}h ago > {sticky_hours}h)"
                )
            else:
                # Reuse only if the pinned base is still an ancestor of the live
                # tip. Fetch it first — it may not be in the partial clone.
                _git(
                    ["fetch", "--no-tags", "--filter=tree:0", "origin", p_base],
                    clone_dir,
                    check=False,
                )
                if (
                    _git(
                        ["merge-base", "--is-ancestor", p_base, live_base_sha],
                        clone_dir,
                        check=False,
                    ).returncode
                    == 0
                ):
                    base_sha = p_base
                    reason = f"reused (pinned {age_h:.1f}h ago, window {sticky_hours}h)"
                else:
                    reason = "reset (pinned base not an ancestor of live tip)"
    except Exception as e:  # noqa: BLE001
        log.warning("sticky base lookup failed, using live tip: %s", e)
        base_sha = live_base_sha
    # Refresh the pin: keep the chosen base_sha, stamp the current run time so the
    # window is measured from this (the previous) run next time.
    try:
        s3.put_object(
            Bucket=bucket,
            Key=pin_key,
            Body=json.dumps(
                {"base_sha": base_sha, "pinned_ts": now, "base_branch": base_branch}
            ).encode(),
            ContentType="application/json",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("could not persist sticky base pin: %s", e)
    log.info("Sticky merge base: %s -> %s", reason, base_sha[:12])
    return base_sha


def _ensure_base_history(clone_dir, base_branch, log) -> None:
    """Bring the shallow head clone up to the history the merge needs: unshallow
    HEAD and fetch the base branch. Mirrors the ``repo unshallow`` step in
    ``native_jobs._config_workflow`` (best-effort, matching its ``||:``)."""
    if (
        _git_out(["rev-parse", "--is-shallow-repository"], clone_dir).strip() == "true"
    ):
        _git(
            [
                "fetch", "--unshallow", "--prune", "--no-recurse-submodules",
                "--filter=tree:0", "origin", "HEAD",
            ],
            clone_dir,
            check=False,
        )
    _git(
        [
            "fetch", "--prune", "--no-recurse-submodules", "--filter=tree:0",
            "origin", base_branch,
        ],
        clone_dir,
        check=False,
    )


def _merge_head_into_base(clone_dir, base_branch, base_sha, head_sha, log) -> str:
    """Deterministically merge ``head_sha`` into ``base_sha`` in place. Returns
    the merge commit sha. Raises ``MergeConflict`` if the merge does not apply
    cleanly. Port of the merge block in ``native_jobs._prepare_repo_snapshot``.
    """
    # base_sha is the first parent (matches GitHub's refs/pull/N/merge).
    _git(["checkout", "--quiet", "--force", base_sha], clone_dir)
    merged = _git(
        [
            "merge", "--no-ff", "--no-edit",
            "-m", f"Merge {head_sha} into {base_branch} ({base_sha})",
            head_sha,
        ],
        clone_dir,
        env=_MERGE_IDENTITY,
        check=False,
    )
    if merged.returncode != 0:
        conflicts = _git_out(
            ["diff", "--name-only", "--diff-filter=U"], clone_dir
        )
        _git(["merge", "--abort"], clone_dir, check=False)
        raise MergeConflict(
            f"PR head {head_sha[:12]} does not cleanly merge into "
            f"{base_branch} ({base_sha[:12]}). Conflicting files:",
            conflicts,
        )
    snapshot_sha = _git_out(["rev-parse", "HEAD"], clone_dir)
    log.info("Ephemeral merge commit created: %s", snapshot_sha)
    return snapshot_sha


def _build_and_publish_snapshot(clone_dir, snapshot_sha, is_pr, artifact_bucket, s3, log) -> str:
    """Pack a minimal, history-free snapshot (depth-1, keeps ``.git``) of
    ``snapshot_sha`` and upload it content-addressed to S3. Returns the
    ``bucket/key`` the run stores as ``repo_snapshot_key``. Port of the archive
    build/publish block in ``native_jobs._prepare_repo_snapshot``.
    """
    scratch = tempfile.mkdtemp(prefix="praktika-snapshot-")
    snap_dir = os.path.join(scratch, "repo_snapshot")
    archive_path = os.path.join(scratch, "repo_snapshot.tar.zst")
    try:
        subprocess.run(["git", "init", "-q", snap_dir], check=True)
        # Tag the commit first so it is advertised to the fetch (an unadvertised
        # sha would require uploadpack.allowAnySHA1InWant on the source).
        _git(["tag", "-f", _REPO_SNAPSHOT_TAG, snapshot_sha], clone_dir)
        try:
            subprocess.run(
                [
                    "git", "-C", snap_dir, "fetch", "--depth=1", "-q",
                    f"file://{os.path.abspath(clone_dir)}",
                    f"refs/tags/{_REPO_SNAPSHOT_TAG}",
                ],
                check=True,
            )
        finally:
            _git(["tag", "-d", _REPO_SNAPSHOT_TAG], clone_dir, check=False)
        subprocess.run(
            ["git", "-C", snap_dir, "checkout", "-q", "--detach", "FETCH_HEAD"],
            check=True,
        )
        snap_sha = _git_out(["rev-parse", "HEAD"], snap_dir)
        if snap_sha != snapshot_sha:
            raise RuntimeError(
                f"snapshot HEAD {snap_sha} != expected {snapshot_sha}"
            )

        subprocess.run(
            f"tar -C {shlex.quote(snap_dir)} -cf - . | zstd -c -T0 -q > "
            f"{shlex.quote(archive_path)}",
            shell=True,
            check=True,
            executable="/bin/bash",
        )

        content_hash = _sha256_file(archive_path)
        # Trust tier: PRs/ (pull_request, fork-reachable) vs REFs/ (push/trusted).
        # IAM scopes these so a pr-* pool can read but not write REFs/, and a
        # trusted pool can neither read nor write PRs/.
        tier = "PRs" if is_pr else "REFs"
        bucket, key = _split_artifact_bucket(
            artifact_bucket, f"repo-snapshots/v1/{tier}/{content_hash}.tar.zst"
        )
        repo_snapshot_key = f"{bucket}/{key}"

        if _s3_object_exists(s3, bucket, key):
            log.info("Repo snapshot already present: %s", repo_snapshot_key)
        else:
            try:
                with open(archive_path, "rb") as f:
                    s3.put_object(Bucket=bucket, Key=key, Body=f.read(), IfNoneMatch="*")
                log.info("Repo snapshot uploaded: %s", repo_snapshot_key)
            except Exception as e:  # noqa: BLE001
                code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
                if str(code) in ("PreconditionFailed", "ConditionalRequestConflict"):
                    # Lost a write-once race — the object exists, which is success.
                    log.info("Repo snapshot created concurrently: %s", repo_snapshot_key)
                elif _s3_object_exists(s3, bucket, key):
                    log.info("Repo snapshot created concurrently: %s", repo_snapshot_key)
                else:
                    raise RuntimeError(
                        f"Failed to upload repo snapshot; object absent from S3: "
                        f"{repo_snapshot_key}"
                    ) from e
        return repo_snapshot_key
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _s3_object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001
        return False


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def prepare_repo_snapshot(clone_dir, event, settings, s3, log):
    """Establish the run's single commit and publish its snapshot.

    Runs in the controller after cloning ``head_sha``. For a ``pull_request`` in
    merge mode it merges the PR head into the (pinned or live) base tip in place —
    so ``clone_dir`` HEAD becomes the merge commit and the subsequent
    ``runtime_source`` reinstall installs praktika from the merged tree — then
    packs and uploads a history-free snapshot. For push / non-merge it snapshots
    the plain head.

    Returns ``(base_sha, snapshot_sha, repo_snapshot_key)``, or ``None`` when
    ``ENABLE_S3_REPO_SNAPSHOT`` is off (nothing to do). Raises ``MergeConflict``
    on an unclean merge.
    """
    if not settings.get("ENABLE_S3_REPO_SNAPSHOT"):
        return None

    event_type = event.get("type", "")
    pr_number = event.get("pr_number")
    # Authoritative checked-out head (clone_repo already pinned it to head_sha).
    head_sha = _git_out(["rev-parse", "HEAD"], clone_dir)
    is_pr = bool(pr_number) and event_type == "pull_request"
    do_merge = is_pr and bool(settings.get("ENABLE_PR_EPHEMERAL_MERGE_COMMIT"))

    base_sha = ""
    # The commit every layer of the run derives from: the ephemeral merge for a PR
    # in merge mode, otherwise the head as-is.
    snapshot_sha = head_sha

    if do_merge:
        base_branch = event.get("base_ref", "")
        if not base_branch:
            raise RuntimeError(
                "Ephemeral merge requested but no base branch on the event"
            )
        _ensure_base_history(clone_dir, base_branch, log)
        live_base_sha = _git_out(["rev-parse", f"origin/{base_branch}"], clone_dir)
        if not live_base_sha:
            raise RuntimeError(f"Failed to resolve tip of base branch [{base_branch}]")

        # Sticky merge base: within a window after this PR's previous run, reuse
        # the same target-branch commit even if the branch advanced, so the digest
        # cache stays warm. Off unless STICKY_MERGE_BASE_HOURS > 0; always falls
        # back to the live tip.
        base_sha = live_base_sha
        sticky_hours = float(settings.get("STICKY_MERGE_BASE_HOURS") or 0)
        if sticky_hours > 0:
            base_sha = _resolve_sticky_base(
                s3,
                settings["S3_ARTIFACT_BUCKET"],
                pr_number,
                base_branch,
                live_base_sha,
                sticky_hours,
                clone_dir,
                log,
            )
        log.info(
            "Ephemeral merge: base [%s] %s (live tip %s) + head %s",
            base_branch, base_sha[:12], live_base_sha[:12], head_sha[:12],
        )

        # When the pinned base differs from the live tip (sticky), still verify the
        # PR merges cleanly with the CURRENT target HEAD so a green never hides a
        # real conflict with live main. Non-destructive (git merge-tree, >= 2.38).
        if base_sha != live_base_sha:
            r = _git(
                [
                    "merge-tree", "--write-tree", "--name-only",
                    live_base_sha, head_sha,
                ],
                clone_dir,
                check=False,
            )
            if r.returncode == 1:
                raise MergeConflict(
                    f"PR head {head_sha[:12]} conflicts with the current "
                    f"{base_branch} HEAD ({live_base_sha[:12]}) and needs a "
                    f"rebase/merge. (CI ran against the pinned base "
                    f"{base_sha[:12]}.)",
                    "\n".join(r.stdout.splitlines()[1:]),
                )
            if r.returncode != 0:
                # Fail closed: this is the only guard that the sticky (older)
                # pinned base does not hide a conflict with the current tip.
                raise MergeConflict(
                    f"Could not verify PR head {head_sha[:12]} merges into the "
                    f"current {base_branch} HEAD ({live_base_sha[:12]}): git "
                    f"merge-tree exited {r.returncode}.",
                    f"{r.stdout}\n{r.stderr}",
                )

        snapshot_sha = _merge_head_into_base(
            clone_dir, base_branch, base_sha, head_sha, log
        )
    else:
        log.info("Repo snapshot: head %s (no merge)", head_sha[:12])

    repo_snapshot_key = _build_and_publish_snapshot(
        clone_dir, snapshot_sha, is_pr, settings["S3_ARTIFACT_BUCKET"], s3, log
    )
    return base_sha, snapshot_sha, repo_snapshot_key
