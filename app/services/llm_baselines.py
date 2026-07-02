"""Multi-LLM Baseline Panel (Section 7) — the differentiation engine.

For a question we ask the configured frontier models what they say (the
"commodity floor"), store each in ``llm_baselines``, and show them side by side
so GND answers *beyond* them. Every provider call is isolated: a missing key or
an API error degrades to a stored error string, never a crash.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LlmBaseline, Question
from app.services import ai_settings

logger = logging.getLogger("app.baselines")

# Commodity-floor models. Exact ids are not load-bearing — the panel just needs
# to reflect the current frontier. Adjust freely as models evolve.
_OPENAI_MODEL = "gpt-4o-mini"
_GEMINI_MODEL = "gemini-2.5-flash"
_PERPLEXITY_MODEL = "sonar"
_MAX_TOKENS = 700
_TIMEOUT = 40

PROVIDERS = ["claude", "openai", "gemini", "perplexity"]


def _prompt(question_text: str) -> str:
    return (
        "Answer this question about the Bhagavad Gita clearly and helpfully for a "
        "thoughtful modern reader:\n\n" + question_text
    )


def _claude(text: str, api_key: str | None, model: str) -> tuple[str, int, int]:
    if not api_key:
        raise RuntimeError("Anthropic API key not configured")
    from app.services import anthropic_client

    client = anthropic_client.make(api_key, retries=anthropic_client.BG_RETRIES)
    msg = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": _prompt(text)}],
    )
    out = "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
    u = getattr(msg, "usage", None)
    return out, (getattr(u, "input_tokens", 0) or 0), (getattr(u, "output_tokens", 0) or 0)


def _openai(text: str, api_key: str | None, model: str) -> tuple[str, int, int]:
    if not api_key:
        raise RuntimeError("OpenAI API key not configured")
    import openai

    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": _prompt(text)}],
    )
    u = getattr(resp, "usage", None)
    return (resp.choices[0].message.content or ""), (getattr(u, "prompt_tokens", 0) or 0), (getattr(u, "completion_tokens", 0) or 0)


def _gemini(text: str, api_key: str | None, model: str) -> tuple[str, int, int]:
    if not api_key:
        raise RuntimeError("Gemini API key not configured")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.post(url, json={"contents": [{"parts": [{"text": _prompt(text)}]}]})
        r.raise_for_status()
        data = r.json()
    um = data.get("usageMetadata") or {}
    return (
        data["candidates"][0]["content"]["parts"][0]["text"],
        (um.get("promptTokenCount") or 0),
        (um.get("candidatesTokenCount") or 0),
    )


def _perplexity(text: str, api_key: str | None, model: str) -> tuple[str, int, int]:
    if not api_key:
        raise RuntimeError("Perplexity API key not configured")
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": _prompt(text)}]},
        )
        r.raise_for_status()
        data = r.json()
    u = data.get("usage") or {}
    return (
        data["choices"][0]["message"]["content"],
        (u.get("prompt_tokens") or 0),
        (u.get("completion_tokens") or 0),
    )


def call_provider(
    provider: str, question_text: str, cfg: ai_settings.AiRuntime
) -> tuple[str | None, str | None, int, int, str | None]:
    """Return (answer, error, tokens_in, tokens_out, model). Keys/models from cfg."""
    bm = cfg.baseline_models or {}
    model = {
        "claude": bm.get("claude") or cfg.model_admin,
        "openai": bm.get("openai") or _OPENAI_MODEL,
        "gemini": bm.get("gemini") or _GEMINI_MODEL,
        "perplexity": bm.get("perplexity") or _PERPLEXITY_MODEL,
    }.get(provider)
    if model is None:
        return None, f"Unknown provider: {provider}", 0, 0, None
    key_provider = "anthropic" if provider == "claude" else provider
    fn = {"claude": _claude, "openai": _openai, "gemini": _gemini, "perplexity": _perplexity}[provider]
    try:
        answer, tin, tout = fn(question_text, cfg.key_for(key_provider), model)
        return answer, None, tin, tout, model
    except Exception as exc:  # noqa: BLE001 — isolate provider failures
        logger.warning("Baseline provider %s failed: %s", provider, exc)
        return None, str(exc), 0, 0, model


def generate_baselines(db: Session, question: Question) -> list[LlmBaseline]:
    """(Re)generate baselines across providers, recording each call's token cost."""
    from app.services import pricing, site_settings

    cfg = ai_settings.resolved(db)
    usd_inr = site_settings.get_accounting(db)["usd_inr"]
    results: list[LlmBaseline] = []
    for provider in cfg.baseline_providers:
        answer, error, tin, tout, model = call_provider(provider, question.question_text, cfg)
        cost = pricing.estimate_inr(model, tin, tout, usd_inr) if answer is not None else 0.0
        row = db.execute(
            select(LlmBaseline).where(
                LlmBaseline.question_id == question.id, LlmBaseline.provider == provider
            )
        ).scalar_one_or_none()
        text = answer if answer is not None else f"[error] {error}"
        if row is None:
            row = LlmBaseline(
                question_id=question.id, provider=provider, answer_text=text,
                tokens_in=tin, tokens_out=tout, cost_inr=cost,
            )
            db.add(row)
        else:
            row.answer_text = text
            row.tokens_in = tin
            row.tokens_out = tout
            row.cost_inr = cost
        results.append(row)
    db.commit()
    return results


def baselines_for(db: Session, question_id: int) -> list[LlmBaseline]:
    return list(
        db.execute(
            select(LlmBaseline).where(LlmBaseline.question_id == question_id).order_by(LlmBaseline.provider)
        ).scalars()
    )
