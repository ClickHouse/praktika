"""Controller self-upgrade.

The ``praktika_controller_version`` key in the ``ci_config`` SSM parameter pins
the controller wheel a run uses. Unlike the baked AMI/boot version, it can be
rolled forward or back from SSM without rebaking an image. The orchestrator reads
it once from SSM and freezes it into run metadata; every controller (orchestrator
and job runners) converges to the pinned version by reinstalling **in place** into
the system Python and re-launching (the ``Restart=always`` systemd unit relaunches
into the new code). See ``praktika/docs/ci-config.md``.

The source is one of the three pip forms: a version spec (``praktika-controller==0.1.9``),
a wheel URL (``https://…whl``), or a filesystem path.

Guards (see the design doc's "Risks / guards"):

- **Persistence** — the last-installed source string is persisted, so a controller
  only reinstalls when the pin actually changes (not on every message).
- **Crash-loop protection** — a per-source attempt counter caps reinstalls of a
  bad pin, and a post-install import check gates the restart. On a failed install
  the controller stays on its current (in-memory) code and best-effort restores the
  last-known-good source, rather than restarting into broken code.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Where the last-installed source / version and per-source failure counts live.
# Overridable for tests. ``/var/lib/praktika`` is root-writable (the controller
# runs as root) and already used by the system-log streamer.
STATE_PATH = Path(
    os.environ.get(
        "PRAKTIKA_CONTROLLER_STATE_PATH",
        "/var/lib/praktika/controller_version.json",
    )
)

CONTROLLER_PACKAGE = "praktika-controller"

# Give up reinstalling a source after this many failed attempts, so a bad pin
# cannot make every fresh instance reinstall -> fail -> restart -> reinstall
# forever.
MAX_ATTEMPTS = 3


def current_controller_version() -> str:
    """The running ``praktika-controller`` version, or "" if undeterminable."""
    try:
        return importlib.metadata.version(CONTROLLER_PACKAGE)
    except Exception:  # noqa: BLE001 - version lookup is best-effort
        return ""


_REQUIREMENT_OPERATORS = ("==", ">=", "<=", "~=", "!=", "<", ">")


def _is_valid_controller_source(source: str) -> bool:
    """Whether ``source`` is installable at controller self-update time.

    Self-update runs before the workflow repo is cloned, so there is no checkout to
    resolve a relative path against. Only forms pip can install standalone are
    allowed: a URL (``scheme://…``), a requirement spec (``name==…``), or an
    **absolute** host path. A relative path (``.``, ``./bootstrap``) is rejected."""
    if "://" in source:
        return True
    if any(op in source for op in _REQUIREMENT_OPERATORS):
        return True
    return os.path.isabs(source)


def _normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name (``_``/``.``/``-`` runs -> ``-``,
    lowercased), so ``praktika_controller`` and ``praktika-controller`` compare
    equal."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _pin_identity(source: str):
    """The (distribution name, exact version) a pin *targets*, as far as it can be
    known before installing. Returns ``(name|None, version|None)``:

    - wheel URL/path (``…/praktika_controller-0.1.8-py3-none-any.whl``): parsed from
      the standardized wheel filename ``{dist}-{version}-…``.
    - requirement spec (``praktika-controller==0.1.8``): the name, and the version
      only for an exact ``==`` pin.
    - anything else (a non-wheel URL, a bare directory path): ``(None, None)`` —
      identity can't be determined up front.

    Used to (a) reject a pin that targets the wrong project before installing it and
    (b) confirm the intended version actually landed afterwards, so a typo like
    ``praktika==0.1.8`` or a URL to an unrelated wheel can't be silently adopted."""
    seg = source.rstrip("/").split("/")[-1].split("?")[0]
    if seg.lower().endswith(".whl"):
        parts = seg[:-4].split("-")
        if len(parts) >= 2:
            return _normalize_dist(parts[0]), parts[1]
        return None, None
    if "://" in source:
        return None, None  # non-wheel URL — can't tell without fetching
    m = re.match(
        r"^\s*([A-Za-z0-9._-]+)\s*(==|~=|!=|>=|<=|<|>)\s*([^,;\s]+)", source
    )
    if m:
        version = m.group(3) if m.group(2) == "==" else None
        return _normalize_dist(m.group(1)), version
    return None, None


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("failed", {})
            return data
    except Exception:  # noqa: BLE001 - missing/corrupt state -> start fresh
        pass
    return {"installed_source": "", "installed_version": "", "failed": {}}


def _save_state(state: dict, log=None) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        if log is not None:
            log.warning("Could not persist controller version state: %s", e)


def _pip_install(source: str, *, python: str, run) -> None:
    """Reinstall the controller wheel from ``source`` into ``python``'s site.

    Mirrors the boot-time reinstall: a plain ``--force-reinstall``, retried with
    ``--break-system-packages`` when the interpreter is PEP-668 externally-managed
    (Ubuntu images). Raises ``CalledProcessError`` if the install fails."""
    base = [python, "-m", "pip", "install", "--force-reinstall", source]
    proc = run(base, capture_output=True, text=True)
    if proc.returncode == 0:
        return
    stderr = proc.stderr or ""
    if "externally-managed-environment" in stderr or "--break-system-packages" in stderr:
        proc = run(base + ["--break-system-packages"], capture_output=True, text=True)
        if proc.returncode == 0:
            return
        stderr = proc.stderr or ""
    raise subprocess.CalledProcessError(proc.returncode, base, stderr=stderr)


def _verify_import(python: str, *, run) -> str:
    """Return the installed controller version if the fresh install imports, else
    "". A failing import means the new wheel is broken and we must NOT restart
    into it."""
    proc = run(
        [
            python,
            "-c",
            "import praktika_controller, importlib.metadata;"
            "print(importlib.metadata.version('praktika-controller'))",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def maybe_self_update(
    desired_source: str,
    log,
    *,
    python: str | None = None,
    run=subprocess.run,
) -> bool:
    """Converge the controller to ``desired_source``.

    Returns ``True`` when a new version was installed and verified and the process
    should restart into it (the caller releases any in-flight message and exits so
    systemd relaunches). Returns ``False`` when nothing to do (no pin / already at
    the pin) or the install did not succeed (stay on current code)."""
    desired_source = (desired_source or "").strip()
    if not desired_source:
        return False  # no pin: run whatever is baked/booted

    if not _is_valid_controller_source(desired_source):
        # Controller self-update runs before any workflow checkout exists, so a
        # relative repo path can't be resolved (pip would resolve it against the
        # systemd process cwd and fail). Reject it cleanly — no install attempt,
        # no attempt-cap churn — rather than looping until the cap.
        log.error(
            "Ignoring praktika_controller_version %r: a relative path cannot be "
            "resolved at controller self-update time (it runs before any checkout). "
            "Use a version spec, a wheel URL, or an absolute host path.",
            desired_source,
        )
        return False

    # Reject a pin that targets the wrong project BEFORE installing it. Otherwise
    # a typo like "praktika==0.1.8" (or a URL to an unrelated wheel) installs fine,
    # the still-old praktika-controller imports, and we'd persist the bad source as
    # converged — running the old controller forever. Only enforce when the target
    # is knowable (spec name / wheel filename); a bare path/non-wheel URL is checked
    # by version after install where possible.
    exp_name, exp_version = _pin_identity(desired_source)
    if exp_name and exp_name != _normalize_dist(CONTROLLER_PACKAGE):
        log.error(
            "Ignoring praktika_controller_version %r: it targets %r, not %s.",
            desired_source,
            exp_name,
            CONTROLLER_PACKAGE,
        )
        return False

    python = python or sys.executable or "python3.12"
    state = _load_state()

    if state.get("installed_source") == desired_source:
        # Already converged; do not reinstall unless the pin changes in SSM. Log it
        # (at INFO) so the pin is visibly honored — otherwise a matching pin looks
        # like nothing happened at all.
        log.info(
            "Controller already converged to pinned %s; no reinstall", desired_source
        )
        return False

    # Fresh instance (no recorded install) already running the pinned exact
    # version: record it as satisfied instead of a pointless reinstall.
    if exp_version and exp_version == current_controller_version():
        log.info(
            "Controller already at pinned version %s (%s); no reinstall",
            exp_version,
            desired_source,
        )
        state["installed_source"] = desired_source
        state["installed_version"] = exp_version
        state.get("failed", {}).pop(desired_source, None)
        _save_state(state, log)
        return False

    failed = state.setdefault("failed", {})
    if failed.get(desired_source, 0) >= MAX_ATTEMPTS:
        log.error(
            "Not reinstalling controller %r: failed %s time(s) already; staying on "
            "current version %s",
            desired_source,
            failed[desired_source],
            current_controller_version(),
        )
        return False

    previous_source = state.get("installed_source") or ""
    if not previous_source:
        # Fresh instance: no recorded source to roll back to. Best-effort — target
        # the currently-running version by spec so a failed in-place update can be
        # undone rather than leaving the worker unable to import the controller on
        # its next systemd restart.
        #
        # TODO (dev-only path today): this can't recover a version published only to
        # a private index / S3 (a bare `name==X` spec won't resolve there). True
        # atomicity — stage/verify the install in a separate env and only then swap,
        # never mutating the live interpreter until verified — is deferred as
        # production hardening. See ci-config.md.
        running = current_controller_version()
        previous_source = f"{CONTROLLER_PACKAGE}=={running}" if running else ""

    # Record the attempt before installing, so a crash mid-install still counts
    # toward the cap.
    failed[desired_source] = failed.get(desired_source, 0) + 1
    _save_state(state, log)

    log.info(
        "Self-updating controller %s -> %s (attempt %s/%s)",
        current_controller_version() or "?",
        desired_source,
        failed[desired_source],
        MAX_ATTEMPTS,
    )
    try:
        _pip_install(desired_source, python=python, run=run)
    except Exception as e:  # noqa: BLE001 - install failure -> stay on current code
        log.error("Controller self-update install failed: %s", e)
        _restore_previous(previous_source, python=python, run=run, log=log)
        return False

    version = _verify_import(python, run=run)
    if not version:
        log.error(
            "Controller self-update to %r installed but failed to import; not "
            "restarting into it",
            desired_source,
        )
        _restore_previous(previous_source, python=python, run=run, log=log)
        return False

    # Confirm the intended version actually landed. _verify_import only proves that
    # *some* praktika-controller imports; for an exact pin (== spec or a versioned
    # wheel) require the installed version to match, so an install that resolved to
    # something else (or silently left the old one) isn't adopted as converged.
    if exp_version and version != exp_version:
        log.error(
            "Controller self-update to %r resolved to version %s, expected %s; "
            "not adopting",
            desired_source,
            version,
            exp_version,
        )
        _restore_previous(previous_source, python=python, run=run, log=log)
        return False

    state["installed_source"] = desired_source
    state["installed_version"] = version
    failed.pop(desired_source, None)
    _save_state(state, log)
    log.info("Controller self-update to %s (%s) ready; restarting", desired_source, version)
    return True


def _restore_previous(previous_source: str, *, python: str, run, log) -> None:
    """Best-effort reinstall of the last-known-good source after a failed update,
    so a later unrelated systemd restart doesn't load half-installed/broken code.
    No-op when there was no prior recorded install."""
    if not previous_source:
        return
    try:
        _pip_install(previous_source, python=python, run=run)
        log.info("Restored previous controller source %s after failed update", previous_source)
    except Exception as e:  # noqa: BLE001 - restore is best-effort
        log.warning("Could not restore previous controller source %s: %s", previous_source, e)
