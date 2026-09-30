"""Controller self-upgrade (dev mode).

The ``praktika_controller_version`` key in the ``ci_config`` SSM parameter pins
the controller wheel a run uses. Unlike the baked AMI/boot version, it can be
rolled forward or back from SSM without rebaking an image. The orchestrator reads
it once from SSM and freezes it into run metadata; every controller (orchestrator
and job runners) converges to the pinned version by reinstalling **in place** into
the system Python and re-launching (the ``Restart=always`` systemd unit relaunches
into the new code). See ``praktika/docs/ci-config.md``.

The source is any pip install target: a version spec (``praktika-controller==0.1.9``),
a wheel URL (``https://…whl``), or an absolute host path.

This is a dev-mode mechanism, kept intentionally simple:

- **Persistence** — the last-installed source string is persisted, so a controller
  only reinstalls when the pin actually changes (not on every message).
- **Fail hard** — a bad/unreachable pin is NOT validated, recovered, or rolled
  back; pip's error propagates so the failure is loud rather than silently running
  the old controller.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Where the last-installed source is persisted. Overridable for tests.
# ``/var/lib/praktika`` is root-writable (the controller runs as root) and already
# used by the system-log streamer.
STATE_PATH = Path(
    os.environ.get(
        "PRAKTIKA_CONTROLLER_STATE_PATH",
        "/var/lib/praktika/controller_version.json",
    )
)


def _load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001 - missing/corrupt state -> start fresh
        pass
    return {"installed_source": ""}


def _save_state(state: dict, log=None) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        if log is not None:
            log.warning("Could not persist controller version state: %s", e)


def _pip_install(source: str, *, python: str, run) -> None:
    """Reinstall the controller from ``source`` into ``python``'s site.

    Mirrors the boot-time reinstall: a plain ``--force-reinstall``, retried with
    ``--break-system-packages`` when the interpreter is PEP-668 externally-managed
    (Ubuntu images). Raises ``CalledProcessError`` if the install fails — a bad pin
    fails hard rather than being swallowed."""
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


def maybe_self_update(
    desired_source: str,
    log,
    *,
    python: str | None = None,
    run=subprocess.run,
) -> bool:
    """Converge the controller to ``desired_source`` (dev mode: no validation, no
    rollback).

    Returns ``True`` when it reinstalled and the process should restart into the
    new code (the caller releases any in-flight message and exits so systemd
    relaunches); ``False`` when there is nothing to do (no pin, or already at the
    pinned source). A failed install raises — the pin fails hard."""
    desired_source = (desired_source or "").strip()
    if not desired_source:
        return False  # no pin: run whatever is baked/booted

    state = _load_state()
    if state.get("installed_source") == desired_source:
        # Already at this pin; don't reinstall unless it changes in SSM. Logged so a
        # matching pin is visibly honored rather than looking like a no-op.
        log.info("Controller already at pinned %s; no reinstall", desired_source)
        return False

    python = python or sys.executable or "python3.12"
    log.info("Self-updating controller -> %s", desired_source)
    _pip_install(desired_source, python=python, run=run)  # fail hard on a bad pin
    state["installed_source"] = desired_source
    _save_state(state, log)
    log.info("Controller pinned to %s installed; restarting", desired_source)
    return True
