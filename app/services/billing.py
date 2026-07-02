"""Razorpay subscription billing (Section 3 / 13).

Hosted checkout: we create a Razorpay Subscription for the chosen plan and send
the user to its ``short_url``. Razorpay handles the card mandate and recurring
charges; the **webhook** is the source of truth that flips the user's tier. The
app never stores or accepts card data.

All calls are key-guarded — without Razorpay configured, the upgrade page shows
a friendly "not available yet" instead of crashing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Payment, Subscription, User
from app.services import settings_store

logger = logging.getLogger("app.billing")

# Prices (INR/month). Charged as a one-time Razorpay Order per month (no Plans /
# Subscriptions setup needed — Orders work on any activated account).
TIER_PRICE = {"abhyasi": 499, "sadhaka": 1459}
PAID_TIERS = ("abhyasi", "sadhaka")


def _client(db: Session):
    if not settings_store.razorpay_enabled(db):
        raise RuntimeError("Razorpay is not configured.")
    import razorpay

    return razorpay.Client(
        auth=(settings_store.get("razorpay_key_id", db), settings_store.get("razorpay_key_secret", db))
    )


def diagnose(db: Session) -> tuple[bool, str]:
    """Auth + a test Order (the actual checkout mechanism). Orders work on any
    activated account — no Subscriptions/Plans setup needed."""
    if not settings_store.razorpay_enabled(db):
        return False, "Key ID / Key Secret are not set."
    try:
        client = _client(db)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    try:
        order = client.order.create({"amount": 100, "currency": "INR", "notes": {"diagnostic": "1"}})
    except Exception as exc:  # noqa: BLE001
        return False, f"Order creation failed — check the Key ID/Secret. Razorpay said: {exc}"
    return True, f"Connected — keys valid and Checkout works (test order {order.get('id')})."


def create_order(db: Session, user: User, tier: str) -> dict:
    """Create a Razorpay Order for one month of the tier — one-time payment via
    Checkout (no Plans, no Subscriptions). Returns the order dict (id + amount)."""
    if tier not in PAID_TIERS:
        raise RuntimeError("Unknown plan.")
    client = _client(db)
    return client.order.create({
        "amount": int(TIER_PRICE[tier]) * 100,  # paise
        "currency": "INR",
        "notes": {"user_id": str(user.id), "tier": tier},
    })


def verify_and_activate(db: Session, user: User, *, order_id: str, payment_id: str, signature: str) -> tuple[bool, str]:
    """Verify the Checkout signature, record the payment (idempotently), and grant
    30 days of the tier. Returns (ok, tier-or-error-message)."""
    client = _client(db)
    try:
        client.utility.verify_payment_signature({
            "razorpay_order_id": order_id or "",
            "razorpay_payment_id": payment_id or "",
            "razorpay_signature": signature or "",
        })
    except Exception:  # noqa: BLE001
        return False, "Payment could not be verified."
    try:
        order = client.order.fetch(order_id)
        pay = client.payment.fetch(payment_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not fetch the payment: {exc}"
    tier = (order.get("notes") or {}).get("tier")
    if tier not in PAID_TIERS:
        return False, "Unknown tier on the order."
    amount = (pay.get("amount") or 0) / 100.0
    if db.execute(select(Payment).where(Payment.razorpay_payment_id == payment_id)).scalar_one_or_none() is None:
        db.add(Payment(
            user_id=user.id, razorpay_payment_id=payment_id, razorpay_order_id=order_id,
            amount=amount, currency=pay.get("currency", "INR"), status=pay.get("status", "captured"),
        ))
    now = dt.datetime.now(dt.timezone.utc)
    base = user.plan_expires_at if (user.plan_expires_at and user.plan_expires_at > now) else now
    user.tier = tier
    user.plan_expires_at = base + dt.timedelta(days=30)
    db.commit()
    try:
        from app.services import meta

        meta.track_purchase(user.email, value=amount, phone=user.phone, external_id=user.id, event_id=payment_id)
    except Exception:  # noqa: BLE001
        logger.warning("Purchase event failed", exc_info=True)
    return True, tier


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
    webhook_secret = settings_store.get("razorpay_webhook_secret", db)
    if not webhook_secret or not signature:
        return False
    try:
        import razorpay

        razorpay.Utility().verify_webhook_signature(body.decode("utf-8"), signature, webhook_secret)
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
        if name == "subscription.activated":
            from app.services import meta

            owner = db.get(User, sub_row.user_id)
            if owner is not None:
                meta.track_start_trial(owner.email)  # server-side StartTrial (CAPI)
    elif name in ("subscription.cancelled", "subscription.halted", "subscription.completed", "subscription.expired"):
        _set_tier(db, sub_row, active=False)

    if name == "subscription.charged":
        pay = (payload.get("payment") or {}).get("entity") or {}
        if pay.get("id"):
            amount = (pay.get("amount") or 0) / 100.0
            db.add(
                Payment(
                    user_id=sub_row.user_id,
                    subscription_id=sub_row.id,
                    razorpay_payment_id=pay.get("id"),
                    razorpay_order_id=pay.get("order_id"),
                    amount=amount,
                    currency=pay.get("currency", "INR"),
                    status=pay.get("status", "captured"),
                )
            )
            db.commit()
            from app.services import meta

            owner = db.get(User, sub_row.user_id)
            if owner is not None:
                # Purchase (CAPI) — event_id = razorpay payment id → idempotent dedup.
                meta.track_purchase(
                    owner.email, value=amount, currency=pay.get("currency", "INR"),
                    phone=owner.phone, external_id=owner.id, event_id=pay.get("id"),
                )
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
        _client(db).subscription.cancel(sub.razorpay_subscription_id, {"cancel_at_cycle_end": 0})
    except Exception:  # noqa: BLE001
        logger.warning("Razorpay cancel failed for %s", sub.razorpay_subscription_id, exc_info=True)
    _set_tier(db, sub, active=False)
    return True
