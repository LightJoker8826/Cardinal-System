"""LLM provider layer — the three-tier execution model.

  L3  AnthropicProvider : Fable 5 via API, prompt-cached, token-capped,
                          spend-guarded. Used ONLY for Tier-3 escalations.
  L2  LocalRuleProvider : deterministic rule-based fallback. Handles the
                          known bug classes, mechanical rebalances, and
                          template GDDs with zero API calls.
  --  MockProvider      : (cardinal.llm.mock_provider) schema-exact
                          deterministic responses for end-to-end testing.

Selection (get_provider):
  1. ANTHROPIC_API_KEY present              -> AnthropicProvider
  2. key absent and CARDINAL_USE_MOCK=true  -> MockProvider
  3. otherwise                              -> LocalRuleProvider

Rules enforced here:
  - Hard max_tokens on every L3 call (2048 patches / 4096 GDDs).
  - cache_control on system prompts (90% discount on repeated loop calls).
  - The spend guard is consulted before and charged after every L3 call.
  - Max ONE L3 call per event: complete_with_fallback() falls back to L2
    immediately if the first L3 attempt fails — it never retries L3.
  - Every call is recorded in agent_log.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_DEBUG, SEV_WARNING, get_config, log_event
from cardinal.llm import spend_guard

ANTI_OVERTHINK = (
    "When you have enough information to act, act immediately. "
    "Do not re-derive established facts. "
    "Do not write commentary on alternative approaches. "
    "Output only the deliverable requested."
)

MAX_TOKENS_PATCH = 2048
MAX_TOKENS_GDD = 4096
MAX_TOKENS_DEFAULT = 2048


@dataclass
class LLMResponse:
    text: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def _complete(self, system: str, user: str, max_tokens: int,
                  action: str, context: dict[str, Any]) -> LLMResponse:
        ...

    def complete(self, module: str, action: str, system: str, user: str,
                 max_tokens: int = MAX_TOKENS_DEFAULT,
                 context: dict[str, Any] | None = None) -> LLMResponse:
        context = context or {}
        resp = self._complete(system, user, max_tokens, action, context)
        db.log_agent_action(
            {
                "module": module,
                "action": action,
                "provider": resp.provider,
                "input_summary": user[:1500],
                "output_summary": resp.text[:1500],
                "tokens_used": resp.input_tokens + resp.output_tokens,
                "cost_usd": resp.cost_usd,
            }
        )
        return resp


# ===========================================================================
# L3 — Anthropic (Fable 5)
# ===========================================================================

class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        import anthropic  # imported lazily so offline installs still work

        cfg = get_config()
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        self.model = cfg.model

    def _complete(self, system: str, user: str, max_tokens: int,
                  action: str, context: dict[str, Any]) -> LLMResponse:
        allowed, reason = spend_guard.l3_allowed()
        if not allowed:
            raise SpendLimitReached(reason)
        message = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": f"{system}\n\n{ANTI_OVERTHINK}",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
        usage = message.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = spend_guard.compute_cost_usd(usage.input_tokens, usage.output_tokens, cache_read)
        spend_guard.record_spend(cost)
        return LLMResponse(
            text=text,
            provider=self.name,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
        )


class SpendLimitReached(Exception):
    pass


# ===========================================================================
# L2 — deterministic local rules
# ===========================================================================

class LocalRuleProvider(LLMProvider):
    """Tier-2 deterministic engine. Solves what it can without any cloud call:
    the two known bug classes, mechanical stat rebalancing, template GDDs,
    and keyword sentiment. Zero tokens, always available."""

    name = "local_rules"

    def _complete(self, system: str, user: str, max_tokens: int,
                  action: str, context: dict[str, Any]) -> LLMResponse:
        handler = {
            "patch": self._patch,
            "balance": self._balance,
            "gdd": self._gdd,
            "sentiment": self._sentiment,
        }.get(action)
        if handler is None:
            raise ValueError(f"LocalRuleProvider has no handler for action '{action}'")
        return LLMResponse(text=handler(context), provider=self.name)

    # -- patch ----------------------------------------------------------- #
    def _patch(self, ctx: dict[str, Any]) -> str:
        """Deterministically repair a broken function.

        ZeroDivisionError      -> guard the function body with a safe default
        *WatchdogError/loops   -> bound any unbounded while-loops
        """
        code: str = ctx["code"]
        error_type: str = ctx.get("error_type", "")

        if "while True:" in code and ("Watchdog" in error_type or "Timeout" in error_type
                                      or "loop" in ctx.get("message", "").lower()):
            code = code.replace("while True:", "for _cardinal_guard in range(100_000):")
            return code

        if "ZeroDivision" in error_type:
            return _wrap_body_with_guard(code, "ZeroDivisionError", default_return="0")

        # Generic last resort: guard against the reported exception type.
        etype = error_type.split(".")[-1] or "Exception"
        return _wrap_body_with_guard(code, etype, default_return="None")

    # -- balance ---------------------------------------------------------- #
    def _balance(self, ctx: dict[str, Any]) -> str:
        """Nudge flagged items toward 0.50 win rate. The Sub-Process clamps
        deltas to the Taboo limit anyway; we stay inside 8% here by design."""
        items: list[dict[str, Any]] = json.loads(json.dumps(ctx["items"]))  # deep copy
        flagged: dict[str, float] = ctx.get("flagged", {})
        for item in items:
            wr = flagged.get(item["name"])
            if wr is None:
                continue
            factor = 0.92 if wr > 0.5 else 1.08
            for fieldname in ("damage", "crit_chance", "crit_mult"):
                if isinstance(item.get(fieldname), (int, float)) and item[fieldname]:
                    adjusted = item[fieldname] * factor
                    item[fieldname] = round(adjusted, 4) if isinstance(item[fieldname], float) else int(round(adjusted))
        return json.dumps(items, indent=2)

    # -- gdd --------------------------------------------------------------- #
    def _gdd(self, ctx: dict[str, Any]) -> str:
        title = ctx.get("topic_title", "Unknown Legend")
        archetype = ctx.get("archetype", "open_field")
        text = (ctx.get("topic_text") or "")[:200]
        gdd = {
            "title": f"The Trial of {title}",
            "narrative": (
                f"Whispers of {title} have reached the floating castle. "
                f"An anomaly reshapes the land into a {archetype.replace('_', ' ')} trial. "
                "Only those who unravel its origin may claim the reward."
            ),
            "stages": [
                {"stage": 1, "description": f"Investigate the rumors surrounding {title}. {text}",
                 "objective": "Speak with the Chronicler and find the anomaly site."},
                {"stage": 2, "description": "The anomaly's guardians emerge to test challengers.",
                 "objective": "Defeat 3 Anomaly Guardians."},
                {"stage": 3, "description": f"The heart of the {archetype.replace('_', ' ')} reveals itself.",
                 "objective": f"Defeat the Avatar of {title} and seal the anomaly."},
            ],
            "npcs": [
                {"name": "The Chronicler", "role": "quest_giver",
                 "dialogue": f"Traveler... the tale of {title} is older than this castle. Listen well."},
                {"name": "Lost Scout", "role": "informant",
                 "dialogue": "I saw the guardians with my own eyes. Turn back, or arm yourself."},
            ],
            "enemies": [
                {"name": "Anomaly Guardian", "damage": 18, "hp": 120, "reward_gold": 60},
                {"name": f"Avatar of {title}", "damage": 32, "hp": 400, "reward_gold": 250},
            ],
            "rewards": [
                {"item": f"Relic of {title}", "quantity": 1},
                {"item": "Health Potion", "quantity": 3},
            ],
            "map_archetype": archetype,
            "world_changes": [],
        }
        return json.dumps(gdd, indent=2)

    # -- sentiment ----------------------------------------------------------- #
    _POSITIVE = ("love", "great", "amazing", "fun", "awesome", "best", "enjoy", "praise", "good")
    _NEGATIVE = ("hate", "boring", "broken", "worst", "annoying", "unfair", "nerf", "bad", "bored")

    def _sentiment(self, ctx: dict[str, Any]) -> str:
        results = []
        for post in ctx.get("posts", []):
            text = f"{post.get('title', '')} {post.get('text', '')}".lower()
            pos = sum(text.count(w) for w in self._POSITIVE)
            neg = sum(text.count(w) for w in self._NEGATIVE)
            total = pos + neg
            score = 0.0 if total == 0 else (pos - neg) / total
            results.append({"topic": post.get("title", "")[:80], "sentiment_score": round(score, 3)})
        return json.dumps(results)


def _wrap_body_with_guard(code: str, exception_name: str, default_return: str = "None") -> str:
    """Wrap a function's body in try/except <exception_name>, preserving the
    signature. Deterministic text transform used by the L2 patcher."""
    lines = code.splitlines()
    def_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*def\s+\w+", line):
            def_idx = i
            break
    if def_idx is None:
        return code
    # find end of signature (line whose rstrip ends with ':')
    sig_end = def_idx
    while sig_end < len(lines) and not lines[sig_end].rstrip().endswith(":"):
        sig_end += 1
    header = lines[: sig_end + 1]
    body = lines[sig_end + 1:]
    if not body:
        return code
    # indent of the `def` line itself (NOT the last signature line — multi-line
    # signatures have continuation indentation that would corrupt the wrap)
    base_indent_match = re.match(r"^(\s*)", lines[def_idx])
    base_indent = base_indent_match.group(1) if base_indent_match else ""
    inner = base_indent + "    "
    wrapped = [f"{inner}try:"]
    for line in body:
        wrapped.append(("    " + line) if line.strip() else line)
    wrapped.append(f"{inner}except {exception_name}:")
    wrapped.append(f"{inner}    return {default_return}")
    return "\n".join(header + wrapped)


# ===========================================================================
# Selection + one-call-per-event fallback
# ===========================================================================

def get_provider() -> LLMProvider:
    cfg = get_config()
    if cfg.anthropic_api_key:
        return AnthropicProvider()
    if cfg.use_mock:
        from cardinal.llm.mock_provider import MockProvider

        return MockProvider()
    return LocalRuleProvider()


def complete_with_fallback(module: str, action: str, system: str, user: str,
                           max_tokens: int = MAX_TOKENS_DEFAULT,
                           context: dict[str, Any] | None = None) -> LLMResponse:
    """Make at most ONE L3 attempt; on any failure (API error, spend guard,
    bad output) fall back to L2 LocalRuleProvider immediately. Never retries L3."""
    provider = get_provider()
    try:
        return provider.complete(module, action, system, user, max_tokens, context)
    except Exception as err:
        if isinstance(provider, LocalRuleProvider):
            raise  # L2 itself failed — nothing below it to fall back to
        log_event(module, f"L3 call failed ({type(err).__name__}: {err}) — falling back to L2", SEV_WARNING)
        return LocalRuleProvider().complete(module, action, system, user, max_tokens, context)
