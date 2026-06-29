"""Acquisition & automation (Section 4.7) + operational AI config.

contacts/events drive funnel analytics by AI-referral source. webhooks,
promotions, onboarding, help, and settings are the scalable shell — only the
v1 items get UIs first.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class Contact(Base, PkMixin, TimestampMixin):
    __tablename__ = "contacts"

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str | None] = mapped_column(String(200))
    role_segment: Mapped[str | None] = mapped_column(String(80))
    utm_source: Mapped[str | None] = mapped_column(String(120))
    utm_medium: Mapped[str | None] = mapped_column(String(120))
    utm_campaign: Mapped[str | None] = mapped_column(String(120))
    utm_term: Mapped[str | None] = mapped_column(String(120))
    utm_content: Mapped[str | None] = mapped_column(String(120))
    referral_ai_source: Mapped[str | None] = mapped_column(String(60), index=True)
    opted_in: Mapped[bool] = mapped_column(Boolean, default=False)
    signed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    subscribed_tier: Mapped[str | None] = mapped_column(String(20))
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base, PkMixin):
    __tablename__ = "events"

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), index=True)
    event_name: Mapped[str] = mapped_column(String(120), index=True)
    properties: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Webhook(Base, PkMixin, TimestampMixin):
    __tablename__ = "webhooks"

    name: Mapped[str] = mapped_column(String(120))
    target_url: Mapped[str] = mapped_column(Text)
    event_types: Mapped[dict | None] = mapped_column(JSONB)
    secret: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookLog(Base, PkMixin, TimestampMixin):
    __tablename__ = "webhook_logs"

    webhook_id: Mapped[int | None] = mapped_column(ForeignKey("webhooks.id", ondelete="SET NULL"), index=True)
    event_name: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_text: Mapped[str | None] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, default=False)


class Promotion(Base, PkMixin, TimestampMixin):
    __tablename__ = "promotions"

    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class OnboardingStage(Base, PkMixin, TimestampMixin):
    __tablename__ = "onboarding_stages"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(80))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict | None] = mapped_column(JSONB)


class HelpArticle(Base, PkMixin, TimestampMixin):
    __tablename__ = "help_articles"

    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    published: Mapped[bool] = mapped_column(Boolean, default=False)


class Setting(Base, PkMixin, TimestampMixin):
    """Generic key/value app settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    value: Mapped[dict | None] = mapped_column(JSONB)
