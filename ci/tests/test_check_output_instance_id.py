from types import SimpleNamespace

from praktika.orchestrator import _check_output
from praktika.orchestrator.job_runner import _build_check_output


def test_orchestrator_check_output_includes_instance_id(monkeypatch):
    monkeypatch.setenv("INSTANCE_ID", "i-orchestrator123")

    workflow = SimpleNamespace(name="PR")
    state = SimpleNamespace(
        cancelled=False,
        md_status_summary=lambda: "1 running, 2 pending",
        md_status=lambda: "status table",
    )

    output = _check_output(workflow, state)

    assert output["title"] == "PR"
    assert "orchestrator `i-orchestrator123`" in output["summary"]
    assert "**Orchestrator instance:** `i-orchestrator123`" in output["text"]
    assert "status table" in output["text"]


def test_job_runner_check_output_includes_instance_id(monkeypatch):
    class _FakeResult:
        status = "OK"
        duration = 42

        @staticmethod
        def from_fs(job_name):
            return _FakeResult()

        def to_markdown(self, report_url=""):
            return "job markdown"

    monkeypatch.setattr("praktika.result.Result", _FakeResult)

    output = _build_check_output("unit test", 0, instance_id="i-runner456")

    assert output["title"] == "OK"
    assert "runner `i-runner456`" in output["summary"]
    assert "**Runner instance:** `i-runner456`" in output["text"]
    assert "job markdown" in output["text"]


def test_job_runner_check_output_includes_report_url(monkeypatch):
    class _FakeResult:
        status = "OK"
        duration = 10

        @staticmethod
        def from_fs(job_name):
            return _FakeResult()

        def to_markdown(self, report_url=""):
            return "job markdown"

    monkeypatch.setattr("praktika.result.Result", _FakeResult)

    url = "https://example.com/report?PR=1&sha=abc&name_0=CI&name_1=My+Job"
    output = _build_check_output("My Job", 0, report_url=url)

    assert output["title"] == "OK"
    assert f"[CI Report]({url})" in output["summary"]


def _make_fake_result(status, is_ok):
    class _FakeResult:
        duration = 10

        @staticmethod
        def from_fs(job_name):
            r = _FakeResult()
            r.status = status
            return r

        def is_ok(self):
            return is_ok

        def to_markdown(self, report_url=""):
            return "job markdown"

    r = _FakeResult()
    r.status = status
    return r


def test_check_output_rc0_ok_shows_ok_status(monkeypatch):
    """rc=0 and result OK → summary shows the result status as-is."""
    monkeypatch.setattr(
        "praktika.result.Result", type("R", (), {"from_fs": staticmethod(lambda _: _make_fake_result("OK", True))})
    )
    output = _build_check_output("My Job", rc=0)
    assert "**OK**" in output["summary"]
    assert output["title"] == "OK"
    assert "ERROR" not in output["summary"]
    assert "rc=" not in output["text"]


def test_check_output_rc_nonzero_ok_result_shows_error(monkeypatch):
    """rc!=0 but result says OK → runner crashed after reporting success.
    Summary must show ERROR and text must explain the crash."""
    monkeypatch.setattr(
        "praktika.result.Result", type("R", (), {"from_fs": staticmethod(lambda _: _make_fake_result("OK", True))})
    )
    output = _build_check_output("My Job", rc=137)
    assert "**ERROR**" in output["summary"]
    assert output["title"] == "ERROR"
    assert "rc=137" in output["text"]
    assert "OOM or disk-full" in output["text"]


def test_check_output_rc_nonzero_fail_result_shows_fail_status(monkeypatch):
    """rc!=0 and result is already FAIL → show the result status, no ERROR override."""
    monkeypatch.setattr(
        "praktika.result.Result", type("R", (), {"from_fs": staticmethod(lambda _: _make_fake_result("FAIL", False))})
    )
    output = _build_check_output("My Job", rc=1)
    assert "**FAILED**" in output["summary"]
    assert output["title"] == "FAILED"
    assert "ERROR" not in output["summary"]
    assert "rc=" not in output["text"]
