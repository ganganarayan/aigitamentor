"""Memory & pattern engine (BuildPrompt "the core mechanic").

Never inject raw transcripts. Each turn injects: profile + (paid) the user's
pattern + the conversation's rolling summary. The summary and pattern are
Haiku-generated between turns (a background task after each answer):

- **Rolling summary** — updated every turn; carries within-conversation context
  so we never resend the raw thread.
- **Pattern** — one evolving per-user record, (re)generated every 5th question.

Free tier: summary + pattern are computed (for analytics + a warm start on
upgrade) but the pattern is NOT injected cross-session — free sessions stay cold.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Conversation,
    ConversationSummary,
    Message,
    User,
    UserPattern,
)
from app.services import ai_settings

logger = logging.getLogger("app.memory")


# --- read side (assembled per turn) -----------------------------------------

def profile_line(user: User) -> str:
    bits = []
    if user.age:
        bits.append(f"{user.age}yo")
    if user.gender:
        bits.append(user.gender)
    if user.profession:
        bits.append(user.profession)
    return ("Who they are: " + ", ".join(bits) + ".") if bits else ""


def get_summary(db: Session, conversation_id: int) -> str:
    row = db.execute(
        select(ConversationSummary).where(ConversationSummary.conversation_id == conversation_id)
    ).scalars().first()
    return (row.rolling_summary or "") if row else ""


def get_pattern(db: Session, user_id: int) -> UserPattern | None:
    return db.execute(select(UserPattern).where(UserPattern.user_id == user_id)).scalars().first()


def pattern_text(p: UserPattern) -> str:
    parts = []
    if p.narrative:
        parts.append(p.narrative)
    if p.core_knot:
        parts.append(f"Core knot: {p.core_knot}")
    if p.tried_didnt_work:
        vals = p.tried_didnt_work.get("items") if isinstance(p.tried_didnt_work, dict) else p.tried_didnt_work
        if vals:
            parts.append("Already tried (don't re-prescribe): " + "; ".join(map(str, vals)))
    if p.trajectory:
        parts.append(f"Trajectory: {p.trajectory}")
    return " ".join(parts)


def build_memory(db: Session, user: User, conversation: Conversation) -> str:
    parts = []
    prof = profile_line(user)
    if prof:
        parts.append(prof)
    if user.tier != "seeker":  # cross-session pattern only for paid
        p = get_pattern(db, user.id)
        if p is not None:
            txt = pattern_text(p)
            if txt:
                parts.append("What you know about them: " + txt)
    summary = get_summary(db, conversation.id)
    if summary:
        parts.append("Where this conversation is: " + summary)
    elif user.tier == "seeker":
        parts.append("Free-tier session — you do not retain memory between sessions; memory begins at Abhyāsi.")
    return "\n".join(parts) if parts else "New user — no history yet."


# --- write side (background, own session) -----------------------------------

def _haiku(cfg, system: str, user_content: str, max_tokens: int = 400) -> str:
    key = cfg.key_for("anthropic")
    if not key:
        raise RuntimeError("no anthropic key")
    from app.services import anthropic_client

    client = anthropic_client.make(key, retries=anthropic_client.BG_RETRIES)
    resp = client.messages.create(
        model=cfg.model_seeker,  # Haiku — cheap
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _recent_exchange(db: Session, conversation_id: int, limit: int = 6) -> str:
    rows = db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.role.in_(("user", "assistant")))
        .order_by(Message.id.desc())
        .limit(limit)
    ).scalars().all()
    return "\n".join(f"{m.role}: {m.content}" for m in reversed(rows))


def _update_summary(db: Session, cfg, user_id: int, conversation_id: int) -> None:
    prior = get_summary(db, conversation_id)
    exchange = _recent_exchange(db, conversation_id)
    if not exchange:
        return
    summary = _haiku(
        cfg,
        "Maintain a running summary of a mentoring conversation. Return ONLY the updated summary, "
        "third-person, ≤180 words: the person's situation, what's been diagnosed (nervous-system "
        "mechanism), what was prescribed, and where they are now.",
        f"PRIOR SUMMARY:\n{prior or '(none)'}\n\nRECENT EXCHANGE:\n{exchange}",
        max_tokens=350,
    )
    row = db.execute(
        select(ConversationSummary).where(ConversationSummary.conversation_id == conversation_id)
    ).scalars().first()
    if row is None:
        db.add(ConversationSummary(conversation_id=conversation_id, user_id=user_id, rolling_summary=summary))
    else:
        row.rolling_summary = summary
    db.commit()


_PATTERN_SYS = (
    "From the material, produce a JSON pattern of this person for their mentor. Reply with ONLY JSON: "
    '{"narrative": <≤150-word third-person summary of their recurring inner situation>, '
    '"core_knot": <the recurring unresolved thing>, "signature": <SNS/PNS lean & reactivity, short>, '
    '"active_domains": [<domains like anger, work, parenting, purpose>], '
    '"tried_didnt_work": [<approaches already tried without lasting effect>], '
    '"stage_by_domain": {<domain>: <one of recognition|mechanism|first-regulation|deeper-practice|integration>}, '
    '"trajectory": <one of improving|stuck|deepening>, '
    '"archetype_tag": <a named GND pattern if it clearly fits e.g. "Sovereign\'s Desert", else null>}'
)


def _material_for_pattern(db: Session, user_id: int) -> str:
    summaries = db.execute(
        select(ConversationSummary.rolling_summary)
        .where(ConversationSummary.user_id == user_id)
        .order_by(ConversationSummary.updated_at.desc())
        .limit(5)
    ).scalars().all()
    msgs = db.execute(
        select(Message.content)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == user_id, Message.role == "user")
        .order_by(Message.id.desc())
        .limit(12)
    ).scalars().all()
    return "SUMMARIES:\n" + "\n".join(s for s in summaries if s) + "\n\nRECENT MESSAGES:\n" + "\n".join(msgs)


def _regenerate_pattern(db: Session, cfg, user_id: int) -> None:
    raw = _haiku(cfg, _PATTERN_SYS, _material_for_pattern(db, user_id), max_tokens=600)
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return
    p = get_pattern(db, user_id)
    if p is None:
        p = UserPattern(user_id=user_id, version=1)
        db.add(p)
    else:
        p.version = (p.version or 1) + 1
    p.narrative = (data.get("narrative") or None)
    p.core_knot = (data.get("core_knot") or None)
    p.signature = (str(data["signature"])[:200] if data.get("signature") else None)
    p.active_domains = {"items": data.get("active_domains") or []}
    p.tried_didnt_work = {"items": data.get("tried_didnt_work") or []}
    p.stage_by_domain = data.get("stage_by_domain") or {}
    p.trajectory = (str(data["trajectory"])[:20] if data.get("trajectory") else None)
    p.archetype_tag = (str(data["archetype_tag"])[:60] if data.get("archetype_tag") else None)
    db.commit()


def refresh_after_turn(user_id: int, conversation_id: int) -> None:
    """Background: update the rolling summary; regenerate the pattern every 5th question."""
    from app.db import SessionLocal

    if SessionLocal is None:
        return
    with SessionLocal() as db:
        cfg = ai_settings.resolved(db)
        if not cfg.key_for("anthropic"):
            return
        try:
            _update_summary(db, cfg, user_id, conversation_id)
        except Exception:  # noqa: BLE001
            logger.warning("summary update failed", exc_info=True)
        try:
            q = db.execute(
                select(func.count())
                .select_from(Message)
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id == user_id, Message.role == "user")
            ).scalar_one()
            if q >= 5 and q % 5 == 0:
                _regenerate_pattern(db, cfg, user_id)
        except Exception:  # noqa: BLE001
            logger.warning("pattern regen failed", exc_info=True)
