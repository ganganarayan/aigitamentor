"""One place to build Anthropic clients and classify failures (Section: resilience).

The SDK already retries the right way — it honours the ``retry-after`` header on
429 ``rate_limit_error`` and uses exponential backoff + jitter on 5xx / 529
``overloaded_error``. We just raise ``max_retries`` and set a timeout so a burst
or an Anthropic-side overload is ridden out instead of surfacing to the user, and
we expose ``is_transient`` so callers show "the mentor is catching up" for a
capacity blip rather than the "not configured" message meant for a missing key.

Cached-read tokens (prompt caching, used on the chat system prompt) don't count
against the input-tokens-per-minute limit, so caching already lifts throughput.

The API key must always come from the cached settings accessor (ai_settings), not
os.environ, so a key rotated in Admin → Settings takes effect on the next call.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("app.anthropic")

# Chat is user-facing → ride out more blips. Background/admin calls retry less.
CHAT_RETRIES = 4
BG_RETRIES = 3
TIMEOUT_SECONDS = 60.0


def make(api_key: str | None, *, retries: int = CHAT_RETRIES, timeout: float = TIMEOUT_SECONDS):
    """Build an Anthropic client with resilient retry/timeout defaults."""
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=retries, timeout=timeout)


def is_transient(exc: BaseException) -> bool:
    """True for a rate-limit (429), overload (529), other 5xx, timeout, or
    connection error — i.e. 'catching up', not a configuration/auth problem."""
    try:
        import anthropic
    except Exception:  # noqa: BLE001
        return False
    if isinstance(
        exc,
        (
            anthropic.RateLimitError,      # 429
            anthropic.InternalServerError, # >=500, includes 529 overloaded_error
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ),
    ):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (429, 500, 502, 503, 529)
