"""Accounts & billing (Section 4.6).

The app NEVER stores or accepts card data — Razorpay hosted checkout only.
``payments`` holds Razorpay ids and status, not card details.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class User(Base, PkMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(40))  # collected at signup
    password_hash: Mapped[str | None] = mapped_column(String(255))  # null for OAuth-only
    oauth_provider: Mapped[str | None] = mapped_column(String(40))
    oauth_subject: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user", index=True)  # admin|user
    tier: Mapped[str] = mapped_column(String(20), default="seeker", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    assessment_band: Mapped[str | None] = mapped_column(String(60))  # from Assess360
    # When they last took the emotional assessment — gates the 1-on-1 offer (Chunk 5).
    assessment_taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_ai_source: Mapped[str | None] = mapped_column(String(60))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Profile collected on first contact (the personalization differentiator).
    age: Mapped[int | None] = mapped_column(Integer)
    profession: Mapped[str | None] = mapped_column(String(160))
    gender: Mapped[str | None] = mapped_column(String(40))
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # Admin-set / subscription plan end date (auto-downgrade to seeker once past).
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(Base, PkMixin, TimestampMixin):
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tier: Mapped[str] = mapped_column(String(20))  # abhyasi|sadhaka
    status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String(120), index=True)
    razorpay_plan_id: Mapped[str | None] = mapped_column(String(120))
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class Payment(Base, PkMixin, TimestampMixin):
    __tablename__ = "payments"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(120), index=True)
    razorpay_order_id: Mapped[str | None] = mapped_column(String(120))
    amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
