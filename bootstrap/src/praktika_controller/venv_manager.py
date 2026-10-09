from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_VENV_ROOT = os.environ.get(
    "PRAKTIKA_VENV_ROOT",
    "/opt/praktika/venvs",
)
DEFAULT_BASE_VENV_ROOT = os.environ.get(
    "PRAKTIKA_BASE_VENV_ROOT",
    "/opt/praktika/base-venvs",
)


def ensure_praktika_venv(
    source: str,
    *,
    cache_root: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    log=None,
) -> Path:
    source = _normalize_source(source)
    cache_root = Path(cache_root or DEFAULT_VENV_ROOT)
    cache_root.mkdir(parents=True, exist_ok=True)

    python_path = str(python_executable or sys.executable)
    py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
    # Key the cache by the source too, so a changed source (e.g. a new
    # praktika_version pin) resolves to a different venv and is (re)installed,
    # instead of silently reusing an existing praktika-* venv built from the old
    # source.
    env_name = f"praktika-{py_tag}-{_source_key(source)}"
    venv_dir = cache_root / env_name
    lock_path = cache_root / f"{env_name}.lock"

    with _file_lock(lock_path):
        if _venv_has_praktika(venv_dir):
            if is_passthrough(source):
                # Immutable source (URL / version spec): the string identity IS the
                # content identity, so an existing venv is safe to reuse.
                if log is not None:
                    log.info("Using Praktika from venv %s", venv_dir)
                return venv_dir
            # Mutable local-path source (e.g. "." / a checkout): the SAME path can
            # hold different content across runs (a different restored snapshot),
            # so the source string can't prove the cached venv is current.
            # Reinstall from source on every task — matching the base-venv path in
            # ensure_praktika_runtime — so a run never executes stale Praktika.
            # --no-deps keeps the venv's baked deps (add a new runtime dependency
            # => rebuild the venv).
            if log is not None:
                log.info("Reinstalling Praktika from %s into %s", source, venv_dir)
            subprocess.run(
                _pip_install_cmd(
                    venv_dir / "bin" / "python",
                    "--force-reinstall",
                    "--no-deps",
                    source,
                ),
                check=True,
            )
            return venv_dir

        if log is not None:
            log.info("Building Praktika venv %s for %s", venv_dir, source)
        _build_venv(venv_dir, source, python_path)
        return venv_dir


def ensure_praktika_runtime(
    source: str | None = None,
    *,
    base_venv: str = "",
    cache_root: str | os.PathLike[str] | None = None,
    base_venv_root: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    log=None,
) -> Path:
    source = _normalize_source(source) if source else ""

    if base_venv:
        base_dir = _resolve_base_venv(base_venv, base_venv_root)

        # An explicit source is a deliberate override (a pool's
        # `praktika_runtime_source` tag / a ci_config version pin): (re)install it
        # straight into the prebaked base venv on EVERY task with --force-reinstall,
        # so the pool always runs the current checkout even when the base venv
        # already ships praktika. --no-deps keeps the baked deps (add a new runtime
        # dependency => rebake the base venv).
        #
        # No overlay copy: a runtime_source pool reinstalls every task and never
        # runs a baked-mode task on the same instance, so the base venv needs no
        # pristine copy to protect; and each instance has its own AMI copy, so the
        # mutation can't leak across instances. This drops the ~20s one-time
        # copytree. (The controller processes one task at a time, but take the lock
        # anyway so a stray concurrent call can't corrupt the install.)
        if source:
            with _file_lock(base_dir.parent / f"{base_dir.name}.lock"):
                if log is not None:
                    log.info(
                        "Installing Praktika from %s into base venv %s", source, base_dir
                    )
                subprocess.run(
                    _pip_install_cmd(
                        base_dir / "bin" / "python",
                        "--force-reinstall",
                        "--no-deps",
                        source,
                    ),
                    check=True,
                )
            return base_dir

        if _venv_has_praktika(base_dir):
            if log is not None:
                log.info("Using Praktika from prebaked base venv %s", base_dir)
            return base_dir

        raise ValueError(
            "PRAKTIKA_BASE_VENV is set but the base venv does not contain "
            "praktika and no install source was provided"
        )

    if not source:
        raise ValueError("Either source or base_venv must be provided")

    return ensure_praktika_venv(
        source,
        cache_root=cache_root,
        python_executable=python_executable,
        log=log,
    )


def resolve_praktika_runtime(
    source: str | None,
    *,
    base_venv: str = "",
    cache_root: str | os.PathLike[str] | None = None,
    base_venv_root: str | os.PathLike[str] | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    log=None,
) -> tuple[Path, str | None]:
    """Resolve how to run Praktika for one task: returns ``(venv_dir, pythonpath)``.

    ``pythonpath`` is non-None only for the local-checkout case below; the caller
    puts it on ``PYTHONPATH`` so ``python -P -m praktika`` imports Praktika straight
    from the checkout (``-P`` drops cwd from ``sys.path`` but still honors
    ``PYTHONPATH``).

    - **Local checkout source** (a filesystem path, e.g. a pool's ``./ci`` tag) with
      a base venv: PYTHONPATH mode. Praktika is pure Python and its runtime deps are
      baked into the base venv, so there is nothing to compile — run it directly from
      the checkout via ``PYTHONPATH=<source>`` instead of pip-installing it into the
      venv on every task. This removes the per-task wheel build + install and writes
      NOTHING into the checkout (no ``build/``, no ``*.egg-info`` — so no dirty-repo
      noise), while giving a STRONGER freshness guarantee than ``--force-reinstall``:
      the exact checked-out tree is imported, by construction. No base-venv mutation,
      so no file lock is needed. Returns ``(base_venv_dir, <source>)``.

    - **URL / version-pin source, or no source** (or no base venv): delegate to
      ``ensure_praktika_runtime`` (install the pinned wheel into / reuse the base
      venv, use the baked base venv, or build a standalone venv) and return
      ``(venv_dir, None)`` — these are immutable or prebaked, so they stay installed,
      not path-imported.
    """
    normalized = _normalize_source(source) if source else ""
    if normalized and base_venv and not is_passthrough(normalized):
        base_dir = _resolve_base_venv(base_venv, base_venv_root)
        if log is not None:
            log.info("Running Praktika from checkout %s via PYTHONPATH", normalized)
        return base_dir, normalized

    venv_dir = ensure_praktika_runtime(
        source,
        base_venv=base_venv,
        cache_root=cache_root,
        base_venv_root=base_venv_root,
        python_executable=python_executable,
        log=log,
    )
    return venv_dir, None


def praktika_command(venv_dir: str | os.PathLike[str], *args: str) -> list[str]:
    # Use safe path mode so the controller runs Praktika from the selected venv,
    # without Python prepending the current working directory to sys.path.
    return [str(Path(venv_dir) / "bin" / "python"), "-P", "-m", "praktika", *args]


def venv_env(
    venv_dir: str | os.PathLike[str], base_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = f"{Path(venv_dir) / 'bin'}:{env.get('PATH', '')}"
    return env


# Requirement-specifier operators (PEP 508). A source containing one of these is
# a version spec (e.g. ``praktika==0.1.9``), not a filesystem path.
_REQUIREMENT_OPERATORS = ("==", ">=", "<=", "~=", "!=", "<", ">")


def is_passthrough(source: str) -> bool:
    """True when ``source`` is a pip install target that must be passed to pip
    verbatim rather than resolved as a local filesystem path: a URL (any
    ``scheme://…`` — e.g. an ``https://…whl`` wheel) or a requirement spec (a
    package name with a version operator, e.g. ``praktika==0.1.9``).

    Bare package names without an operator are intentionally NOT passthrough:
    they are ambiguous with a relative path and version pins always carry an
    exact spec, so such inputs stay on the local-path branch."""
    if "://" in source:
        return True
    return any(op in source for op in _REQUIREMENT_OPERATORS)


def _source_key(source: str) -> str:
    """Short stable slug of a (normalized) install source, for use in a venv
    directory name. A hash keeps arbitrary URLs / long paths bounded and
    filesystem-safe while staying 1:1 with the source string."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]


def _normalize_source(source: str) -> str:
    # A URL or requirement spec is a pip target in its own right (version pins:
    # a wheel URL or ``name==version``); hand it to pip verbatim. Everything else
    # is a filesystem path (a checkout, typically "."); resolve to an absolute
    # path for a stable pip target.
    if is_passthrough(source):
        return source
    return str(Path(source).resolve())


def _build_venv(
    venv_dir: Path,
    source: str,
    python_path: str,
) -> None:
    temp_parent = venv_dir.parent
    with tempfile.TemporaryDirectory(prefix=f"{venv_dir.name}.tmp.", dir=temp_parent) as temp_dir:
        temp_path = Path(temp_dir)
        subprocess.run([python_path, "-m", "venv", str(temp_path)], check=True)

        temp_python = temp_path / "bin" / "python"
        subprocess.run(
            _pip_install_cmd(
                temp_python,
                "--upgrade",
                "pip",
                "setuptools",
                "wheel",
            ),
            check=True,
        )
        subprocess.run(_pip_install_cmd(temp_python, source), check=True)

        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        os.replace(temp_path, venv_dir)


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _pip_install_cmd(
    python_path: Path,
    *packages: str,
) -> list[str]:
    cmd = [str(python_path), "-m", "pip", "install"]
    cmd.extend(packages)
    return cmd


def _resolve_base_venv(
    base_venv: str,
    base_venv_root: str | os.PathLike[str] | None,
) -> Path:
    root = Path(base_venv_root or DEFAULT_BASE_VENV_ROOT)
    path = Path(base_venv)
    if not path.is_absolute():
        path = root / base_venv
    path = path.resolve()
    python_path = path / "bin" / "python"
    if not python_path.exists():
        raise FileNotFoundError(f"Praktika base venv does not exist: {path}")
    return path


def _venv_has_praktika(venv_dir: Path) -> bool:
    python_path = venv_dir / "bin" / "python"
    if not python_path.exists():
        return False
    result = subprocess.run(
        [str(python_path), "-c", "import praktika"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

