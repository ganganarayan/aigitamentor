"""Razorpay subscription billing (Section 3 / 13).

Hosted checkout: we create a Razorpay Subscription for the chosen plan and send
the user to its ``short_url``. Razorpay handles the card mandate and recurring
charges; the **webhook** is the source of truth that flips the user's tier. The
app never stores or accepts card data.

All calls are key-guarded — without Razorpay configured, the upgrade page shows
a friendly "not available yet" instead of crashing.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Payment, Subscription, User

logger = logging.getLogger("app.billing")

# Display prices (INR/month). The authoritative amount lives in the Razorpay plan.
TIER_PRICE = {"abhyasi": 499, "sadhaka": 1459}
PAID_TIERS = ("abhyasi", "sadhaka")


def _plan_id(tier: str) -> str | None:
    return {
        "abhyasi": settings.razorpay_plan_abhyasi,
        "sadhaka": settings.razorpay_plan_sadhaka,
    }.get(tier)


def _client():
    if not settings.razorpay_enabled:
        raise RuntimeError("Razorpay is not configured.")
    import razorpay

    return razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))


def create_subscription(db: Session, user: User, tier: str) -> dict:
    """Create a Razorpay subscription and a local record. Returns the Razorpay
    subscription dict (has ``short_url`` for hosted checkout)."""
    if tier not in PAID_TIERS:
        raise RuntimeError("Unknown plan.")
    plan = _plan_id(tier)
    if not plan:
        raise RuntimeError(f"No Razorpay plan configured for {tier} (set RAZORPAY_PLAN_{tier.upper()}).")
    client = _client()
    sub = client.subscription.create(
        {
            "plan_id": plan,
            "total_count": 12,
            "customer_notify": 1,
            "notes": {"user_id": str(user.id), "tier": tier},
        }
    )
    db.add(
        Subscription(
            user_id=user.id,
            tier=tier,
            status="created",
            razorpay_subscription_id=sub.get("id"),
            razorpay_plan_id=plan,
        )
    )
    db.commit()
    return sub


def _set_tier(db: Session, sub_row: Subscription, active: bool) -> None:
    user = db.get(User, sub_row.user_id)
    if active:
        if user:
            user.tier = sub_row.tier
        sub_row.status = "active"
    else:
        # Only downgrade if they're still on the tier this subscription granted.
        if user and user.tier == sub_row.tier:
            user.tier = "seeker"
        sub_row.status = "cancelled"
    db.commit()


def handle_webhook(db: Session, body: bytes, signature: str | None) -> bool:
    """Verify the Razorpay signature and apply the event. Returns True if handled."""
    if not settings.razorpay_webhook_secret or not signature:
        return False
    try:
        import razorpay

        razorpay.Utility().verify_webhook_signature(
            body.decode("utf-8"), signature, settings.razorpay_webhook_secret
        )
    except Exception:  # noqa: BLE001
        logger.warning("Razorpay webhook signature verification failed")
        return False

    try:
        event = json.loads(body)
    except Exception:  # noqa: BLE001
        return False

    name = event.get("event", "")
    payload = event.get("payload", {})
    sub_entity = (payload.get("subscription") or {}).get("entity") or {}
    sub_id = sub_entity.get("id")
    sub_row = None
    if sub_id:
        sub_row = db.execute(
            select(Subscription).where(Subscription.razorpay_subscription_id == sub_id)
        ).scalars().first()
    if sub_row is None:
        return True  # ack unknown/irrelevant events

    if name in ("subscription.activated", "subscription.charged", "subscription.resumed"):
        _set_tier(db, sub_row, active=True)
    elif name in ("subscription.cancelled", "subscription.halted", "subscription.completed", "subscription.expired"):
        _set_tier(db, sub_row, active=False)

    if name == "subscription.charged":
        pay = (payload.get("payment") or {}).get("entity") or {}
        if pay.get("id"):
            db.add(
                Payment(
                    user_id=sub_row.user_id,
                    subscription_id=sub_row.id,
                    razorpay_payment_id=pay.get("id"),
                    razorpay_order_id=pay.get("order_id"),
                    amount=(pay.get("amount") or 0) / 100.0,
                    currency=pay.get("currency", "INR"),
                    status=pay.get("status", "captured"),
                )
            )
            db.commit()
    return True


def active_subscription(db: Session, user_id: int) -> Subscription | None:
    return db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id, Subscription.status == "active")
        .order_by(Subscription.id.desc())
    ).scalars().first()


def cancel_active(db: Session, user: User) -> bool:
    sub = active_subscription(db, user.id)
    if sub is None:
        return False
    try:
        _client().subscription.cancel(sub.razorpay_subscription_id, {"cancel_at_cycle_end": 0})
    except Exception:  # noqa: BLE001
        logger.warning("Razorpay cancel failed for %s", sub.razorpay_subscription_id, exc_info=True)
    _set_tier(db, sub, active=False)
    return True
