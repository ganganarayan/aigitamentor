"""Runtime AI configuration — DB-backed, env-var fallback.

Stored as a single ``settings`` row (key='ai_runtime', jsonb value). The admin
sets, at runtime (Settings → AI), without redeploy:
  - provider     : which provider the model pickers list from (default anthropic)
  - model_admin  : model for the recorder / LLM baseline panel
  - model_seeker : mentor model for the free tier   (default claude-haiku-4-5)
  - model_abhyasi: mentor model for Abhyāsi          (default claude-sonnet-5)
  - model_sadhaka: mentor model for Sādhaka          (default claude-sonnet-5)
  - embedding_model (locked 1536-dim) · transcribe_model
  - baseline_providers · API keys (per provider)

Resolution precedence for every value: DB override if set, else the env var.

**API keys are encrypted at rest** (Fernet, key derived from JWT_SECRET) and are
never returned to the client or written to logs. Legacy plaintext keys are read
transparently and re-encrypted on next save.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

import httpx
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
    return value  # legacy plaintext — readable, re-encrypted on next save


@dataclass
class AiRuntime:
    provider: str
    model_admin: str
    model_seeker: str
    model_abhyasi: str
    model_sadhaka: str
    embedding_model: str
    transcribe_model: str
    baseline_providers: list[str]
    keys: dict

    def key_for(self, provider: str) -> str | None:
        return self.keys.get(provider)

    def chat_model_for_tier(self, tier: str) -> str:
        return {
            "seeker": self.model_seeker,
            "abhyasi": self.model_abhyasi,
            "sadhaka": self.model_sadhaka,
        }.get(tier, self.model_seeker)


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
        provider=raw.get("provider") or "anthropic",
        model_admin=raw.get("model_admin") or env.chat_model,
        model_seeker=raw.get("model_seeker") or env.chat_model_free,
        model_abhyasi=raw.get("model_abhyasi") or env.chat_model_paid,
        model_sadhaka=raw.get("model_sadhaka") or env.chat_model_paid,
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
        "provider": r.provider,
        "model_admin": r.model_admin,
        "model_seeker": r.model_seeker,
        "model_abhyasi": r.model_abhyasi,
        "model_sadhaka": r.model_sadhaka,
        "embedding_model": r.embedding_model,
        "transcribe_model": r.transcribe_model,
        "baseline_providers": r.baseline_providers,
        "key_status": key_status,
        "key_providers": KEY_PROVIDERS,
        "baseline_choices": DEFAULT_BASELINE_PROVIDERS,
    }


# Public, curated fallbacks. Gemini is pulled live when a key is present; these
# are the current public ids used when the key isn't set. Perplexity has no
# list-models endpoint, so its (fixed, public) Sonar ids are curated here.
# Refreshed 2026-07-02 from ai.google.dev and docs.perplexity.ai.
_GEMINI_FALLBACK = [
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
    "gemini-2.0-flash", "gemini-2.0-flash-lite",
]
_PERPLEXITY_MODELS = ["sonar", "sonar-pro", "sonar-reasoning-pro", "sonar-deep-research"]


def _gemini_models(key: str | None) -> list[str]:
    """Live Gemini models (generateContent-capable) via the public REST endpoint;
    curated public fallback when there's no key or the call fails."""
    if key:
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": key, "pageSize": 1000},
                )
                r.raise_for_status()
                data = r.json()
            ids = sorted({
                (m.get("name") or "").split("/")[-1]
                for m in data.get("models", [])
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
                and (m.get("name") or "").split("/")[-1]
            })
            if ids:
                return ids
        except Exception:  # noqa: BLE001
            logger.warning("Gemini model list failed", exc_info=True)
    return list(_GEMINI_FALLBACK)


def list_provider_models(db: Session, provider: str) -> list[str]:
    """List model ids for a provider to populate the pickers.

    anthropic/openai: live from the provider (needs the key). gemini: live via
    its public REST endpoint, else a curated public fallback. perplexity: the
    fixed, public Sonar set (no list endpoint). Empty only for anthropic/openai
    without a key."""
    cfg = resolved(db)
    try:
        if provider == "anthropic":
            key = cfg.key_for("anthropic")
            if not key:
                return []
            import anthropic

            client = anthropic.Anthropic(api_key=key)
            return sorted(m.id for m in client.models.list().data)
        if provider == "openai":
            key = cfg.key_for("openai")
            if not key:
                return []
            import openai

            client = openai.OpenAI(api_key=key)
            ids = [m.id for m in client.models.list().data]
            return sorted(i for i in ids if i.startswith(("gpt", "o1", "o3", "chatgpt")))
        if provider == "gemini":
            return _gemini_models(cfg.key_for("gemini"))
        if provider == "perplexity":
            return list(_PERPLEXITY_MODELS)
    except Exception:  # noqa: BLE001
        logger.warning("Model list failed for %s", provider, exc_info=True)
    return []


def validate_chat_models(db: Session, model_ids: list[str]) -> str | None:
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
    provider: str,
    model_admin: str,
    model_seeker: str,
    model_abhyasi: str,
    model_sadhaka: str,
    embedding_model: str,
    transcribe_model: str,
    baseline_providers: list[str],
    key_updates: dict[str, str],
    key_clears: list[str],
) -> None:
    raw = load_raw(db)
    raw["provider"] = provider or "anthropic"
    raw["model_admin"] = model_admin or env.chat_model
    raw["model_seeker"] = model_seeker or env.chat_model_free
    raw["model_abhyasi"] = model_abhyasi or env.chat_model_paid
    raw["model_sadhaka"] = model_sadhaka or env.chat_model_paid
    raw["embedding_model"] = embedding_model or env.embedding_model
    raw["transcribe_model"] = transcribe_model or env.transcribe_model
    raw["baseline_providers"] = [p for p in baseline_providers if p in DEFAULT_BASELINE_PROVIDERS] or list(
        DEFAULT_BASELINE_PROVIDERS
    )
    keys = dict(raw.get("keys") or {})
    for prov, value in key_updates.items():
        if value:
            keys[prov] = _encrypt(value)
    for prov in key_clears:
        keys.pop(prov, None)
    raw["keys"] = keys
    save_raw(db, raw)
