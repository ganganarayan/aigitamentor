"""Meta (Facebook) Conversions API — server-side events.

CompleteRegistration is fired client-side by the Pixel; StartTrial is sent
server-side here on the first subscription. Guarded: a no-op unless both
META_PIXEL_ID and META_CAPI_TOKEN are set. Emails are SHA-256 hashed per Meta's
requirement. Never raises.
"""

from __future__ import annotations

import hashlib
import logging
import time

from app.config import settings

logger = logging.getLogger("app.meta")


def _hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _send(event_name: str, email: str) -> None:
    if not (settings.meta_pixel_id and settings.meta_capi_token) or not email:
        return
    import httpx

    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(time.time()),
                "action_source": "website",
                "user_data": {"em": [_hash(email)]},
            }
        ]
    }
    try:
        httpx.post(
            f"https://graph.facebook.com/v19.0/{settings.meta_pixel_id}/events",
            params={"access_token": settings.meta_capi_token},
            json=payload,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Meta CAPI %s failed", event_name, exc_info=True)


def track_start_trial(email: str) -> None:
    """Fired on every new signup (free tier = start of the trial) and on the
    first paid subscription — a robust server-side conversion signal."""
    _send("StartTrial", email)


def track_complete_registration(email: str) -> None:
    _send("CompleteRegistration", email)
