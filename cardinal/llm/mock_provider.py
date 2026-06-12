"""Mock LLM provider — zero-token end-to-end pipeline testing.

Outputs structured responses matching the EXACT schemas expected by the
healer (raw Python function), balancer (items JSON array), quest generator
(GDD JSON), and sentiment parser (score JSON array).

Deterministic, requires zero API tokens, and is selected automatically when
ANTHROPIC_API_KEY is absent AND CARDINAL_USE_MOCK=true. The pytest suite
uses it throughout so the entire Cardinal pipeline — gate flow, spend-guard
lockout paths, replay capture, balancer cycles — can be exercised without
spending a single token.

It shares the deterministic transforms of the L2 LocalRuleProvider (the
response *content* logic is identical by design — what differs is intent:
L2 is the production degradation tier, Mock simulates Fable 5's response
shape for tests and demos).
"""
from __future__ import annotations

from typing import Any

from cardinal.llm.provider import LLMResponse, LocalRuleProvider


class MockProvider(LocalRuleProvider):
    name = "mock"

    def _complete(self, system: str, user: str, max_tokens: int,
                  action: str, context: dict[str, Any]) -> LLMResponse:
        resp = super()._complete(system, user, max_tokens, action, context)
        # Simulate plausible token accounting (deterministic, cost 0).
        return LLMResponse(
            text=resp.text,
            provider=self.name,
            input_tokens=len(user) // 4,
            output_tokens=len(resp.text) // 4,
            cost_usd=0.0,
            meta={"mock": True},
        )
