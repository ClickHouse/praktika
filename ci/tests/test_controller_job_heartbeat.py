import logging
import sys
import types

from praktika_controller import controller


class _FakeHeartbeat:
    def __init__(self, *_args, **_kwargs):
        self.events = _kwargs["log"].events

    def start(self):
        self.events.append("heartbeat.start")

    def update(self, **fields):
        phase = fields.get("phase", "?")
        self.events.append(f"heartbeat.update:{phase}")

    def stop(self):
        self.events.append("heartbeat.stop")


class _FakeCancelWatchdog:
    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _FakeProc:
    returncode = 0
    pid = None

    def __init__(self, events):
        self._events = events

    def communicate(self):
        self._events.append("communicate")
        return None, ""


class _FakeLog(logging.Logger):
    def __init__(self):
        super().__init__("fake")
        self.events = []


def test_job_heartbeat_starts_before_runner_setup(monkeypatch, tmp_path):
    events = []
    log = _FakeLog()
    log.events = events
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()

    monkeypatch.setattr(controller, "Heartbeat", _FakeHeartbeat)
    monkeypatch.setattr(controller, "CancelWatchdog", _FakeCancelWatchdog)
    monkeypatch.setattr(controller, "get_github_token", lambda _region: "token")
    monkeypatch.setattr(controller, "_resolve_runtime", lambda *_: ("base", "/venv"))
    monkeypatch.setattr(controller, "praktika_command", lambda *_: ["praktika"])
    monkeypatch.setattr(controller, "_praktika_env", lambda *_: {})

    def fake_run(*_args, **_kwargs):
        events.append("gh-auth")
        return types.SimpleNamespace(returncode=0)

    def fake_clone_repo(*_args, **_kwargs):
        events.append("clone")
        return str(clone_dir), "actual-sha"

    def fake_popen(*_args, **kwargs):
        assert kwargs["start_new_session"] is True
        events.append("popen")
        return _FakeProc(events)

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    monkeypatch.setattr(controller.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(controller, "clone_repo", fake_clone_repo)

    class _FakeS3:
        def head_object(self, **_kwargs):
            error = Exception("not found")
            error.response = {"Error": {"Code": "404"}}
            raise error

    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_args, **_kwargs: _FakeS3()),
    )

    result = controller.handle_task(
        {
            "type": "job_task",
            "repo": "ClickHouse/silk",
            "pr_number": 69,
            "head_sha": "sha",
            "job_name": "Test",
            "heartbeat_s3_bucket": "bucket",
            "heartbeat_s3_key": "runs/1/Test/heartbeat.json",
            "cancel_s3_bucket": "bucket",
            "cancel_s3_key": "runs/1/cancel",
        },
        log,
        "queue",
    )

    assert result["status"] == "ok"
    assert events == [
        "heartbeat.start",
        "heartbeat.update:authenticating",
        "gh-auth",
        "heartbeat.update:cloning",
        "clone",
        "heartbeat.update:resolving_runtime",
        "heartbeat.update:writing_task",
        "heartbeat.update:running_job",
        "popen",
        "communicate",
        "heartbeat.stop",
    ]
