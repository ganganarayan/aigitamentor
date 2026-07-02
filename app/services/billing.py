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

from app.models import Payment, Setting, Subscription, User
from app.services import settings_store

logger = logging.getLogger("app.billing")

# Display prices (INR/month). The authoritative amount lives in the Razorpay plan.
TIER_PRICE = {"abhyasi": 499, "sadhaka": 1459}
PAID_TIERS = ("abhyasi", "sadhaka")
_AUTOPLAN_KEY = "razorpay_autoplans"  # cached plan ids the app created via the API


def _client(db: Session):
    if not settings_store.razorpay_enabled(db):
        raise RuntimeError("Razorpay is not configured.")
    import razorpay

    return razorpay.Client(
        auth=(settings_store.get("razorpay_key_id", db), settings_store.get("razorpay_key_secret", db))
    )


def diagnose(db: Session) -> tuple[bool, str]:
    """Full dry-run of the real checkout path, returning (ok, human message). Tests
    auth → plan.create → subscription.create, cancelling the test subscription so
    it leaves nothing live. Pinpoints the exact failing step with Razorpay's own
    message."""
    if not settings_store.razorpay_enabled(db):
        return False, "Key ID / Key Secret are not set."
    try:
        client = _client(db)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    try:  # 1) auth — side-effect-free
        client.payment.all({"count": 1})
    except Exception as exc:  # noqa: BLE001
        return False, f"Authentication failed — the Key ID and Key Secret don't match. Razorpay said: {exc}"
    try:  # 2) plan (the app auto-creates + caches this)
        plan = _resolve_plan(db, client, "abhyasi")
    except Exception as exc:  # noqa: BLE001
        return False, f"Plan creation failed. Razorpay said: {exc}"
    try:  # 3) subscription — create a throwaway one, then cancel it immediately
        sub = client.subscription.create(
            {"plan_id": plan, "total_count": 1, "customer_notify": 0, "notes": {"diagnostic": "1"}}
        )
        sid = sub.get("id")
        if sid:
            try:
                client.subscription.cancel(sid, {"cancel_at_cycle_end": 0})
            except Exception:  # noqa: BLE001
                pass  # cleanup best-effort; the diagnostic already succeeded
    except Exception as exc:  # noqa: BLE001
        return False, f"Subscription creation failed. Razorpay said: {exc}"
    return True, "Connected — auth, plan, and a test subscription all worked. Checkout should go through."


def _autoplans(db: Session) -> dict:
    row = db.execute(select(Setting).where(Setting.key == _AUTOPLAN_KEY)).scalar_one_or_none()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _cache_autoplan(db: Session, tier: str, plan_id: str) -> None:
    row = db.execute(select(Setting).where(Setting.key == _AUTOPLAN_KEY)).scalar_one_or_none()
    data = dict(row.value) if row and isinstance(row.value, dict) else {}
    data[tier] = plan_id
    if row is None:
        db.add(Setting(key=_AUTOPLAN_KEY, value=data))
    else:
        row.value = data
    db.commit()


def _resolve_plan(db: Session, client, tier: str) -> str:
    """Get a Razorpay plan id WITHOUT anyone touching the dashboard.

    Order: an explicitly-configured ``plan_…`` id (Settings → Integrations) → a
    plan the app already auto-created (cached) → otherwise create one via the API
    now and cache it. So GND never creates a plan or supplies a plan id.
    """
    configured = settings_store.get(f"razorpay_plan_{tier}", db)
    if configured and str(configured).startswith("plan_"):
        return configured
    cached = _autoplans(db).get(tier)
    if cached and str(cached).startswith("plan_"):
        return cached
    price = TIER_PRICE.get(tier, 0)
    created = client.plan.create({
        "period": "monthly",
        "interval": 1,
        "item": {"name": f"AI Gita Mentor — {tier.title()}", "amount": int(price) * 100, "currency": "INR"},
        "notes": {"tier": tier},
    })
    plan_id = created.get("id")
    if not plan_id:
        raise RuntimeError("Razorpay plan creation returned no id.")
    _cache_autoplan(db, tier, plan_id)
    logger.info("Auto-created Razorpay plan %s for %s", plan_id, tier)
    return plan_id


def create_subscription(db: Session, user: User, tier: str) -> dict:
    """Create a Razorpay subscription (plan auto-created via API — no dashboard
    setup) and a local record. Returns the subscription dict (has ``short_url``)."""
    if tier not in PAID_TIERS:
        raise RuntimeError("Unknown plan.")
    client = _client(db)
    plan = _resolve_plan(db, client, tier)
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
