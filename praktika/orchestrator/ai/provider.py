"""AI provider boundary for the orchestrator.

This module defines the *stable contract* between the orchestrator loop and
whatever model decides what to do. The loop never imports an SDK directly; it
hands a serializable ``Observation`` to an ``AIProvider`` and gets back a
``Turn``. A real provider (Anthropic, OpenAI, ...) is added by subclassing
``AIProvider``, implementing the lifecycle hook(s) it cares about — formatting
the observation into a prompt + tool specs, running the SDK's tool-use loop, and
filling in ``Usage`` / ``Turn`` from the response — without touching
``praktika/orchestrator/__init__.py``.

The contract is **event-typed**: the orchestrator (via the ``Advisor``) calls a
hook named for the lifecycle event that fired — ``on_job_failure`` when a job
fails, ``on_job_success`` when one passes, ``on_run_start`` / ``on_run_finish``
around the run. Every hook is a **no-op by default** (returns ``None`` → no
model call, no recorded turn), so a provider implements only the events it
reacts to. Today's real providers implement ``on_job_failure`` only: a green
job never reaches the model.

For the current skeleton the only registered provider beyond the real ones is
``mock`` (see ``mock.py``), which returns a no-op ``Turn`` on failure.
"""
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

    def to_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "provider": self.provider,
            "model": self.model,
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

    def to_dict(self):
        return {
            "reasoning": self.reasoning,
            "decision": self.decision,
            "usage": self.usage.to_dict(),
            "error": self.error,
        }


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

    def resolved_model(self) -> str:
        """The model id this provider will actually use for a call.

        Falls back to a subclass ``DEFAULT_MODEL`` when ``model`` is unset, so
        accounting (incl. error Turns built before a call runs) names the real
        model rather than an empty string.
        """
        return self.model or getattr(self, "DEFAULT_MODEL", "")

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
    """Turn an ``AI_PROVIDER`` setting into a ready ``AIProvider`` instance.

    ``spec`` may be:

    * an ``AIProvider`` **instance** — used as-is (a user wired up their own
      provider object in ``settings.py``; ``model`` is whatever they gave it),
    * an ``AIProvider`` **subclass** — instantiated with ``model``,
    * a registered **name** (str) — looked up in the registry, then
      instantiated with ``model``.

    The instance/subclass paths are how a project plugs in a custom provider
    without touching this package — assign the object (or class) to
    ``AI_PROVIDER`` in ``ci/settings/settings.py``.
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

register(_mock.MockProvider)
register(_anthropic.AnthropicProvider)
register(_anthropic.BedrockProvider)
