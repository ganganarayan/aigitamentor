"""DB-backed integration settings — the rest of the config, editable in Admin.

Every field here changes in the admin UI without a Railway visit. Secrets are
encrypted at rest (secretbox), never returned to the browser (only "set / not
set"), and never logged. Precedence: a DB value wins if set, else the Railway env
var — so a fresh deploy with an empty table behaves exactly as before. Reads go
through settings_cache (never the DB per request).

Kept in env on purpose (infra / bootstrap): DATABASE_URL, SETTINGS_ENCRYPTION_KEY,
JWT_SECRET, APP_BASE_URL/APP_URL (wired into host-routing at import), ADMIN_EMAILS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings as env
from app.models import Event, Setting
from app.services import secretbox, settings_cache

logger = logging.getLogger("app.settings_store")

_STORE_KEY = "app_config"


@dataclass(frozen=True)
class Field:
    name: str
    section: str
    label: str
    secret: bool = False
    env_attr: str | None = None  # attribute on config.settings for the env fallback
    placeholder: str = ""
    help: str = ""


FIELDS: list[Field] = [
    # Google OAuth (sign-in)
    Field("google_oauth_client_id", "Google OAuth", "Client ID", env_attr="google_oauth_client_id"),
    Field("google_oauth_client_secret", "Google OAuth", "Client secret", secret=True, env_attr="google_oauth_client_secret"),
    Field("google_oauth_redirect_uri", "Google OAuth", "Redirect URI", env_attr="google_oauth_redirect_uri",
          placeholder="https://ai.applygitawisdom.com/auth/google/callback"),
    # Google Drive (audio archive)
    Field("google_drive_client_id", "Google Drive", "Client ID", env_attr="google_drive_client_id"),
    Field("google_drive_client_secret", "Google Drive", "Client secret", secret=True, env_attr="google_drive_client_secret"),
    Field("google_drive_refresh_token", "Google Drive", "Refresh token", secret=True, env_attr="google_drive_refresh_token"),
    Field("drive_recordings_folder_id", "Google Drive", "Recordings folder ID", env_attr="drive_recordings_folder_id"),
    # Payments (Razorpay)
    Field("razorpay_key_id", "Payments (Razorpay)", "Key ID", env_attr="razorpay_key_id"),
    Field("razorpay_key_secret", "Payments (Razorpay)", "Key secret", secret=True, env_attr="razorpay_key_secret"),
    Field("razorpay_webhook_secret", "Payments (Razorpay)", "Webhook secret", secret=True, env_attr="razorpay_webhook_secret"),
    Field("razorpay_plan_abhyasi", "Payments (Razorpay)", "Abhyāsi plan id (optional)",
          env_attr="razorpay_plan_abhyasi", placeholder="leave blank — auto-created",
          help="Optional. Leave blank and the app creates the ₹499 monthly plan via the API on first "
               "subscribe. Only set a plan_… id to force a specific plan."),
    Field("razorpay_plan_sadhaka", "Payments (Razorpay)", "Sādhaka plan id (optional)",
          env_attr="razorpay_plan_sadhaka", placeholder="leave blank — auto-created",
          help="Optional. Leave blank and the app creates the ₹1,459 monthly plan via the API on first "
               "subscribe. Only set a plan_… id to force a specific plan."),
    # Meta Pixel / CAPI
    Field("meta_pixel_id", "Meta Pixel / CAPI", "Pixel ID", env_attr="meta_pixel_id"),
    Field("meta_capi_token", "Meta Pixel / CAPI", "CAPI token", secret=True, env_attr="meta_capi_token"),
    Field("meta_test_event_code", "Meta Pixel / CAPI", "Test event code", env_attr="meta_test_event_code",
          help="Only while testing in Events Manager → Test Events."),
    # Video / CDN signing (Sādhaka gated video; no env fallback yet)
    Field("cloudflare_account_id", "Video / CDN signing", "Cloudflare account ID"),
    Field("cloudflare_stream_signing_key", "Video / CDN signing", "Stream signing key", secret=True),
]

_BY_NAME: dict[str, Field] = {f.name: f for f in FIELDS}


def _raw(db: Session | None = None) -> dict:
    return settings_cache.get(_STORE_KEY, db)


# --- "is this integration configured?" predicates (DB-over-env via get) -------

def meta_enabled(db: Session | None = None) -> bool:
    return bool(get("meta_pixel_id", db) and get("meta_capi_token", db))


def razorpay_enabled(db: Session | None = None) -> bool:
    return bool(get("razorpay_key_id", db) and get("razorpay_key_secret", db))


def google_oauth_enabled(db: Session | None = None) -> bool:
    return bool(get("google_oauth_client_id", db) and get("google_oauth_client_secret", db))


def google_drive_configured(db: Session | None = None) -> bool:
    return bool(
        get("google_drive_client_id", db)
        and get("google_drive_client_secret", db)
        and get("google_drive_refresh_token", db)
    )


def get(name: str, db: Session | None = None) -> str | None:
    """Resolved value: DB (decrypted if secret) if set, else the env fallback."""
    f = _BY_NAME.get(name)
    if f is None:
        return None
    stored = _raw(db).get(name)
    if stored:
        return secretbox.decrypt(stored) if f.secret else stored
    return getattr(env, f.env_attr, None) if f.env_attr else None


def is_set(name: str, db: Session | None = None) -> bool:
    return bool(get(name, db))


def source(name: str, db: Session | None = None) -> str:
    """database | environment | none."""
    f = _BY_NAME.get(name)
    if f is None:
        return "none"
    if _raw(db).get(name):
        return "database"
    if f.env_attr and getattr(env, f.env_attr, None):
        return "environment"
    return "none"


def sections(db: Session | None = None) -> list[dict]:
    """Grouped view for the admin form. Secret values are never included — only
    their source/set state. Non-secrets carry their plain value for inline edit."""
    out: list[dict] = []
    by_section: dict[str, list[dict]] = {}
    for f in FIELDS:
        src = source(f.name, db)
        row = {
            "name": f.name, "label": f.label, "secret": f.secret,
            "placeholder": f.placeholder, "help": f.help, "source": src,
            "is_set": src != "none",
            "value": ("" if f.secret else (get(f.name, db) or "")),
        }
        by_section.setdefault(f.section, []).append(row)
    for section, rows in by_section.items():
        out.append({"section": section, "fields": rows})
    return out


def _write(db: Session, data: dict) -> None:
    row = db.execute(select(Setting).where(Setting.key == _STORE_KEY)).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=_STORE_KEY, value=data))
    else:
        row.value = data
    db.commit()
    settings_cache.invalidate(_STORE_KEY)


def audit(db: Session, actor_id: int | None, actor_email: str, names: list[str], action: str) -> None:
    """Record WHICH setting changed, by whom, when — never the value."""
    if not names:
        return
    try:
        for name in names:
            db.add(Event(
                user_id=actor_id, event_name="settings_changed",
                properties={"setting": name, "action": action, "actor": actor_email},
            ))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def save(db: Session, updates: dict[str, str], clears: list[str], *, actor_id=None, actor_email="") -> None:
    """Apply changes. For secrets: a blank value keeps the current one; a value
    replaces it (encrypted); ticking clear removes the DB value (falls back to env).
    For non-secrets: the submitted value is stored (blank removes it)."""
    raw = dict(_raw(db))
    changed: list[str] = []
    for name, val in (updates or {}).items():
        f = _BY_NAME.get(name)
        if f is None:
            continue
        val = (val or "").strip()
        if f.secret:
            if val:  # only touch a secret when a new value is pasted
                raw[name] = secretbox.encrypt(val)
                changed.append(name)
        else:
            if val:
                if raw.get(name) != val:
                    raw[name] = val
                    changed.append(name)
            elif name in raw:
                raw.pop(name, None)
                changed.append(name)
    for name in clears or []:
        if name in raw:
            raw.pop(name, None)
            changed.append(name)
    _write(db, raw)
    audit(db, actor_id, actor_email, changed, "update")


def recent_audit(db: Session, limit: int = 40) -> list[Event]:
    return list(
        db.execute(
            select(Event).where(Event.event_name == "settings_changed").order_by(Event.id.desc()).limit(limit)
        ).scalars()
    )
