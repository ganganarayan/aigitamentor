"""Token metering (BuildPrompt "Tiers & token metering — FINAL").

Meter unit = conversation tokens (fresh input + output; the cached system prompt
is excluded automatically — Anthropic's ``input_tokens`` already omits cache
reads/writes). Governing limit = min(daily remaining, monthly remaining); free
is governed by the daily cap only (no monthly cap). Daily resets at midnight IST;
monthly resets on the billing-cycle start (else the 1st of the IST month).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Subscription, UsageCounter

TIER_DAILY = {"seeker": 9_000, "abhyasi": 40_000, "sadhaka": 110_000}
TIER_MONTHLY = {"abhyasi": 460_000, "sadhaka": 1_360_000}  # seeker: none
_APPROX_TOKENS_PER_Q = 1_800  # for the "≈ N questions" hint
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ist_today() -> dt.date:
    return dt.datetime.now(tz=IST).date()


def _month_start(db: Session, user) -> dt.date:
    sub = db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id, Subscription.status == "active")
        .order_by(Subscription.id.desc())
    ).scalars().first()
    if sub is not None and sub.current_period_start is not None:
        return sub.current_period_start.astimezone(IST).date()
    return ist_today().replace(day=1)


def daily_used(db: Session, user) -> int:
    v = db.execute(
        select(UsageCounter.conversation_tokens).where(
            UsageCounter.user_id == user.id, UsageCounter.period_date == ist_today()
        )
    ).scalar_one_or_none()
    return int(v or 0)


def monthly_used(db: Session, user) -> int:
    start = _month_start(db, user)
    v = db.execute(
        select(func.coalesce(func.sum(UsageCounter.conversation_tokens), 0)).where(
            UsageCounter.user_id == user.id, UsageCounter.period_date >= start
        )
    ).scalar_one()
    return int(v or 0)


def status(db: Session, user) -> dict:
    tier = user.tier
    dcap = TIER_DAILY.get(tier, TIER_DAILY["seeker"])
    dused = daily_used(db, user)
    dleft = max(0, dcap - dused)
    mcap = TIER_MONTHLY.get(tier)
    if mcap:
        mused = monthly_used(db, user)
        mleft = max(0, mcap - mused)
        governing = min(dleft, mleft)
    else:
        mused = mleft = None
        governing = dleft
    return {
        "tier": tier,
        "daily_cap": dcap,
        "daily_used": dused,
        "daily_left": dleft,
        "daily_pct": round(100 * dused / dcap) if dcap else 0,
        "monthly_cap": mcap,
        "monthly_used": mused,
        "monthly_left": mleft,
        "governing_left": governing,
        "questions_left_today": dleft // _APPROX_TOKENS_PER_Q,
        "questions_left_month": (mleft // _APPROX_TOKENS_PER_Q) if mleft is not None else None,
    }


def allowed(db: Session, user) -> bool:
    return status(db, user)["governing_left"] > 0


def add_usage(db: Session, user, tokens: int) -> None:
    day = ist_today()
    counter = db.execute(
        select(UsageCounter).where(UsageCounter.user_id == user.id, UsageCounter.period_date == day)
    ).scalars().first()
    if counter is None:
        counter = UsageCounter(user_id=user.id, period_date=day, conversation_tokens=0, message_count=0)
        db.add(counter)
        db.flush()
    counter.conversation_tokens = (counter.conversation_tokens or 0) + max(0, int(tokens or 0))
    counter.message_count = (counter.message_count or 0) + 1
    db.commit()


def limit_message(tier: str) -> str:
    if tier == "seeker":
        return (
            "You've used today's free tokens — the free tier resets at midnight. The work continues, "
            "and your journey is *remembered*, in Abhyāsi. Come back tomorrow, or step up when you're ready."
        )
    return "You've reached today's limit for your plan. It resets at midnight IST — I'll be here."
