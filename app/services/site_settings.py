"""DB-backed site settings the admin edits at runtime (no redeploy).

Values live in the generic ``settings`` key/value table; the env/config values are
only fallback defaults. Currently holds the Chunk 5 escalation config (the 1-on-1
booking link, the assessment link, the video-link lifetime, the assessment-fresh
window). Reads never raise — a cold/missing row falls back to code defaults.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Setting

logger = logging.getLogger("app.site_settings")

_ESCALATION_KEY = "escalation"


def _get(db: Session, key: str) -> dict:
    try:
        row = db.execute(select(Setting).where(Setting.key == key)).scalars().first()
        if row and isinstance(row.value, dict):
            return row.value
    except Exception:  # noqa: BLE001
        logger.warning("site_settings read failed for %s", key, exc_info=True)
    return {}


def _put(db: Session, key: str, value: dict) -> None:
    row = db.execute(select(Setting).where(Setting.key == key)).scalars().first()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _int(val, default: int) -> int:
    try:
        n = int(val)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def get_escalation(db: Session) -> dict:
    """Merged escalation config: DB value over code/env defaults. Always complete."""
    data = _get(db, _ESCALATION_KEY)
    return {
        "booking_url": (data.get("booking_url") or settings.oneonone_booking_url or "").strip(),
        "assessment_url": (data.get("assessment_url") or settings.assessment_url or "").strip(),
        "ttl_hours": _int(data.get("ttl_hours"), settings.resource_link_ttl_hours or 24),
        "fresh_days": _int(data.get("fresh_days"), settings.assessment_fresh_days or 15),
    }


def save_escalation(db: Session, *, booking_url: str, assessment_url: str, ttl_hours, fresh_days) -> None:
    _put(
        db,
        _ESCALATION_KEY,
        {
            "booking_url": (booking_url or "").strip(),
            "assessment_url": (assessment_url or "").strip(),
            "ttl_hours": _int(ttl_hours, settings.resource_link_ttl_hours or 24),
            "fresh_days": _int(fresh_days, settings.assessment_fresh_days or 15),
        },
    )
