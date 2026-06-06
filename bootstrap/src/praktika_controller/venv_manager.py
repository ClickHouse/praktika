from __future__ import annotations

import contextlib
import fcntl
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
    env_name = f"praktika-{py_tag}"
    venv_dir = cache_root / env_name
    lock_path = cache_root / f"{env_name}.lock"

    with _file_lock(lock_path):
        if _venv_has_praktika(venv_dir):
            if log is not None:
                log.info("Using Praktika from venv %s", venv_dir)
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
        if _venv_has_praktika(base_dir):
            if log is not None:
                log.info("Using Praktika from prebaked base venv %s", base_dir)
            return base_dir

        if not source:
            raise ValueError(
                "PRAKTIKA_BASE_VENV is set but the base venv does not contain "
                "praktika and no install source was provided"
            )

        return _rebuild_runtime_from_base_venv(
            source,
            base_dir=base_dir,
            base_name=base_venv,
            cache_root=cache_root,
            log=log,
        )

    if not source:
        raise ValueError("Either source or base_venv must be provided")

    return ensure_praktika_venv(
        source,
        cache_root=cache_root,
        python_executable=python_executable,
        log=log,
    )


def praktika_command(venv_dir: str | os.PathLike[str], *args: str) -> list[str]:
    return [str(Path(venv_dir) / "bin" / "python"), "-m", "praktika", *args]


def venv_env(
    venv_dir: str | os.PathLike[str], base_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = f"{Path(venv_dir) / 'bin'}:{env.get('PATH', '')}"
    return env


def _normalize_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
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


def _rebuild_runtime_from_base_venv(
    source: str,
    *,
    base_dir: Path,
    base_name: str,
    cache_root: str | os.PathLike[str] | None,
    log=None,
) -> Path:
    source = _normalize_source(source)
    cache_root = Path(cache_root or DEFAULT_VENV_ROOT)
    cache_root.mkdir(parents=True, exist_ok=True)

    py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
    base_tag = _slugify(base_name)
    env_name = f"praktika-{base_tag}-{py_tag}"
    venv_dir = cache_root / env_name
    lock_path = cache_root / f"{env_name}.lock"

    with _file_lock(lock_path):
        if _venv_has_praktika(venv_dir):
            if log is not None:
                log.info("Using Praktika from runtime venv %s", venv_dir)
            return venv_dir
        if log is not None:
            log.info(
                "Building Praktika runtime venv %s from %s on top of %s",
                venv_dir,
                source,
                base_dir,
            )
        _build_runtime_from_base_venv(
            venv_dir,
            source,
            base_dir,
        )
        return venv_dir


def _build_runtime_from_base_venv(
    venv_dir: Path,
    source: str,
    base_dir: Path,
) -> None:
    temp_parent = venv_dir.parent
    with tempfile.TemporaryDirectory(prefix=f"{venv_dir.name}.tmp.", dir=temp_parent) as temp_dir:
        temp_path = Path(temp_dir)
        shutil.copytree(base_dir, temp_path, symlinks=True, dirs_exist_ok=True)

        temp_python = temp_path / "bin" / "python"
        subprocess.run(_pip_install_cmd(temp_python, source), check=True)

        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        os.replace(temp_path, venv_dir)


def _slugify(value: str) -> str:
    allowed = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_"}:
            allowed.append(ch)
        else:
            allowed.append("-")
    return "".join(allowed).strip("-") or "base"
