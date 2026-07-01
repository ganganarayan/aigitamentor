"""Meta (Facebook) Conversions API — robust server-side events.

Every conversion is sent via CAPI (survives ad-blockers) AND, where a browser
context exists, mirrored by the client Pixel using the **same event_id** so Meta
deduplicates — one count, never a miss. user_data is enriched (hashed email +
phone + external_id, client IP, user-agent, fbp/fbc cookies) for match quality.

Guarded: a no-op unless META_PIXEL_ID and META_CAPI_TOKEN are set. Never raises.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from app.config import settings

logger = logging.getLogger("app.meta")
_GRAPH = "https://graph.facebook.com/v19.0"


def enabled() -> bool:
    return bool(settings.meta_pixel_id and settings.meta_capi_token)


def new_event_id() -> str:
    return uuid.uuid4().hex


def _hash(value) -> str | None:
    v = str(value or "").strip().lower()
    return hashlib.sha256(v.encode("utf-8")).hexdigest() if v else None


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _user_data(request=None, email=None, phone=None, external_id=None) -> dict:
    ud: dict = {}
    if email:
        ud["em"] = [_hash(email)]
    if phone:
        ud["ph"] = [_hash(_digits(phone))]
    if external_id is not None:
        ud["external_id"] = [_hash(external_id)]
    if request is not None:
        xff = request.headers.get("x-forwarded-for")
        ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)
        if ip:
            ud["client_ip_address"] = ip
        ua = request.headers.get("user-agent")
        if ua:
            ud["client_user_agent"] = ua
        fbp = request.cookies.get("_fbp")
        fbc = request.cookies.get("_fbc")
        if fbp:
            ud["fbp"] = fbp
        if fbc:
            ud["fbc"] = fbc
    return ud


def send(
    event_name: str,
    *,
    request=None,
    email=None,
    phone=None,
    external_id=None,
    event_id: str | None = None,
    value: float | None = None,
    currency: str = "INR",
    event_source_url: str | None = None,
) -> None:
    if not enabled():
        return
    import httpx

    event: dict = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": "website",
        "event_id": event_id or new_event_id(),
        "user_data": _user_data(request, email, phone, external_id),
    }
    url = event_source_url or (str(request.url) if request is not None else None)
    if url:
        event["event_source_url"] = url
    if value is not None:
        event["custom_data"] = {"value": round(float(value), 2), "currency": currency}
    payload: dict = {"data": [event]}
    if settings.meta_test_event_code:
        payload["test_event_code"] = settings.meta_test_event_code
    try:
        httpx.post(
            f"{_GRAPH}/{settings.meta_pixel_id}/events",
            params={"access_token": settings.meta_capi_token},
            json=payload,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Meta CAPI %s failed", event_name, exc_info=True)


# --- convenience wrappers ---------------------------------------------------

def track_page_view(request, event_id: str | None = None) -> None:
    send("PageView", request=request, event_id=event_id)


def track_complete_registration(email, *, request=None, phone=None, external_id=None, event_id=None) -> None:
    send("CompleteRegistration", request=request, email=email, phone=phone, external_id=external_id, event_id=event_id)


def track_start_trial(email, *, request=None, phone=None, external_id=None, event_id=None) -> None:
    """Fired on every new signup (free = trial start) and on first subscription."""
    send("StartTrial", request=request, email=email, phone=phone, external_id=external_id, event_id=event_id)


def track_purchase(email, value, *, currency="INR", phone=None, external_id=None, event_id=None) -> None:
    send("Purchase", email=email, value=value, currency=currency, phone=phone, external_id=external_id, event_id=event_id)
