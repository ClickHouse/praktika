"""AI provider boundary for the orchestrator.

This module defines the *stable contract* between the orchestrator loop and
whatever model decides what to do. The loop never imports an SDK directly; it
hands a serializable ``Observation`` to an ``AIProvider`` and gets back a
``Turn``. A real provider (Anthropic, OpenAI, ...) is added by subclassing
``AIProvider``, formatting the observation into a prompt + tool specs, running
the SDK's tool-use loop, and filling in ``Usage`` / ``Turn`` from the response —
without touching ``praktika/orchestrator/__init__.py``.

For the current skeleton the only registered provider is ``mock`` (see
``mock.py``), which returns a no-op ``Turn``.
"""
from abc import ABC, abstractmethod
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
    changed: List[dict] = field(default_factory=list)  # jobs that became terminal this turn
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
    """Base class for anything that decides what to do on a workflow update."""

    name: str = ""

    def __init__(self, model=""):
        self.model = model or ""

    @abstractmethod
    def decide(self, observation: Observation) -> Turn:
        """Inspect the observation and return a Turn (reasoning + decision + usage)."""
        raise NotImplementedError


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


# Register built-in providers. Imported here (not at module top) to avoid a
# circular import: mock.py imports the dataclasses from this module.
from . import mock as _mock  # noqa: E402

register(_mock.MockProvider)
