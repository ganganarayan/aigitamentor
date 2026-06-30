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
_GEMINI_MODEL = "gemini-1.5-flash"
_PERPLEXITY_MODEL = "sonar"
_MAX_TOKENS = 700
_TIMEOUT = 40

PROVIDERS = ["claude", "openai", "gemini", "perplexity"]


def _prompt(question_text: str) -> str:
    return (
        "Answer this question about the Bhagavad Gita clearly and helpfully for a "
        "thoughtful modern reader:\n\n" + question_text
    )


def _claude(text: str, api_key: str | None, model: str) -> str:
    if not api_key:
        raise RuntimeError("Anthropic API key not configured")
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": _prompt(text)}],
    )
    return "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")


def _openai(text: str, api_key: str | None, model: str) -> str:
    if not api_key:
        raise RuntimeError("OpenAI API key not configured")
    import openai

    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": _prompt(text)}],
    )
    return resp.choices[0].message.content or ""


def _gemini(text: str, api_key: str | None, model: str) -> str:
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
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _perplexity(text: str, api_key: str | None, model: str) -> str:
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
    return data["choices"][0]["message"]["content"]


def call_provider(
    provider: str, question_text: str, cfg: ai_settings.AiRuntime
) -> tuple[str | None, str | None]:
    """Return (answer, error). Exactly one is non-None. Keys/models come from cfg."""
    try:
        if provider == "claude":
            return _claude(question_text, cfg.key_for("anthropic"), cfg.chat_model), None
        if provider == "openai":
            return _openai(question_text, cfg.key_for("openai"), _OPENAI_MODEL), None
        if provider == "gemini":
            return _gemini(question_text, cfg.key_for("gemini"), _GEMINI_MODEL), None
        if provider == "perplexity":
            return _perplexity(question_text, cfg.key_for("perplexity"), _PERPLEXITY_MODEL), None
        return None, f"Unknown provider: {provider}"
    except Exception as exc:  # noqa: BLE001 — isolate provider failures
        logger.warning("Baseline provider %s failed: %s", provider, exc)
        return None, str(exc)


def generate_baselines(db: Session, question: Question) -> list[LlmBaseline]:
    """(Re)generate baselines for a question across the configured providers."""
    cfg = ai_settings.resolved(db)
    results: list[LlmBaseline] = []
    for provider in cfg.baseline_providers:
        answer, error = call_provider(provider, question.question_text, cfg)
        row = db.execute(
            select(LlmBaseline).where(
                LlmBaseline.question_id == question.id, LlmBaseline.provider == provider
            )
        ).scalar_one_or_none()
        text = answer if answer is not None else f"[error] {error}"
        if row is None:
            row = LlmBaseline(question_id=question.id, provider=provider, answer_text=text)
            db.add(row)
        else:
            row.answer_text = text
        results.append(row)
    db.commit()
    return results


def baselines_for(db: Session, question_id: int) -> list[LlmBaseline]:
    return list(
        db.execute(
            select(LlmBaseline).where(LlmBaseline.question_id == question_id).order_by(LlmBaseline.provider)
        ).scalars()
    )
