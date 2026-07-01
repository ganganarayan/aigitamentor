"""Escalation layer (Chunk 5) — anger-triggered video resource + 1-on-1 with GND.

When a user's pattern shows recurring anger, the mentor offers a private video by
GND (a time-limited page built from an admin-curated embed), and — if that isn't
enough — the paid 1-on-1, gated on how recently they took the emotional
assessment. State advances per conversation so the choreography stays coherent
even though the mentor is sent only the current turn.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PkMixin, TimestampMixin


class VideoResource(Base, PkMixin, TimestampMixin):
    """An admin-curated video, keyed by a small domain topic (e.g. ``anger``).

    ``embed_html`` is trusted HTML GND pastes from the video host (Cloudflare
    Stream, unlisted YouTube, Vimeo, …) — rendered as-is on the private page.
    """

    __tablename__ = "video_resources"

    topic: Mapped[str] = mapped_column(String(60), index=True)  # anger, sleep, parenting…
    title: Mapped[str] = mapped_column(String(300))
    embed_html: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)  # optional line shown under the video
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class ResourceGrant(Base, PkMixin):
    """A private, time-limited grant of one VideoResource to one user — the
    dynamic page the mentor hands out. Verified + expiry-checked on view."""

    __tablename__ = "resource_grants"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    video_resource_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_resources.id", ondelete="SET NULL")
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EscalationState(Base, PkMixin, TimestampMixin):
    """Where a conversation is in the anger → video → 1-on-1 funnel.

    stage ∈ none | offered_video | gave_video | offered_1on1 | explained_paid |
    shared_link | closed.
    """

    __tablename__ = "escalation_states"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(30), default="none")
