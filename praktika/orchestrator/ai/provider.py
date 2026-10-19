"""AI provider boundary for the orchestrator.

This module defines the *stable contract* between the orchestrator loop and
whatever model decides what to do. The loop never imports an SDK directly; it
hands a serializable ``Observation`` to an ``AIProvider`` and gets back a
``Turn``. A real provider (Anthropic, OpenAI, ...) is added by subclassing
``AIProvider``, implementing the lifecycle hook(s) it cares about — formatting
the observation into a prompt + tool specs, running the SDK's tool-use loop, and
filling in ``Usage`` / ``Turn`` from the response — without touching
``praktika/orchestrator/__init__.py``.

The contract is **event-typed**: the orchestrator (via the ``AIAssist``) calls a
hook named for the lifecycle event that fired — ``on_job_failure`` when a job
fails, ``on_job_success`` when one passes, ``on_run_start`` / ``on_run_finish``
around the run. Every hook is a **no-op by default** (returns ``None`` → no
model call, no recorded turn), so a provider implements only the events it
reacts to. Today's real providers implement ``on_job_failure`` only: a green
job never reaches the model.

For the current skeleton the only registered provider beyond the real ones is
``mock`` (see ``mock.py``), which returns a no-op ``Turn`` on failure.
"""
import fnmatch
from abc import ABC
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Usage:
    """Token + cost accounting for a single provider call.

    ``cost_usd`` is the seam for cost tracking: a real provider computes it
    from its own pricing table and the reported token counts. The mock leaves
    everything at zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    provider: str = ""
    model: str = ""
    # Investigation-loop accounting for a tool-using provider: how many tool
    # calls and tool-issuing rounds it ran, and the round budget. max_tool_rounds
    # == 0 means the provider does not run a tool loop (nothing to report).
    # exhausted is True only when the loop hit the cap and had to force a final
    # write-up - not merely when tool_rounds reaches the budget.
    tool_calls: int = 0
    tool_rounds: int = 0
    max_tool_rounds: int = 0
    exhausted: bool = False

    def to_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "model": self.model,
            "tool_calls": self.tool_calls,
            "tool_rounds": self.tool_rounds,
            "max_tool_rounds": self.max_tool_rounds,
            "exhausted": self.exhausted,
        }


@dataclass
class Observation:
    """Serializable snapshot of the run that the provider reasons over.

    Built by ``ai.build_observation`` from the live ``WorkflowState`` each time
    a job result lands. Everything here is plain JSON so it can be logged
    verbatim and, later, dropped straight into a prompt.
    """

    event: dict = field(default_factory=dict)  # type, action, pr_number, head_sha, ...
    jobs: List[dict] = field(default_factory=list)  # [{name, status, duration_s, info?}]
    # jobs that became terminal this turn; failed ones also carry a compact
    # Result digest under `result` ({status, info?, failed[], errors?}).
    changed: List[dict] = field(default_factory=list)
    summary: str = ""  # state.md_status_summary()
    # True when this observation batches a whole wave of failures (consolidated
    # drive mode) rather than a single tick's change. A framing hint for the
    # provider's prompt; not part of the serialized content.
    consolidated: bool = False

    def to_dict(self):
        return {
            "event": self.event,
            "jobs": self.jobs,
            "changed": self.changed,
            "summary": self.summary,
        }


@dataclass
class Turn:
    """The provider's response to one ``Observation``.

    ``decision`` is the list of actions the model wants to take. In the current
    advisory-only phase it is always empty and never applied — it is recorded
    so the flow is exercised end-to-end and so a real provider can populate it
    later without a loop change.
    """

    reasoning: str = ""  # free-text rationale, kept for the audit trail
    decision: List[dict] = field(default_factory=list)  # always [] in advisory phase
    usage: Usage = field(default_factory=Usage)
    raw: Optional[dict] = None  # provider-native response, for debugging
    error: Optional[str] = None
    # Whether re-running the identical call could plausibly succeed. A transient
    # API/network failure is retryable; a deterministic outcome of the current
    # configuration (e.g. the model exhausting its output-token budget before it
    # writes anything) is not — retrying it only wastes time and model spend.
    retryable: bool = True

    def to_dict(self):
        return {
            "reasoning": self.reasoning,
            "decision": self.decision,
            "usage": self.usage.to_dict(),
            "error": self.error,
            "retryable": self.retryable,
        }


# Lifecycle event name -> AIProvider hook method. The advisor dispatches by
# event name through `AIProvider.consult`, which brackets the hook with tracking.
_EVENT_HOOKS = {
    "run_start": "on_run_start",
    "job_failure": "on_job_failure",
    "job_success": "on_job_success",
    "run_finish": "on_run_finish",
}


def _md_cell(text, limit=90):
    """Flatten text into one table cell: escape pipes, drop newlines, truncate."""
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _event_cell(observation):
    """Summarize what triggered a turn: the jobs that changed this observation."""
    changed = getattr(observation, "changed", None) or []
    parts = [f"{c.get('name', '?')}: {c.get('status', '?')}" for c in changed]
    return _md_cell(", ".join(parts) or "(update)")


# The decision column carries the model's rationale, so give it a generous
# budget (the event column stays short). GitHub wraps long cells.
_DECISION_CELL_LIMIT = 800


def _decision_cell(turn):
    """Summarize the model's turn: decision type(s) + reasoning/detail."""
    if getattr(turn, "error", None):
        return _md_cell(f"⚠ error: {turn.error}", limit=_DECISION_CELL_LIMIT)
    decision = getattr(turn, "decision", None) or []
    reasoning = _md_cell(getattr(turn, "reasoning", ""), limit=240)
    types = [d.get("type", "?") for d in decision if isinstance(d, dict)]
    if not types:
        return (
            _md_cell(f"`continue` — {reasoning}", limit=_DECISION_CELL_LIMIT)
            if reasoning
            else "`continue`"
        )
    label = "`" + ", ".join(types) + "`"
    detail = next(
        (d.get("detail", "") for d in decision if isinstance(d, dict) and d.get("detail")),
        "",
    )
    if detail and reasoning:
        return _md_cell(
            f"{label} — {detail}. Reasoning: {reasoning}",
            limit=_DECISION_CELL_LIMIT,
        )
    if detail:
        return _md_cell(f"{label} — {detail}", limit=_DECISION_CELL_LIMIT)
    if reasoning:
        return _md_cell(f"{label} — {reasoning}", limit=_DECISION_CELL_LIMIT)
    return label


class AIProvider(ABC):
    """Base class for anything that reacts to workflow lifecycle events.

    The orchestrator routes each event to the matching hook below. All hooks
    default to a **no-op** (``return None``), so a subclass implements only the
    events it cares about; an unimplemented event never reaches the model and
    records no turn. A hook returns a ``Turn`` (reasoning + decision + usage)
    when it actually consults the model, or ``None`` to opt out for this event.
    """

    name: str = ""

    def __init__(self, model=""):
        self.model = model or ""
        # History of everything the provider exchanged with the model this run,
        # and the live "AI Assist" GitHub check it mirrors to (optional).
        self.observations = []  # observations sent, in order
        self.turns = []         # (observation, turn) pairs, as turns come back
        self._check_updater = None  # fn(status, summary_md); status in-progress|neutral
        self._check_open = False    # True between track_observation and track_turn
        # Project-specific guidance appended to the system prompt, keyed by a
        # job-name glob (see Workflow.AIAssist.Config.extra_prompt). The "*"
        # entry applies to every consult; other entries apply only when a job
        # whose name matches the glob is part of the consult. Empty adds nothing.
        self._extra_guidance = {}

    def set_extra_guidance(self, guidance):
        """Store project-specific guidance to append to the system prompt.

        Wired from ``Workflow.AIAssist.Config.extra_prompt`` by the advisor at
        construction time. ``guidance`` is a ``{job_name_glob: text}`` map — the
        ``"*"`` key applies to every consult, a glob key only when a job matching
        it is in the consult. A plain string is accepted too and treated as
        ``{"*": text}`` (global guidance). A provider folds the applicable
        entries into its system prompt via ``system_prompt``; providers that
        don't build a prompt simply ignore it.
        """
        if isinstance(guidance, str):
            guidance = {"*": guidance} if guidance.strip() else {}
        self._extra_guidance = {
            str(glob): text.strip()
            for glob, text in (guidance or {}).items()
            if isinstance(text, str) and text.strip()
        }

    def _applicable_guidance(self, observation=None):
        """Return the guidance texts applicable to ``observation``, in order.

        The ``"*"`` entry always applies; a glob entry applies when it matches
        any job named in ``observation.changed`` (the jobs that triggered this
        consult). ``"*"`` comes first, then the remaining matches in insertion
        order. Without an observation (run_start/finish), only ``"*"`` applies.
        """
        names = [
            c.get("name", "")
            for c in getattr(observation, "changed", None) or []
            if c.get("name")
        ]
        texts = []
        for glob, text in self._extra_guidance.items():
            if glob == "*":
                continue
            if any(fnmatch.fnmatch(name, glob) for name in names):
                texts.append(text)
        star = self._extra_guidance.get("*")
        return ([star] if star else []) + texts

    def system_prompt(self, base, observation=None):
        """Return ``base`` with the applicable extra guidance appended.

        Keeps the base/system prompt a module constant while letting a workflow
        steer the advisor (when to act, which failures to fix) without a code
        change. Only guidance whose job-name glob matches the jobs in this
        consult is appended (plus the always-on ``"*"`` entry). Returns ``base``
        unchanged when no guidance applies."""
        texts = self._applicable_guidance(observation)
        if not texts:
            return base
        return (
            f"{base}\n\n"
            "Additional project-specific guidance (follow it when it applies):\n"
            + "\n\n".join(texts)
        )

    def complete(
        self,
        system,
        user_content,
        tools=None,
        tool_executor=None,
        max_tokens=4000,
        response_schema=None,
    ) -> "Turn":
        """One-shot model call outside the orchestrator lifecycle.

        The seam the standalone jobs (e.g. ``praktika review``) use to talk to
        *any* provider without touching the event-hook machinery. Given a
        ``system`` prompt, a ``user_content`` string, and an optional toolset
        (name/description/``input_schema`` dicts, same shape ``on_job_failure``
        uses), the provider runs its own tool-use loop — calling
        ``tool_executor(name, input) -> str`` for each tool the model invokes,
        so tool execution stays with the caller — and returns a ``Turn`` whose
        ``reasoning`` holds the model's final raw text and whose ``usage`` is
        filled. The caller parses ``reasoning`` however it needs.

        ``response_schema`` (a JSON Schema dict), when set, requests
        **structured output**: the provider offers a forced ``submit_result``
        tool whose input schema is ``response_schema`` and returns the
        submitted arguments as a JSON string in ``Turn.reasoning``. This is far
        more reliable than parsing free-text JSON from a reasoning model, which
        tends to intermix its analysis with the answer. A provider that doesn't
        implement structured output ignores the schema and returns free text.

        No default implementation: a provider that supports one-shot completion
        overrides this. Providers that only react to lifecycle events (e.g. the
        mock) leave it unimplemented.
        """
        raise NotImplementedError(
            f"provider {self.name!r} does not implement complete()"
        )

    def resolved_model(self) -> str:
        """The model id this provider will actually use for a call.

        Falls back to a subclass ``DEFAULT_MODEL`` when ``model`` is unset, so
        accounting (incl. error Turns built before a call runs) names the real
        model rather than an empty string.
        """
        return self.model or getattr(self, "DEFAULT_MODEL", "")

    # -------------------------------------------------- observation/turn tracking
    # The model-calling hooks bracket their round-trip with these two methods so
    # the provider keeps the full observation/turn history and drives the "AI
    # Advisor" GitHub check: in-progress while an observation is out, neutral
    # once the turn lands. Both are non-abstract — a concrete provider can
    # override either to change what is tracked or how the check is rendered.

    def attach_check_updater(self, updater):
        """Wire the AI Assist check updater: ``fn(status, summary_md)`` where
        status is ``"in_progress"`` or ``"neutral"``. Best-effort — the updater
        swallows its own errors; passing None disables check reporting."""
        self._check_updater = updater

    def track_observation(self, observation):
        """Record an observation about to be sent to the model and flip the AI
        Advisor check to *in-progress* (with the table so far + the active row)."""
        self.observations.append(observation)
        self._check_open = True
        self._push_check("in_progress", active=observation)

    def track_turn(self, observation, turn):
        """Record the turn the model returned and flip the AI Assist check to
        *neutral*, refreshing the decision table."""
        self.turns.append((observation, turn))
        self._check_open = False
        self._push_check("neutral")

    def finalize_check(self):
        """Ensure the check isn't left in-progress if a round-trip died mid-flight."""
        if self._check_open:
            self._check_open = False
            self._push_check("neutral")

    def _push_check(self, status, active=None):
        if self._check_updater is None:
            return
        try:
            self._check_updater(status, self._render_check_table(active=active))
        except Exception as e:  # never let observability break the run
            print(f"[AI   ] advisor check update failed: {type(e).__name__}: {e}")

    def _render_check_table(self, active=None):
        """Concise markdown for the AI Assist check: one row per decision with
        the event that triggered it, plus an in-progress row while investigating."""
        cost = sum((t.usage.cost_usd or 0.0) for _, t in self.turns)
        head = (
            f"**AI Assist** — provider `{self.name}`, model "
            f"`{self.resolved_model()}` · {len(self.turns)} decision(s) · "
            f"${cost:.4f}"
        )
        rows = ["", "| # | Event | Decision |", "|---|---|---|"]
        for i, (obs, turn) in enumerate(self.turns, 1):
            rows.append(f"| {i} | {_event_cell(obs)} | {_decision_cell(turn)} |")
        if active is not None:
            rows.append(f"| … | {_event_cell(active)} | ⏳ investigating… |")
        return "\n".join([head, *rows])

    def consult(self, event, observation) -> Optional["Turn"]:
        """Dispatch a lifecycle ``event`` to its hook, with tracking built in.

        This is the entry point the advisor calls (not the hooks directly). It
        gives **every** provider observation/turn history and the AI Assist
        check for free — the hooks stay pure model logic:

        * an unimplemented hook (still the base no-op) returns ``None`` with no
          tracking and no model call;
        * otherwise the observation is tracked (check -> in-progress), the hook
          runs, and its turn is tracked (check -> neutral);
        * a hook exception becomes an error ``Turn`` (never raised), so a
          provider bug can't take down the orchestration loop.
        """
        hook_name = _EVENT_HOOKS.get(event)
        if hook_name is None:
            return None
        if getattr(type(self), hook_name) is getattr(AIProvider, hook_name):
            return None  # hook not overridden -> no-op; skip tracking entirely
        self.track_observation(observation)
        try:
            turn = getattr(self, hook_name)(observation)
        except Exception as e:
            turn = Turn(
                error=f"{type(e).__name__}: {e}",
                usage=Usage(provider=self.name, model=self.resolved_model()),
            )
            self.track_turn(observation, turn)
            return turn
        if turn is None:
            self._check_open = False  # hook opted out after all; nothing tracked
            return None
        self.track_turn(observation, turn)
        return turn

    # ----------------------------------------------------------- event hooks
    # Each receives the Observation for its event and returns a Turn, or None
    # (the default) to opt out — no model call, nothing recorded. Override only
    # the hooks the provider acts on.

    def on_run_start(self, observation: Observation) -> Optional[Turn]:
        """The run just started (no job is terminal yet)."""
        return None

    def on_job_failure(self, observation: Observation) -> Optional[Turn]:
        """One or more jobs failed this turn (``changed`` carries their digests)."""
        return None

    def on_job_success(self, observation: Observation) -> Optional[Turn]:
        """One or more jobs passed this turn."""
        return None

    def on_run_finish(self, observation: Observation) -> Optional[Turn]:
        """The run reached a terminal state."""
        return None


# name -> AIProvider subclass. Populated below; new providers register here.
_REGISTRY = {}


def register(cls):
    """Class decorator: register an AIProvider subclass under its ``name``."""
    assert cls.name, f"{cls.__name__} must set a non-empty `name`"
    _REGISTRY[cls.name] = cls
    return cls


def resolve(name) -> type:
    """Look up a provider class by name. Raises KeyError on unknown names."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown AI provider {name!r}; registered: {sorted(_REGISTRY)}"
        )


def resolve_provider(spec, model="") -> "AIProvider":
    """Turn a workflow AI provider spec into a ready ``AIProvider`` instance.

    ``spec`` may be:

    * an ``AIProvider`` **instance** — used as-is (a workflow wired up its own
      provider object; ``model`` is whatever it was given),
    * an ``AIProvider`` **subclass** — instantiated with ``model``,
    * a registered **name** (str) — looked up in the registry, then
      instantiated with ``model``.

    The instance/subclass paths are how a workflow plugs in a custom provider
    without touching this package — assign the object (or class) to
    ``Workflow.Config.ai_assist.provider``.
    """
    if isinstance(spec, AIProvider):
        return spec
    if isinstance(spec, type) and issubclass(spec, AIProvider):
        return spec(model=model)
    return resolve(spec)(model=model)


# Register built-in providers. Imported here (not at module top) to avoid a
# circular import: mock.py imports the dataclasses from this module.
from . import mock as _mock  # noqa: E402
from . import anthropic as _anthropic  # noqa: E402
from . import bedrock_openai as _bedrock_openai  # noqa: E402

register(_mock.MockProvider)
register(_anthropic.AnthropicProvider)
register(_anthropic.BedrockAnthropicProvider)
register(_bedrock_openai.BedrockOpenAIProvider)
