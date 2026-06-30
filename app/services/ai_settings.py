"""Runtime AI configuration — DB-backed, env-var fallback.

Stored as a single ``settings`` row (key='ai_runtime', jsonb value) so the admin
can select the chat model/provider, the embedding/transcribe models, which
providers feed the baseline panel, and paste API keys — all without redeploying
or touching env vars.

Resolution precedence for every value: **DB override if set, else the env var.**
That keeps existing env config working while making everything UI-selectable.

API keys are stored in your database (jsonb). The admin UI never echoes a key
back — it only shows whether one is set and where it came from.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as env
from app.models import Setting

SETTINGS_KEY = "ai_runtime"

# Providers we hold API keys for.
KEY_PROVIDERS = ["anthropic", "openai", "gemini", "perplexity"]
# Providers selectable for the chat mentor + which key each uses.
CHAT_PROVIDERS = ["claude", "openai", "gemini", "perplexity"]
CHAT_PROVIDER_KEY = {"claude": "anthropic", "openai": "openai", "gemini": "gemini", "perplexity": "perplexity"}
# Providers shown in the multi-LLM baseline panel by default.
DEFAULT_BASELINE_PROVIDERS = ["claude", "openai", "gemini", "perplexity"]


@dataclass
class AiRuntime:
    chat_provider: str
    chat_model: str
    embedding_model: str
    transcribe_model: str
    baseline_providers: list[str]
    keys: dict  # provider -> resolved key (may be None)

    def key_for(self, provider: str) -> str | None:
        return self.keys.get(provider)

    @property
    def chat_key(self) -> str | None:
        return self.keys.get(CHAT_PROVIDER_KEY.get(self.chat_provider, "anthropic"))


def _env_key(provider: str) -> str | None:
    return getattr(env, f"{provider}_api_key", None)


def load_raw(db: Session) -> dict:
    row = db.execute(select(Setting).where(Setting.key == SETTINGS_KEY)).scalar_one_or_none()
    return dict(row.value) if row and row.value else {}


def save_raw(db: Session, data: dict) -> None:
    row = db.execute(select(Setting).where(Setting.key == SETTINGS_KEY)).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=SETTINGS_KEY, value=data))
    else:
        row.value = data
    db.commit()


def resolved(db: Session) -> AiRuntime:
    """The effective AI config: DB overrides over env-var fallbacks."""
    raw = load_raw(db)
    db_keys = raw.get("keys") or {}
    keys = {p: (db_keys.get(p) or _env_key(p)) for p in KEY_PROVIDERS}
    return AiRuntime(
        chat_provider=raw.get("chat_provider") or "claude",
        chat_model=raw.get("chat_model") or env.chat_model,
        embedding_model=raw.get("embedding_model") or env.embedding_model,
        transcribe_model=raw.get("transcribe_model") or env.transcribe_model,
        baseline_providers=raw.get("baseline_providers") or list(DEFAULT_BASELINE_PROVIDERS),
        keys=keys,
    )


def view(db: Session) -> dict:
    """Render-friendly snapshot for the admin form (keys masked)."""
    raw = load_raw(db)
    r = resolved(db)
    db_keys = raw.get("keys") or {}
    key_status = {}
    for p in KEY_PROVIDERS:
        db_set = bool(db_keys.get(p))
        env_set = bool(_env_key(p))
        key_status[p] = {
            "db_set": db_set,
            "env_set": env_set,
            "source": "database" if db_set else ("environment" if env_set else "none"),
        }
    return {
        "chat_provider": r.chat_provider,
        "chat_model": r.chat_model,
        "embedding_model": r.embedding_model,
        "transcribe_model": r.transcribe_model,
        "baseline_providers": r.baseline_providers,
        "key_status": key_status,
        "key_providers": KEY_PROVIDERS,
        "chat_providers": CHAT_PROVIDERS,
    }


def update(
    db: Session,
    *,
    chat_provider: str,
    chat_model: str,
    embedding_model: str,
    transcribe_model: str,
    baseline_providers: list[str],
    key_updates: dict[str, str],
    key_clears: list[str],
) -> None:
    raw = load_raw(db)
    if chat_provider in CHAT_PROVIDERS:
        raw["chat_provider"] = chat_provider
    raw["chat_model"] = chat_model or env.chat_model
    raw["embedding_model"] = embedding_model or env.embedding_model
    raw["transcribe_model"] = transcribe_model or env.transcribe_model
    raw["baseline_providers"] = [p for p in baseline_providers if p in CHAT_PROVIDERS] or list(
        DEFAULT_BASELINE_PROVIDERS
    )
    keys = dict(raw.get("keys") or {})
    for provider, value in key_updates.items():
        if value:  # only overwrite when a new key was actually typed
            keys[provider] = value
    for provider in key_clears:
        keys.pop(provider, None)  # revert to env-var fallback
    raw["keys"] = keys
    save_raw(db, raw)
