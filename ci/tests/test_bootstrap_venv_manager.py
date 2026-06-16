import sys
from pathlib import Path

from praktika_controller import venv_manager


class _CompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_PY_TAG = f"py{sys.version_info.major}.{sys.version_info.minor}"


def test_ensure_praktika_venv_reuses_existing_installed_env(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        expected_python = str(cache_root / f"praktika-{_PY_TAG}" / "bin" / "python")
        if cmd == [expected_python, "-c", "import praktika"]:
            return _CompletedProcess(
                returncode=0
                if (cache_root / f"praktika-{_PY_TAG}" / "bin" / "python").exists()
                else 1
            )
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_path = Path(cmd[3])
            (venv_path / "bin").mkdir(parents=True, exist_ok=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
            return _CompletedProcess()
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    first = venv_manager.ensure_praktika_venv(
        str(source_dir),
        cache_root=cache_root,
        python_executable="/usr/bin/python3.12",
    )
    first_calls = list(calls)

    second = venv_manager.ensure_praktika_venv(
        str(source_dir),
        cache_root=cache_root,
        python_executable="/usr/bin/python3.12",
    )

    assert first == second
    assert first == (cache_root / f"praktika-{_PY_TAG}").resolve()

    venv_creates = [cmd for cmd in first_calls if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]]
    assert len(venv_creates) == 1
    assert calls == first_calls + [
        [str(first / "bin" / "python"), "-c", "import praktika"],
    ]

def test_ensure_praktika_runtime_uses_base_venv_when_praktika_is_installed(
    tmp_path, monkeypatch
):
    base_root = tmp_path / "base-venvs"
    base_dir = base_root / "pytest"
    (base_dir / "bin").mkdir(parents=True)
    (base_dir / "bin" / "python").write_text("", encoding="utf-8")

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        if cmd == [str(base_dir / "bin" / "python"), "-c", "import praktika"]:
            return _CompletedProcess(returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    resolved = venv_manager.ensure_praktika_runtime(
        "https://example.invalid/praktika.whl",
        base_venv="pytest",
        base_venv_root=base_root,
    )

    assert resolved == base_dir.resolve()


def test_ensure_praktika_runtime_builds_overlay_from_base_venv(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    base_root = tmp_path / "base-venvs"
    base_dir = base_root / "pytest"
    (base_dir / "bin").mkdir(parents=True)
    (base_dir / "bin" / "python").write_text("", encoding="utf-8")
    (base_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        if cmd == [str(base_dir / "bin" / "python"), "-c", "import praktika"]:
            return _CompletedProcess(returncode=1, stderr="ModuleNotFoundError")
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    resolved = venv_manager.ensure_praktika_runtime(
        str(source_dir),
        base_venv="pytest",
        base_venv_root=base_root,
        cache_root=cache_root,
    )

    assert resolved != base_dir.resolve()
    assert resolved == (cache_root / f"praktika-pytest-{_PY_TAG}").resolve()
    install_calls = [
        cmd for cmd in calls if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert len(install_calls) == 1
    assert str(source_dir.resolve()) in install_calls[0]


def test_ensure_praktika_runtime_requires_explicit_source_when_base_lacks_praktika(
    tmp_path, monkeypatch
):
    base_root = tmp_path / "base-venvs"
    base_dir = base_root / "pytest"
    (base_dir / "bin").mkdir(parents=True)
    (base_dir / "bin" / "python").write_text("", encoding="utf-8")
    (base_dir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        if cmd == [str(base_dir / "bin" / "python"), "-c", "import praktika"]:
            return _CompletedProcess(returncode=1, stderr="ModuleNotFoundError")
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    try:
        venv_manager.ensure_praktika_runtime(
            None,
            base_venv="pytest",
            base_venv_root=base_root,
            cache_root=cache_root,
        )
    except ValueError as ex:
        assert "no install source was provided" in str(ex)
    else:
        raise AssertionError("Expected ensure_praktika_runtime() to fail without source")


def test_ensure_praktika_runtime_reuses_existing_runtime_venv(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    base_root = tmp_path / "base-venvs"
    base_dir = base_root / "pytest"
    (base_dir / "bin").mkdir(parents=True)
    (base_dir / "bin" / "python").write_text("", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    runtime_python = cache_root / f"praktika-pytest-{_PY_TAG}" / "bin" / "python"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        if cmd == [str(base_dir / "bin" / "python"), "-c", "import praktika"]:
            return _CompletedProcess(returncode=1, stderr="ModuleNotFoundError")
        if cmd == [str(runtime_python), "-c", "import praktika"]:
            return _CompletedProcess(returncode=0 if runtime_python.exists() else 1)
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_path = Path(cmd[3])
            (venv_path / "bin").mkdir(parents=True, exist_ok=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
            return _CompletedProcess()
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    first = venv_manager.ensure_praktika_runtime(
        str(source_dir),
        base_venv="pytest",
        base_venv_root=base_root,
        cache_root=cache_root,
    )
    first_calls = list(calls)

    second = venv_manager.ensure_praktika_runtime(
        "https://example.invalid/another.whl",
        base_venv="pytest",
        base_venv_root=base_root,
        cache_root=cache_root,
    )

    assert first == second == (cache_root / f"praktika-pytest-{_PY_TAG}").resolve()
    install_calls = [
        cmd for cmd in first_calls if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert len(install_calls) == 1
    assert calls == first_calls + [
        [str(base_dir / "bin" / "python"), "-c", "import praktika"],
        [str(second / "bin" / "python"), "-c", "import praktika"],
    ]
