"""AI advisor for the orchestrator (skeleton).

The orchestrator consults an ``Advisor`` every time the workflow state advances
(a job result lands). The advisor builds a serializable ``Observation``, asks a
pluggable ``AIProvider`` what to do, and records the turn (reasoning + decision
+ token usage + cost). This is the *plumbing* — the flow is wired end-to-end so
real model providers and, later, action dispatch slot in without changing the
orchestrator loop.

Current scope (advisory-only): the provider's ``decision`` is recorded but
never applied to the run. The default provider is ``mock``, which does nothing.
Disabled by default (``Settings.AI_ORCHESTRATION_ENABLED``).
"""
from praktika.settings import Settings

from .provider import Observation, Turn, Usage, resolve
from .session import SessionManager
from .trace import TraceLogger, UsageLedger

# Job status values (JobStatus.value) that count as terminal — a result has
# landed. Kept as plain strings so the advisor stays decoupled from the
# JobStatus enum and is trivial to drive with stubs in tests.
_TERMINAL_STATUSES = {"success", "failure", "skipped", "cancelled"}


def _status_value(job_state):
    """Best-effort read of a JobState's status as a plain string."""
    status = getattr(job_state, "status", None)
    return getattr(status, "value", status)


def build_observation(state, event, changed) -> Observation:
    """Snapshot the live WorkflowState into a serializable Observation."""
    jobs = []
    for js in state.jobs.values():
        started = getattr(js, "started_at", None)
        finished = getattr(js, "finished_at", None)
        duration_s = round(finished - started, 1) if started and finished else None
        entry = {"name": js.name, "status": _status_value(js)}
        if duration_s is not None:
            entry["duration_s"] = duration_s
        reason = getattr(js, "filter_reason", None)
        if reason:
            entry["reason"] = reason
        jobs.append(entry)

    ev = event or {}
    return Observation(
        event={
            "type": ev.get("type", ""),
            "action": ev.get("action", ""),
            "pr_number": ev.get("pr_number"),
            "head_sha": ev.get("head_sha", ""),
            "head_ref": ev.get("head_ref", ""),
        },
        jobs=jobs,
        changed=changed,
        summary=state.md_status_summary() if hasattr(state, "md_status_summary") else "",
    )


class Advisor:
    """Consults an AIProvider on every workflow update and records the turn."""

    def __init__(self, provider, console, session=None):
        self._provider = provider
        self._console = console
        self._session = session
        self._ledger = UsageLedger()
        # Last status value seen per job — used to fire a turn only when a job
        # newly reaches a terminal state (a result was actually received).
        self._last_status = {}

    @classmethod
    def maybe_create(cls, event=None, run_id=None, local_mode=False):
        """Build an Advisor from Settings, or return None if AI is disabled.

        Also opens (or rejoins) the durable per-PR AI session and registers
        this CI run with it. Session setup is best-effort: if it fails the
        advisor still runs with console-only output.
        """
        if not getattr(Settings, "AI_ORCHESTRATION_ENABLED", False):
            return None
        provider_name = getattr(Settings, "AI_PROVIDER", "mock") or "mock"
        model = getattr(Settings, "AI_MODEL", "") or ""
        provider = resolve(provider_name)(model=model)
        console = TraceLogger(run_id=run_id)
        print(
            f"[AI   ] advisor enabled: provider={provider_name} model={model or '(default)'}"
        )

        session = None
        if event is not None:
            try:
                session = SessionManager.from_event(
                    event, run_id=run_id, local_mode=local_mode, console=console
                )
                session.begin_run(
                    run_id=run_id or "", sha=event.get("head_sha", ""), event=event
                )
            except Exception as e:
                print(f"  [warn] AI session unavailable: {type(e).__name__}: {e}")
                session = None
        return cls(provider, console, session=session)

    def _delta(self, state):
        """Return [{name, status, ...}] for jobs newly terminal since last turn."""
        changed = []
        for name, js in state.jobs.items():
            value = _status_value(js)
            if value == self._last_status.get(name):
                continue
            self._last_status[name] = value
            if value in _TERMINAL_STATUSES:
                entry = {"name": name, "status": value}
                reason = getattr(js, "filter_reason", None)
                if reason:
                    entry["reason"] = reason
                changed.append(entry)
        return changed

    def _safe_decide(self, observation) -> Turn:
        """Call the provider, converting any failure into an error Turn so a
        provider bug can never take down the orchestration loop."""
        try:
            return self._provider.decide(observation)
        except Exception as e:
            return Turn(
                error=f"{type(e).__name__}: {e}",
                usage=Usage(
                    provider=getattr(self._provider, "name", ""),
                    model=getattr(self._provider, "model", ""),
                ),
            )

    def on_workflow_update(self, state, event):
        """Hook called from the orchestrator loop after each ``state.wait()``.

        Fires a turn only when at least one job newly reached a terminal state;
        idle poll ticks are ignored. Advisory-only: the returned decision is
        recorded, never applied.
        """
        changed = self._delta(state)
        if not changed:
            return None
        observation = build_observation(state, event, changed)
        turn = self._safe_decide(observation)
        self._ledger.add(turn.usage)
        if self._session is not None:
            # Session owns persistence + console + index; it also opens a round
            # implicitly on the first failure.
            self._session.observe_turn(observation, turn)
        else:
            self._console.record(observation, turn)
        return turn

    def finalize(self, state=None):
        """End-of-run hook: print the run cost total and close out the session.

        On a green run an open round auto-resolves; otherwise it is left open
        and persisted, so the next CI run on this PR rejoins it.
        """
        self._console.summary(self._ledger)
        if self._session is not None:
            self._session.finalize_run(
                conclusion=_run_conclusion(state),
                job_outcomes=_job_outcomes(state),
            )


def _run_conclusion(state):
    if state is None:
        return "error"
    if getattr(state, "cancelled", False):
        return "cancelled"
    if state.any_failed():
        return "failure"
    return "success"


def _job_outcomes(state):
    if state is None:
        return []
    return [{"name": js.name, "status": _status_value(js)} for js in state.jobs.values()]
