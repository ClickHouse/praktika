"""Local smoke test for the AI advisor + AnthropicProvider.

Drives the real OrchestratorAI (workflow-resolved provider, local session store) against
a stub WorkflowState, simulating a job failing so a turn fires and the Anthropic
provider is actually called. Run from the repo root with ANTHROPIC_API_KEY set:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python _local_ai_smoke.py

Not a pytest test — a throwaway harness. Delete when done.
"""
from types import SimpleNamespace

from praktika import Workflow
from praktika.orchestrator.ai import OrchestratorAI


class _Status:
    def __init__(self, value):
        self.value = value


class _Job:
    def __init__(self, name, status, started=None, finished=None, reason=None):
        self.name = name
        self.status = _Status(status)
        self.started_at = started
        self.finished_at = finished
        self.filter_reason = reason


class StubState:
    """Minimal stand-in for WorkflowState — only what the advisor reads."""

    def __init__(self, jobs):
        self.jobs = {j.name: j for j in jobs}
        self.cancelled = False

    def md_status_summary(self):
        return ", ".join(f"{j.name}={j.status.value}" for j in self.jobs.values())

    def any_failed(self):
        return any(j.status.value == "failure" for j in self.jobs.values())


def main():
    event = {
        "type": "pull_request",
        "action": "synchronize",
        "pr_number": 9999,
        "head_sha": "deadbeefcafe",
        "head_ref": "ai-orchestration-smoke",
        "repo": "ClickHouse/ClickHouse",
    }

    workflow = SimpleNamespace(
        orchestrator_ai=Workflow.OrchestratorAI.Config(
            enabled=True,
            provider="anthropic",
        )
    )
    advisor = OrchestratorAI.maybe_create(
        event=event,
        run_id="local-smoke-1",
        local_mode=True,
        workflow_config=workflow,
    )
    if advisor is None:
        raise SystemExit("Advisor disabled — check Workflow.Config.orchestrator_ai")

    state = StubState(
        [
            _Job("Build (amd64)", "running", started=1000.0),
            _Job("Style check", "success", started=1000.0, finished=1042.0),
            _Job("Stateless tests", "pending"),
        ]
    )

    # Turn 1: only a success became terminal — advisor records it, no round opens.
    advisor.on_workflow_update(state, event)

    # Build now fails -> a job newly reaches a terminal failure -> a turn fires,
    # a round opens, and AnthropicProvider.decide() is actually called.
    state.jobs["Build (amd64)"].status = _Status("failure")
    state.jobs["Build (amd64)"].finished_at = 1180.0
    turn = advisor.on_workflow_update(state, event)

    print("\n--- Turn returned to orchestrator ---")
    print("error    :", turn.error)
    print("reasoning:", (turn.reasoning or "")[:500])
    print("decision :", turn.decision)
    print("usage    :", turn.usage.to_dict())

    advisor.finalize(state)


if __name__ == "__main__":
    main()
