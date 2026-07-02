"""LLM cost estimation for the accounting ledger.

Public list prices per 1M tokens (USD), input/output. These change over time — they
are ESTIMATES for internal bookkeeping; edit here to refine. Converted to INR with a
rate set on the Accounting page. Matched by longest model-id prefix so dated
snapshots (e.g. claude-haiku-4-5-20251001) resolve to their family price.
"""

from __future__ import annotations

# model-id prefix -> (input_usd_per_1M, output_usd_per_1M)
_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (0.80, 4.0),
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "o1": (15.0, 60.0),
    "o3-mini": (1.10, 4.40),
    # Google Gemini
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash-lite": (0.075, 0.30),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini-1.5-flash": (0.075, 0.30),
    # Perplexity Sonar
    "sonar-reasoning-pro": (2.0, 8.0),
    "sonar-reasoning": (1.0, 5.0),
    "sonar-deep-research": (2.0, 8.0),
    "sonar-pro": (3.0, 15.0),
    "sonar": (1.0, 1.0),
    # OpenAI embeddings (input only)
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}
_DEFAULT_PRICE = (2.0, 8.0)   # unknown model — a middle-of-the-road estimate
DEFAULT_USD_INR = 84.0


def _price_for(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    best_key = ""
    for key in _PRICES:
        if m.startswith(key) and len(key) > len(best_key):
            best_key = key
    return _PRICES[best_key] if best_key else _DEFAULT_PRICE


def estimate_usd(model: str | None, tokens_in: int | None, tokens_out: int | None) -> float:
    pin, pout = _price_for(model)
    return (tokens_in or 0) / 1_000_000 * pin + (tokens_out or 0) / 1_000_000 * pout


def estimate_inr(
    model: str | None, tokens_in: int | None, tokens_out: int | None, usd_inr: float = DEFAULT_USD_INR
) -> float:
    return round(estimate_usd(model, tokens_in, tokens_out) * (usd_inr or DEFAULT_USD_INR), 4)
