import sys
from pathlib import Path

from praktika_controller import venv_manager


class _CompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_PY_TAG = f"py{sys.version_info.major}.{sys.version_info.minor}"


def test_praktika_command_uses_safe_python_path_mode(tmp_path):
    venv_dir = tmp_path / "runtime"

    assert venv_manager.praktika_command(venv_dir, "orchestrate", "job") == [
        str(venv_dir / "bin" / "python"),
        "-P",
        "-m",
        "praktika",
        "orchestrate",
        "job",
    ]


def test_ensure_praktika_venv_reuses_existing_installed_env(tmp_path, monkeypatch):
    source_dir = tmp_path / "praktika-src"
    source_dir.mkdir()
    (source_dir / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")

    cache_root = tmp_path / "venvs"
    # The venv name is keyed by the (normalized) source, so a changed source
    # resolves to a different venv rather than silently reusing the old one.
    env_name = f"praktika-{_PY_TAG}-{venv_manager._source_key(str(source_dir.resolve()))}"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        expected_python = str(cache_root / env_name / "bin" / "python")
        if cmd == [expected_python, "-c", "import praktika"]:
            return _CompletedProcess(
                returncode=0
                if (cache_root / env_name / "bin" / "python").exists()
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
    assert first == (cache_root / env_name).resolve()

    venv_creates = [cmd for cmd in first_calls if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]]
    assert len(venv_creates) == 1
    assert calls == first_calls + [
        [str(first / "bin" / "python"), "-c", "import praktika"],
    ]

def test_ensure_praktika_runtime_uses_base_venv_when_no_source(
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
        None,
        base_venv="pytest",
        base_venv_root=base_root,
    )

    assert resolved == base_dir.resolve()


def test_ensure_praktika_runtime_source_overrides_base_venv(tmp_path, monkeypatch):
    # An explicit source is a runtime override: even when the base venv already
    # ships praktika, the source is installed on top of a copy of it.
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
        # Base venv HAS praktika — the old fallback semantics would short-circuit
        # to it here; the override must ignore that and build the overlay.
        if cmd == [str(base_dir / "bin" / "python"), "-c", "import praktika"]:
            return _CompletedProcess(returncode=0)
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


def test_ensure_praktika_runtime_reinstalls_source_every_task(tmp_path, monkeypatch):
    # A warm runner processes tasks from many checkouts. The overlay is created
    # once, but praktika is reinstalled from the source on EVERY task (with
    # --force-reinstall so a checkout change takes effect even at the same
    # version, and --no-deps so the baked deps are reused).
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
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    kw = dict(base_venv="pytest", base_venv_root=base_root, cache_root=cache_root)
    first = venv_manager.ensure_praktika_runtime(str(source_dir), **kw)
    second = venv_manager.ensure_praktika_runtime(str(source_dir), **kw)

    assert first == second == (cache_root / f"praktika-pytest-{_PY_TAG}").resolve()
    install_calls = [
        c for c in calls if len(c) >= 4 and c[1:4] == ["-m", "pip", "install"]
    ]
    assert len(install_calls) == 2  # reinstalled on every task
    for cmd in install_calls:
        assert "--force-reinstall" in cmd
        assert "--no-deps" in cmd
        assert str(source_dir.resolve()) in cmd


def test_is_passthrough_url_and_spec_but_not_path():
    assert venv_manager.is_passthrough("https://x/praktika-0.1.9-py3-none-any.whl")
    assert venv_manager.is_passthrough("praktika==0.1.9")
    assert venv_manager.is_passthrough("praktika>=0.1")
    # Bare name and paths are NOT passthrough (ambiguous with a relative path).
    assert not venv_manager.is_passthrough("praktika")
    assert not venv_manager.is_passthrough(".")
    assert not venv_manager.is_passthrough("/opt/praktika/src")


def test_normalize_source_passes_through_url_and_spec_resolves_path(tmp_path):
    url = "https://x/praktika-0.1.9-py3-none-any.whl"
    spec = "praktika==0.1.9"
    assert venv_manager._normalize_source(url) == url
    assert venv_manager._normalize_source(spec) == spec
    # A local path is still resolved to an absolute path.
    assert venv_manager._normalize_source(str(tmp_path)) == str(tmp_path.resolve())


def test_source_key_differs_by_source():
    a = venv_manager._source_key("praktika==0.1.9")
    b = venv_manager._source_key("praktika==0.2.0")
    assert a != b
    # Stable for the same source.
    assert a == venv_manager._source_key("praktika==0.1.9")


def test_ensure_praktika_venv_changed_source_uses_new_venv(tmp_path, monkeypatch):
    cache_root = tmp_path / "venvs"

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        # Report "no praktika yet" so each distinct source triggers a build.
        if len(cmd) >= 2 and cmd[-2:] == ["-c", "import praktika"]:
            return _CompletedProcess(returncode=1)
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_path = Path(cmd[3])
            (venv_path / "bin").mkdir(parents=True, exist_ok=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    first = venv_manager.ensure_praktika_venv("praktika==0.1.9", cache_root=cache_root)
    second = venv_manager.ensure_praktika_venv("praktika==0.2.0", cache_root=cache_root)
    # A different pin resolves to a different venv (so it is actually reinstalled),
    # instead of silently reusing the first one.
    assert first != second


def test_build_venv_installs_url_verbatim(tmp_path, monkeypatch):
    cache_root = tmp_path / "venvs"
    calls = []

    def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        # Make the built venv look like it has praktika so ensure_* returns.
        return _CompletedProcess()

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_run)

    url = "https://x/praktika-0.1.9-py3-none-any.whl"
    venv_manager.ensure_praktika_venv(url, cache_root=cache_root)
    install_calls = [
        c for c in calls if len(c) >= 4 and c[1:4] == ["-m", "pip", "install"]
    ]
    # The wheel URL reaches pip verbatim (not resolved to a filesystem path).
    assert any(url in c for c in install_calls)
