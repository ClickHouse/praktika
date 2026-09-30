import json
import logging

import pytest

from praktika_controller import self_update


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "controller_version.json"
    monkeypatch.setattr(self_update, "STATE_PATH", path)
    return path


@pytest.fixture
def log():
    return logging.getLogger("test_self_update")


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _read(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))


def test_empty_source_is_noop(state_path, log):
    calls = []
    assert not self_update.maybe_self_update(
        "", log, python="py", run=lambda *a, **k: calls.append(a) or _Proc()
    )
    assert calls == []
    assert not state_path.exists()


def test_same_source_does_not_reinstall(state_path, log):
    state_path.write_text(
        json.dumps({"installed_source": "praktika-controller==1.0"}),
        encoding="utf-8",
    )
    calls = []
    assert not self_update.maybe_self_update(
        "praktika-controller==1.0",
        log,
        python="py",
        run=lambda *a, **k: calls.append(a) or _Proc(),
    )
    assert calls == []  # persistence: no reinstall unless the pin changes


def test_new_source_installs_persists_and_restarts(state_path, log):
    installs = []

    def run(cmd, **kwargs):
        installs.append(cmd)
        return _Proc(returncode=0)

    assert self_update.maybe_self_update(
        "https://x/praktika_controller-3.0-py3-none-any.whl",
        log,
        python="py",
        run=run,
    )
    saved = _read(state_path)
    assert saved["installed_source"].endswith("praktika_controller-3.0-py3-none-any.whl")
    # No post-install verification in dev mode: install then persist + restart.
    assert any("install" in c and "--force-reinstall" in c for c in installs)


def test_bad_pin_fails_hard(state_path, log):
    # A bad/unreachable pin is not validated or rolled back — pip's failure
    # propagates so the failure is loud.
    def run(cmd, **kwargs):
        return _Proc(returncode=1, stderr="ERROR: 404 Not Found")

    with pytest.raises(Exception):
        self_update.maybe_self_update(
            "https://x/does-not-exist-9.9.whl", log, python="py", run=run
        )
    # Nothing persisted as converged.
    assert not state_path.exists()


def test_break_system_packages_retry_on_pep668(state_path, log):
    cmds = []

    def run(cmd, **kwargs):
        cmds.append(cmd)
        if "--break-system-packages" not in cmd:
            return _Proc(returncode=1, stderr="error: externally-managed-environment")
        return _Proc(returncode=0)

    assert self_update.maybe_self_update(
        "praktika-controller==3.0", log, python="py", run=run
    )
    assert any("--break-system-packages" in c for c in cmds)
    assert _read(state_path)["installed_source"] == "praktika-controller==3.0"
