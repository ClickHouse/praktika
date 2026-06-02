from pathlib import Path

from praktika_bootstrap import venv_manager


class _CompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_praktika_source_fingerprint_changes_with_directory_content(tmp_path):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "module.py").write_text("value = 1\n", encoding="utf-8")

    first = venv_manager.praktika_source_fingerprint(str(source_dir))
    (source_dir / "module.py").write_text("value = 2\n", encoding="utf-8")
    second = venv_manager.praktika_source_fingerprint(str(source_dir))

    assert first != second


def test_ensure_praktika_venv_reuses_existing_matching_env(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "-C", str(source_dir)]:
            return _CompletedProcess(returncode=128, stderr="not a git repo")
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
    assert (first / venv_manager.MARKER_FILE).exists()

    venv_creates = [cmd for cmd in first_calls if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]]
    assert len(venv_creates) == 1
    assert calls == first_calls + [
        ["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"],
    ]


def test_ensure_praktika_venv_uses_wheelhouse_when_configured(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "-C", str(source_dir)]:
            return _CompletedProcess(returncode=128, stderr="not a git repo")
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_path = Path(cmd[3])
            (venv_path / "bin").mkdir(parents=True, exist_ok=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
            return _CompletedProcess()
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    venv_dir = venv_manager.ensure_praktika_venv(
        str(source_dir),
        cache_root=cache_root,
        python_executable="/usr/bin/python3.12",
        wheelhouse=wheelhouse,
    )

    marker = (venv_dir / venv_manager.MARKER_FILE).read_text(encoding="utf-8")
    assert str(wheelhouse.resolve()) in marker
    install_calls = [
        cmd for cmd in calls if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert len(install_calls) == 2
    for cmd in install_calls:
        assert "--no-index" in cmd
        assert str(wheelhouse.resolve()) in cmd
    assert any("--upgrade" in cmd and "wheel" in cmd for cmd in install_calls)
    assert any(str(source_dir.resolve()) in cmd for cmd in install_calls)


def test_ensure_praktika_runtime_returns_named_base_venv(tmp_path):
    base_root = tmp_path / "base-venvs"
    base_dir = base_root / "pytest"
    (base_dir / "bin").mkdir(parents=True)
    (base_dir / "bin" / "python").write_text("", encoding="utf-8")

    resolved = venv_manager.ensure_praktika_runtime(
        None,
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
        if cmd[:3] == ["git", "-C", str(source_dir)]:
            return _CompletedProcess(returncode=128, stderr="not a git repo")
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    resolved = venv_manager.ensure_praktika_runtime(
        str(source_dir),
        base_venv="pytest",
        base_venv_root=base_root,
        cache_root=cache_root,
        python_executable="/usr/bin/python3.12",
    )

    assert resolved != base_dir.resolve()
    marker = (resolved / venv_manager.MARKER_FILE).read_text(encoding="utf-8")
    assert str(base_dir.resolve()) in marker
    install_calls = [
        cmd for cmd in calls if len(cmd) >= 4 and cmd[1:4] == ["-m", "pip", "install"]
    ]
    assert len(install_calls) == 1
    assert str(source_dir.resolve()) in install_calls[0]
