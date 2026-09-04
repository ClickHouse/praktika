from types import SimpleNamespace

from praktika.orchestrator import _check_output
from praktika.orchestrator.state import _build_check_output


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


def _make_fake_result(status, is_ok, duration=10):
    class _FakeResult:
        def __init__(self):
            self.status = status
            self.duration = duration

        def is_ok(self):
            return is_ok

        def to_markdown(self, report_url=""):
            return "job markdown"

    return _FakeResult()


def test_job_runner_check_output_includes_instance_id():
    output = _build_check_output(
        _make_fake_result("OK", True, duration=42), 0, instance_id="i-runner456"
    )

    assert output["title"] == "OK"
    assert "runner `i-runner456`" in output["summary"]
    assert "**Runner instance:** `i-runner456`" in output["text"]
    assert "job markdown" in output["text"]


def test_job_runner_check_output_includes_pool():
    output = _build_check_output(
        _make_fake_result("FAIL", False),
        rc=1,
        instance_id="i-runner456",
        pool="arm-2xsmall-bedrock",
    )

    assert "runner `i-runner456`" in output["summary"]
    assert "pool `arm-2xsmall-bedrock`" in output["summary"]
    assert "**Runner pool:** `arm-2xsmall-bedrock`" in output["text"]


def test_job_runner_check_output_omits_pool_when_absent():
    output = _build_check_output(_make_fake_result("OK", True), 0, instance_id="i-x")
    assert "pool `" not in output["summary"]
    assert "Runner pool" not in output["text"]


def test_job_runner_check_output_includes_report_url():
    url = "https://example.com/report?PR=1&sha=abc&name_0=CI&name_1=My+Job"
    output = _build_check_output(_make_fake_result("OK", True), 0, report_url=url)

    assert output["title"] == "OK"
    assert f"[CI Report]({url})" in output["summary"]


def test_check_output_rc0_ok_shows_ok_status():
    """rc=0 and result OK → summary shows the result status as-is."""
    output = _build_check_output(_make_fake_result("OK", True), rc=0)
    assert "**OK**" in output["summary"]
    assert output["title"] == "OK"
    assert "ERROR" not in output["summary"]
    assert "rc=" not in output["text"]


def test_check_output_rc_nonzero_ok_result_shows_error():
    """rc!=0 but result says OK → runner crashed after reporting success.
    Summary must show ERROR and text must explain the crash."""
    output = _build_check_output(_make_fake_result("OK", True), rc=137)
    assert "**ERROR**" in output["summary"]
    assert output["title"] == "ERROR"
    assert "rc=137" in output["text"]
    assert "OOM or disk-full" in output["text"]


def test_check_output_rc_nonzero_fail_result_shows_fail_status():
    """rc!=0 and result is already FAIL → show the result status, no ERROR override."""
    output = _build_check_output(_make_fake_result("FAIL", False), rc=1)
    assert "**FAILED**" in output["summary"]
    assert output["title"] == "FAILED"
    assert "ERROR" not in output["summary"]
    assert "rc=" not in output["text"]


def _running_state():
    return SimpleNamespace(
        cancelled=False,
        md_status_summary=lambda: "1 running",
        md_status=lambda: "status table",
    )


def test_check_output_hides_first_attempt(monkeypatch):
    # attempt 1/3 is the normal first attempt — not surfaced (it's noise).
    monkeypatch.setenv("PRAKTIKA_ATTEMPT", "1/3")
    out = _check_output(SimpleNamespace(name="PR"), _running_state())
    assert "attempt 1/3" not in out["summary"]
    assert "Attempt" not in out["text"]


def test_check_output_shows_genuine_retry_attempt(monkeypatch):
    # A real infra retry (N > 1) IS surfaced.
    monkeypatch.setenv("PRAKTIKA_ATTEMPT", "2/3")
    out = _check_output(SimpleNamespace(name="PR"), _running_state())
    assert "attempt 2/3" in out["summary"]
    assert "**Attempt:** `2/3`" in out["text"]


def test_check_output_rerun_does_not_show_reset_attempt(monkeypatch):
    # A re-run resume is a fresh 1/3 orchestrator: show the re-run marker but not
    # a "reset" attempt counter next to it.
    monkeypatch.setenv("PRAKTIKA_ATTEMPT", "1/3")
    out = _check_output(SimpleNamespace(name="PR"), _running_state(), is_rerun=True)
    assert "🔁 re-run" in out["summary"]
    assert "attempt 1/3" not in out["summary"]
    assert "Attempt" not in out["text"]
