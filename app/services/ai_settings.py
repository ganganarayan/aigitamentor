"""Runtime AI configuration — DB-backed, env-var fallback.

Stored as a single ``settings`` row (key='ai_runtime', jsonb value). The admin
sets, at runtime (Settings → AI), without redeploy:
  - model_admin  : model for the recorder / LLM baseline panel
  - model_free   : mentor model for the free tier   (default claude-haiku-4-5)
  - model_paid   : mentor model for paid tiers       (default claude-sonnet-5)
  - embedding_model (locked 1536-dim) · transcribe_model
  - baseline_providers · API keys (per provider)

Resolution precedence for every value: DB override if set, else the env var.

**API keys are encrypted at rest** (Fernet, key derived from JWT_SECRET) and are
never returned to the client or written to logs — the UI shows only set/unset.
Legacy plaintext keys are read transparently and re-encrypted on next save.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as env
from app.models import Setting

logger = logging.getLogger("app.ai_settings")

SETTINGS_KEY = "ai_runtime"
KEY_PROVIDERS = ["anthropic", "openai", "gemini", "perplexity"]
DEFAULT_BASELINE_PROVIDERS = ["claude", "openai", "gemini", "perplexity"]
_ENC_PREFIX = "enc:v1:"


# --- key encryption ---------------------------------------------------------

def _cipher() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(env.jwt_secret.encode("utf-8")).digest())
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _ENC_PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(_ENC_PREFIX):
        try:
            return _cipher().decrypt(value[len(_ENC_PREFIX):].encode("utf-8")).decode("utf-8")
        except InvalidToken:
            logger.warning("Stored API key failed to decrypt (JWT_SECRET changed?).")
            return None
    return value  # legacy plaintext — transparently readable, re-encrypted on next save


@dataclass
class AiRuntime:
    model_admin: str
    model_free: str
    model_paid: str
    embedding_model: str
    transcribe_model: str
    baseline_providers: list[str]
    keys: dict  # provider -> resolved (decrypted) key or None

    def key_for(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def chat_model_for_tier(self, tier: str) -> str:
        return self.model_paid if tier in ("abhyasi", "sadhaka") else self.model_free


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
    raw = load_raw(db)
    db_keys = raw.get("keys") or {}
    keys = {p: (_decrypt(db_keys.get(p)) or _env_key(p)) for p in KEY_PROVIDERS}
    return AiRuntime(
        model_admin=raw.get("model_admin") or env.chat_model,
        model_free=raw.get("model_free") or env.chat_model_free,
        model_paid=raw.get("model_paid") or env.chat_model_paid,
        embedding_model=raw.get("embedding_model") or env.embedding_model,
        transcribe_model=raw.get("transcribe_model") or env.transcribe_model,
        baseline_providers=raw.get("baseline_providers") or list(DEFAULT_BASELINE_PROVIDERS),
        keys=keys,
    )


def view(db: Session) -> dict:
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
        "model_admin": r.model_admin,
        "model_free": r.model_free,
        "model_paid": r.model_paid,
        "embedding_model": r.embedding_model,
        "transcribe_model": r.transcribe_model,
        "baseline_providers": r.baseline_providers,
        "key_status": key_status,
        "key_providers": KEY_PROVIDERS,
        "baseline_choices": DEFAULT_BASELINE_PROVIDERS,
    }


def validate_chat_models(db: Session, model_ids: list[str]) -> str | None:
    """Return an error string if any claude model id is unknown to the Models API.

    Only validates when an Anthropic key is available; on any API error we allow
    the save (can't validate ≠ invalid)."""
    ids = [m for m in model_ids if m and m.startswith("claude")]
    if not ids:
        return None
    key = resolved(db).key_for("anthropic")
    if not key:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        available = {m.id for m in client.models.list().data}
    except Exception:  # noqa: BLE001
        return None
    bad = sorted({m for m in ids if m not in available})
    if bad:
        return "Unknown model id(s): " + ", ".join(bad) + ". Check Settings → AI."
    return None


def update(
    db: Session,
    *,
    model_admin: str,
    model_free: str,
    model_paid: str,
    embedding_model: str,
    transcribe_model: str,
    baseline_providers: list[str],
    key_updates: dict[str, str],
    key_clears: list[str],
) -> None:
    raw = load_raw(db)
    raw["model_admin"] = model_admin or env.chat_model
    raw["model_free"] = model_free or env.chat_model_free
    raw["model_paid"] = model_paid or env.chat_model_paid
    raw["embedding_model"] = embedding_model or env.embedding_model
    raw["transcribe_model"] = transcribe_model or env.transcribe_model
    raw["baseline_providers"] = [p for p in baseline_providers if p in DEFAULT_BASELINE_PROVIDERS] or list(
        DEFAULT_BASELINE_PROVIDERS
    )
    keys = dict(raw.get("keys") or {})
    for provider, value in key_updates.items():
        if value:
            keys[provider] = _encrypt(value)  # encrypt at rest
    for provider in key_clears:
        keys.pop(provider, None)
    raw["keys"] = keys
    save_raw(db, raw)
