from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
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
MARKER_FILE = ".praktika-bootstrap.json"
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".tox",
    ".venv",
    "venv",
    "build",
    "dist",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


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
    fingerprint = praktika_source_fingerprint(source)
    env_name = f"praktika-{py_tag}-{fingerprint}"
    venv_dir = cache_root / env_name
    lock_path = cache_root / f"{env_name}.lock"

    with _file_lock(lock_path):
        if _venv_matches(venv_dir, source, fingerprint):
            if log is not None:
                log.info("Reusing Praktika venv %s for %s", venv_dir, source)
            return venv_dir

        if log is not None:
            log.info("Building Praktika venv %s for %s", venv_dir, source)
        _build_venv(venv_dir, source, fingerprint, python_path)
        return venv_dir


def praktika_command(venv_dir: str | os.PathLike[str], *args: str) -> list[str]:
    return [str(Path(venv_dir) / "bin" / "python"), "-m", "praktika", *args]


def venv_env(
    venv_dir: str | os.PathLike[str], base_env: dict[str, str] | None = None
) -> dict[str, str]:
    env = dict(base_env or os.environ)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = f"{Path(venv_dir) / 'bin'}:{env.get('PATH', '')}"
    return env


def praktika_source_fingerprint(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return hashlib.sha256(f"url:{source}".encode("utf-8")).hexdigest()[:16]

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Praktika source does not exist: {source}")

    git_fingerprint = _git_fingerprint(path)
    if git_fingerprint:
        return git_fingerprint[:16]
    if path.is_dir():
        return _hash_directory(path)
    return _hash_file(path)


def _normalize_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return source
    return str(Path(source).resolve())


def _venv_matches(venv_dir: Path, source: str, fingerprint: str) -> bool:
    marker_path = venv_dir / MARKER_FILE
    python_path = venv_dir / "bin" / "python"
    if not marker_path.exists() or not python_path.exists():
        return False
    try:
        marker = json.loads(marker_path.read_text())
    except Exception:
        return False
    return (
        marker.get("source") == source
        and marker.get("fingerprint") == fingerprint
    )


def _build_venv(venv_dir: Path, source: str, fingerprint: str, python_path: str) -> None:
    temp_parent = venv_dir.parent
    with tempfile.TemporaryDirectory(prefix=f"{venv_dir.name}.tmp.", dir=temp_parent) as temp_dir:
        temp_path = Path(temp_dir)
        subprocess.run([python_path, "-m", "venv", str(temp_path)], check=True)

        temp_python = temp_path / "bin" / "python"
        subprocess.run(
            [str(temp_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            check=True,
        )
        subprocess.run([str(temp_python), "-m", "pip", "install", source], check=True)

        marker = {
            "source": source,
            "fingerprint": fingerprint,
            "python": python_path,
        }
        (temp_path / MARKER_FILE).write_text(json.dumps(marker, indent=2))

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


def _git_fingerprint(path: Path) -> str | None:
    target = path if path.is_dir() else path.parent
    repo_root = _git_output(["git", "-C", str(target), "rev-parse", "--show-toplevel"])
    if not repo_root:
        return None

    repo_root_path = Path(repo_root)
    try:
        rel_path = path.resolve().relative_to(repo_root_path.resolve())
    except ValueError:
        return None

    if str(rel_path) == ".":
        return _git_output(["git", "-C", str(repo_root_path), "rev-parse", "HEAD"])
    return _git_output(
        ["git", "-C", str(repo_root_path), "rev-parse", f"HEAD:{rel_path.as_posix()}"]
    )


def _git_output(cmd: list[str]) -> str | None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _hash_directory(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(_iter_files(path)):
        rel_path = file_path.relative_to(path)
        digest.update(rel_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _iter_files(path: Path):
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in file_path.parts):
            continue
        if file_path.suffix in IGNORED_SUFFIXES:
            continue
        yield file_path


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

