"""Mock AI provider — does nothing.

Returns a no-op ``Turn`` (empty decision, zero usage, no network call) so the
orchestrator can exercise the full advisor flow without any real model. This is
the default provider for the skeleton; replace it by registering a real
``AIProvider`` subclass and pointing ``Settings.AI_PROVIDER`` at it.
"""
from .provider import AIProvider, Turn, Usage


class MockProvider(AIProvider):
    name = "mock"

    def decide(self, observation) -> Turn:
        # Intentionally inspects nothing and changes nothing.
        return Turn(
            reasoning="noop: mock provider",
            decision=[],
            usage=Usage(provider=self.name, model=self.model),
        )
