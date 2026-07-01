"""Crisis-signal detection → Safety Logs (Section 14).

Keyword screen on user messages. On a hit we log an ``events`` row for admin
review; we do NOT block — the system prompt handles the reply with care and the
crisis-first override. Never raises.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import Event

logger = logging.getLogger("app.safety")

_CRISIS = (
    "suicide", "suicidal", "kill myself", "killing myself", "end my life", "end it all",
    "want to die", "wanna die", "no reason to live", "better off dead", "take my life",
    "self-harm", "self harm", "hurt myself", "harming myself", "cut myself", "cutting myself",
    "overdose", "kill me", "i can't go on", "cant go on",
)


def check(text: str) -> bool:
    t = (text or "").lower()
    return any(kw in t for kw in _CRISIS)


def maybe_flag(db: Session, user_id: int, conversation_id: int, message: str) -> bool:
    try:
        if check(message):
            db.add(
                Event(
                    user_id=user_id,
                    event_name="safety_flag",
                    properties={"conversation_id": conversation_id, "snippet": message[:280]},
                )
            )
            db.commit()
            return True
    except Exception:  # noqa: BLE001
        logger.warning("safety flag logging failed", exc_info=True)
    return False
