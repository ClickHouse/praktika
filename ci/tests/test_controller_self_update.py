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
        json.dumps({"installed_source": "praktika-controller==1.0", "failed": {}}),
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


def test_spec_matching_running_version_persists_without_install(
    state_path, log, monkeypatch
):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "2.0")
    calls = []
    assert not self_update.maybe_self_update(
        "praktika-controller==2.0",
        log,
        python="py",
        run=lambda *a, **k: calls.append(a) or _Proc(),
    )
    assert calls == []
    saved = _read(state_path)
    assert saved["installed_source"] == "praktika-controller==2.0"
    assert saved["installed_version"] == "2.0"


def test_new_source_installs_verifies_persists_and_restarts(
    state_path, log, monkeypatch
):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "1.0")

    def run(cmd, **kwargs):
        if "install" in cmd:
            return _Proc(returncode=0)
        # verify import
        return _Proc(returncode=0, stdout="3.0\n")

    assert self_update.maybe_self_update(
        "https://x/praktika_controller-3.0-py3-none-any.whl",
        log,
        python="py",
        run=run,
    )
    saved = _read(state_path)
    assert saved["installed_source"].endswith("praktika_controller-3.0-py3-none-any.whl")
    assert saved["installed_version"] == "3.0"
    assert saved["failed"] == {}


def test_verify_failure_does_not_restart_and_restores_previous(
    state_path, log, monkeypatch
):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "1.0")
    state_path.write_text(
        json.dumps({"installed_source": "praktika-controller==1.0", "failed": {}}),
        encoding="utf-8",
    )
    installs = []

    def run(cmd, **kwargs):
        if "install" in cmd:
            installs.append(cmd)
            return _Proc(returncode=0)
        return _Proc(returncode=1, stderr="ImportError")  # verify fails

    assert not self_update.maybe_self_update(
        "praktika-controller==9.9", log, python="py", run=run
    )
    saved = _read(state_path)
    # Did not adopt the broken version; attempt was counted.
    assert saved["installed_source"] == "praktika-controller==1.0"
    assert saved["failed"]["praktika-controller==9.9"] == 1
    # Best-effort restore of the previous good source ran.
    assert any("praktika-controller==1.0" in c for c in installs)


def test_crash_loop_cap_stops_reinstalling(state_path, log, monkeypatch):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "1.0")
    state_path.write_text(
        json.dumps(
            {
                "installed_source": "praktika-controller==1.0",
                "failed": {"praktika-controller==9.9": self_update.MAX_ATTEMPTS},
            }
        ),
        encoding="utf-8",
    )
    calls = []
    assert not self_update.maybe_self_update(
        "praktika-controller==9.9",
        log,
        python="py",
        run=lambda *a, **k: calls.append(a) or _Proc(),
    )
    assert calls == []  # gave up; no further reinstall attempts


def test_relative_path_pin_is_rejected_without_install(state_path, log):
    calls = []
    for src in (".", "./bootstrap", "bootstrap/src"):
        assert not self_update.maybe_self_update(
            src, log, python="py", run=lambda *a, **k: calls.append(a) or _Proc()
        )
    assert calls == []  # never attempts an install for an unresolvable relative path
    # Absolute path / URL / spec are accepted (validation passes).
    assert self_update._is_valid_controller_source("/opt/praktika/src")
    assert self_update._is_valid_controller_source("https://x/y.whl")
    assert self_update._is_valid_controller_source("praktika-controller==1.0")
    assert not self_update._is_valid_controller_source(".")


def test_wrong_package_spec_rejected_before_install(state_path, log):
    # A pin targeting the wrong project (typo: praktika instead of
    # praktika-controller) must be rejected before any install, so the old
    # controller isn't silently persisted as converged.
    calls = []
    assert not self_update.maybe_self_update(
        "praktika==0.1.8",
        log,
        python="py",
        run=lambda *a, **k: calls.append(a) or _Proc(),
    )
    assert calls == []
    assert not state_path.exists()


def test_wrong_package_wheel_url_rejected(state_path, log):
    calls = []
    assert not self_update.maybe_self_update(
        "https://x/some_other_pkg-1.0-py3-none-any.whl",
        log,
        python="py",
        run=lambda *a, **k: calls.append(a) or _Proc(),
    )
    assert calls == []


def test_installed_version_mismatch_is_not_adopted(state_path, log, monkeypatch):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "1.0")
    state_path.write_text(
        json.dumps({"installed_source": "praktika-controller==1.0", "failed": {}}),
        encoding="utf-8",
    )
    installs = []

    def run(cmd, **kwargs):
        if "install" in cmd:
            installs.append(cmd)
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="1.0\n")  # verify: still the OLD version

    # Pin asks for 5.0 but the install resolved to 1.0 -> must not adopt.
    assert not self_update.maybe_self_update(
        "praktika-controller==5.0", log, python="py", run=run
    )
    saved = _read(state_path)
    assert saved["installed_source"] == "praktika-controller==1.0"  # unchanged
    assert saved["failed"]["praktika-controller==5.0"] == 1


def test_break_system_packages_retry_on_pep668(state_path, log, monkeypatch):
    monkeypatch.setattr(self_update, "current_controller_version", lambda: "1.0")
    cmds = []

    def run(cmd, **kwargs):
        if "install" in cmd:
            cmds.append(cmd)
            if "--break-system-packages" not in cmd:
                return _Proc(returncode=1, stderr="error: externally-managed-environment")
            return _Proc(returncode=0)
        return _Proc(returncode=0, stdout="3.0\n")

    assert self_update.maybe_self_update(
        "praktika-controller==3.0", log, python="py", run=run
    )
    assert any("--break-system-packages" in c for c in cmds)
