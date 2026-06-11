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
        status = "success"
        duration = 42

        @staticmethod
        def from_fs(job_name):
            return _FakeResult()

        def to_markdown(self, report_url=""):
            return "job markdown"

    monkeypatch.setattr("praktika.result.Result", _FakeResult)

    output = _build_check_output("unit test", 0, instance_id="i-runner456")

    assert output["title"] == "unit test"
    assert "runner `i-runner456`" in output["summary"]
    assert "**Runner instance:** `i-runner456`" in output["text"]
    assert "job markdown" in output["text"]


def test_job_runner_check_output_includes_report_url(monkeypatch):
    class _FakeResult:
        status = "success"
        duration = 10

        @staticmethod
        def from_fs(job_name):
            return _FakeResult()

        def to_markdown(self, report_url=""):
            return "job markdown"

    monkeypatch.setattr("praktika.result.Result", _FakeResult)

    url = "https://example.com/report?PR=1&sha=abc&name_0=CI&name_1=My+Job"
    output = _build_check_output("My Job", 0, report_url=url)

    assert output["title"] == "My Job"
    assert f"[CI Report]({url})" in output["summary"]
