"""Anger-triggered escalation (Chunk 5).

The choreography the mentor follows, driven by a per-conversation state machine:

  recurring anger pattern
    → (as the FIRST recommendation) offer a short video by GND
    → "yes"                 → hand over a private, 24-hour video page
    → "is there any other way?"
                            → propose a 1-on-1 with GND
    → "tell me more"        → say it's a paid consultation, ask if they want to know more
    → "yes"                 → assessment-gated link:
                                • assessment taken ≤15 days ago → the booking link (no price)
                                • otherwise → the assessment link, with a nudge to take it first
    → "I've already taken it" → acknowledge, but it was >15 days ago and patterns evolve,
                                 so a fresh reading still helps.

The mentor writes the *prose*; this module owns the *mechanics* — when to advance,
which real link to hand over, and the 24-hour expiry — so links are never
fabricated and the assessment gate is always decided from real data. Everything
degrades safely and never raises into the chat path.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Conversation,
    EscalationState,
    Event,
    ResourceGrant,
    User,
    VideoResource,
)
from app.services import memory, safety

logger = logging.getLogger("app.escalation")

# Primary trigger domain. Kept as a set so it's easy to widen later.
_ANGER_WORDS = (
    "anger", "angry", "rage", "raging", "furious", "fury", "irritat", "irritable",
    "resent", "frustrat", "temper", "snapp", "snap ", "lash out", "lashing",
    "hostile", "hostility", "seething", "outburst", "short fuse", "pissed",
)

# Stage constants.
NONE, OFFERED_VIDEO, GAVE_VIDEO, OFFERED_1ON1, EXPLAINED_PAID, SHARED_LINK, CLOSED = (
    "none", "offered_video", "gave_video", "offered_1on1", "explained_paid", "shared_link", "closed",
)


@dataclass
class Directive:
    """What this turn should do. ``inject`` is appended to the mentor's system as an
    uncached block; ``action`` triggers a real link in :func:`finalize`."""

    inject: str = ""
    next_stage: str | None = None
    action: str | None = None  # None | "video" | "oneonone" | None
    topic: str = "anger"


_EMPTY = Directive()


# --- trigger + classification ----------------------------------------------

def _has_anger(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _ANGER_WORDS)


def anger_active(db: Session, user: User, conversation: Conversation, message: str) -> bool:
    """Recurring anger: either the rolling pattern names anger as an active domain,
    or anger surfaces across ≥2 of this user's messages (including now)."""
    try:
        p = memory.get_pattern(db, user.id)
        if p is not None:
            domains = (p.active_domains or {}).get("items") if isinstance(p.active_domains, dict) else None
            if domains and any("anger" in str(d).lower() for d in domains):
                return True
            if _has_anger(p.core_knot or "") or _has_anger(p.narrative or ""):
                return True
        # Fallback: anger recurring within the current message + recent history.
        from app.models import Message

        rows = db.execute(
            select(Message.content)
            .where(Message.conversation_id == conversation.id, Message.role == "user")
            .order_by(Message.id.desc())
            .limit(8)
        ).scalars().all()
        hits = sum(1 for c in rows if _has_anger(c))
        if _has_anger(message):
            hits += 1
        return hits >= 2
    except Exception:  # noqa: BLE001
        logger.warning("anger_active check failed", exc_info=True)
        return False


# Allowed intent labels per stage — keeps the classifier tightly bounded.
_STAGE_LABELS = {
    OFFERED_VIDEO: ("affirmative", "other_way", "negative", "neutral"),
    GAVE_VIDEO: ("other_way", "affirmative", "negative", "neutral"),
    OFFERED_1ON1: ("affirmative", "tell_more", "negative", "neutral"),
    EXPLAINED_PAID: ("affirmative", "negative", "neutral"),
    SHARED_LINK: ("already_taken", "negative", "neutral"),
}

_KEYWORDS = {
    "other_way": ("other way", "another way", "anything else", "something else", "alternative",
                  "any other", "what else", "besides", "apart from", "different", "didn't help",
                  "didnt help", "not enough", "still angry", "still", "not working"),
    "already_taken": ("already", "i have taken", "i've taken", "i took", "i did it", "did it",
                      "taken it", "completed", "done it", "have done"),
    "tell_more": ("tell me more", "know more", "more about", "what is it", "what's it", "how does",
                  "how do", "details", "curious", "interested", "learn more", "go on"),
    "affirmative": ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "i would", "i'd like",
                    "go ahead", "sounds good", "alright", "definitely", "share it", "send it",
                    "show me", "i want", "let's", "lets", "absolutely"),
    "negative": ("no thanks", "no thank", "not now", "later", "not really", "nope", "i'm fine",
                 "im fine", "no need", "maybe later", "not interested", "no."),
}


def _keyword_intent(stage: str, message: str) -> str:
    t = " " + (message or "").strip().lower() + " "
    allowed = _STAGE_LABELS.get(stage, ())
    # Order matters: specific intents before the broad "affirmative"/"negative".
    for label in ("already_taken", "other_way", "tell_more", "negative", "affirmative"):
        if label in allowed and any(k in t for k in _KEYWORDS[label]):
            return label
    return "neutral"


_STAGE_CTX = {
    OFFERED_VIDEO: "offered them a short video by GND and asked if they'd like to see it",
    GAVE_VIDEO: "gave them a video and is checking whether it helped or they want another way",
    OFFERED_1ON1: "proposed a 1-on-1 with GND and is asking if they want to explore it",
    EXPLAINED_PAID: "said the 1-on-1 is a paid consultation and asked if they want to know more",
    SHARED_LINK: "shared the assessment link and expects they may say they've already taken it",
}


def _classify(cfg, stage: str, message: str) -> str:
    """Best-effort intent for the current stage. Haiku when available, else keywords."""
    kw = _keyword_intent(stage, message)
    if kw != "neutral":
        return kw
    key = cfg.key_for("anthropic")
    if not key:
        return "neutral"
    allowed = _STAGE_LABELS.get(stage, ("neutral",))
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=cfg.model_seeker,
            max_tokens=6,
            system=(
                "You label a user's short reply in a support chat. The assistant just "
                f"{_STAGE_CTX.get(stage, 'spoke to them')}. Reply with EXACTLY ONE of these labels and "
                f"nothing else: {', '.join(allowed)}."
            ),
            messages=[{"role": "user", "content": (message or "")[:500]}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip().lower()
        for label in allowed:
            if label in raw:
                return label
    except Exception:  # noqa: BLE001
        logger.warning("intent classify failed", exc_info=True)
    return "neutral"


# --- injected instructions (guide the prose; no links go through the model) --

_OFFER_VIDEO = (
    "ESCALATION — this user shows a recurring pattern of anger. In THIS reply, after you name the "
    "mechanism and give your one step, make your FIRST recommendation a gentle, optional offer: ask "
    "whether they'd like to see a short video resource from GND that speaks directly to anger. Ask it "
    "as a simple yes/no invitation. Do NOT include any link or URL — none exists yet."
)
_GIVE_VIDEO = (
    "ESCALATION — they accepted the video. In one or two warm sentences, tell them you're sharing "
    "GND's video for exactly this. Do NOT write any URL or say '24 hours' — the app appends the "
    "private link and its expiry right after your message. End your message where the link belongs."
)
_OFFER_1ON1 = (
    "ESCALATION — the video wasn't enough / they asked if there's another way. Gently propose the "
    "deeper option: a 1-on-1 with GND himself. Do NOT mention any price. Simply ask if they'd like to "
    "explore it. No links."
)
_EXPLAIN_PAID = (
    "ESCALATION — they want to know about the 1-on-1. Explain warmly that it is a paid personal "
    "consultation with GND. Do NOT state any amount (the price is on the booking page). Then ask: "
    "would you like to know more? No links."
)
_SHARE_LINK = (
    "ESCALATION — they said yes, they want to know more about booking the 1-on-1. Write one warm "
    "sentence leading into it, then STOP — do NOT write any URL, price, or instructions. The app "
    "appends the correct link and guidance right after your message."
)
_ALREADY_TAKEN = (
    "ESCALATION — they say they've already taken the assessment. Acknowledge warmly ('I understand'), "
    "then explain gently that it was more than {days} days ago; the patterns you're both working with "
    "evolve over time and were different back then, so a fresh reading will meaningfully help the "
    "session. Do NOT write any URL — the app re-appends the assessment link after your message."
)
_SOFT_REOFFER = (
    "ESCALATION — they show recurring anger and haven't taken up the video yet. Answer their message "
    "fully, and you may once more, lightly, leave the door open to GND's short video on anger. No links."
)


# --- state helpers ----------------------------------------------------------

def _get_state(db: Session, conversation_id: int) -> EscalationState | None:
    return db.execute(
        select(EscalationState).where(EscalationState.conversation_id == conversation_id)
    ).scalars().first()


def _set_stage(db: Session, user_id: int, conversation_id: int, stage: str) -> None:
    row = _get_state(db, conversation_id)
    if row is None:
        db.add(EscalationState(conversation_id=conversation_id, user_id=user_id, stage=stage))
    else:
        row.stage = stage
    db.commit()


def _log(db: Session, user_id: int, name: str, props: dict) -> None:
    try:
        db.add(Event(user_id=user_id, event_name=name, properties=props))
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


# --- planning (called before the mentor answers) ----------------------------

def plan(db: Session, user: User, conversation: Conversation, message: str) -> Directive:
    """Decide what this turn should do. Never raises."""
    try:
        if safety.check(message):  # crisis overrides the funnel entirely
            return _EMPTY
        state = _get_state(db, conversation.id)
        stage = state.stage if state else NONE

        if stage in (NONE, CLOSED):
            if stage == NONE and anger_active(db, user, conversation, message):
                return Directive(inject=_OFFER_VIDEO, next_stage=OFFERED_VIDEO)
            return _EMPTY

        # Mid-funnel: only now do we need AI config to classify their reply.
        intent = _classify(_cfg(db), stage, message)

        if stage == OFFERED_VIDEO:
            if intent == "affirmative":
                return Directive(inject=_GIVE_VIDEO, next_stage=GAVE_VIDEO, action="video")
            if intent == "other_way":
                return Directive(inject=_OFFER_1ON1, next_stage=OFFERED_1ON1)
            if intent == "negative":
                return Directive(inject="", next_stage=CLOSED)
            return Directive(inject=_SOFT_REOFFER, next_stage=OFFERED_VIDEO)

        if stage == GAVE_VIDEO:
            if intent in ("other_way", "negative"):
                return Directive(inject=_OFFER_1ON1, next_stage=OFFERED_1ON1)
            return _EMPTY

        if stage == OFFERED_1ON1:
            if intent in ("affirmative", "tell_more"):
                return Directive(inject=_EXPLAIN_PAID, next_stage=EXPLAINED_PAID)
            if intent == "negative":
                return Directive(inject="", next_stage=CLOSED)
            return Directive(inject=_EXPLAIN_PAID, next_stage=EXPLAINED_PAID)

        if stage == EXPLAINED_PAID:
            if intent == "affirmative":
                return Directive(inject=_SHARE_LINK, next_stage=SHARED_LINK, action="oneonone")
            if intent == "negative":
                return Directive(inject="", next_stage=CLOSED)
            return _EMPTY

        if stage == SHARED_LINK:
            if intent == "already_taken":
                return Directive(
                    inject=_ALREADY_TAKEN.format(days=settings.assessment_fresh_days),
                    next_stage=CLOSED,
                    action="assessment_again",
                )
            return Directive(inject="", next_stage=CLOSED)

        return _EMPTY
    except Exception:  # noqa: BLE001
        logger.warning("escalation.plan failed", exc_info=True)
        return _EMPTY


# --- finalizing (called after the mentor answers) ---------------------------

def _mint_video_url(db: Session, user: User, topic: str) -> tuple[str | None, dt.datetime | None]:
    video = db.execute(
        select(VideoResource)
        .where(VideoResource.topic == topic, VideoResource.active.is_(True))
        .order_by(VideoResource.id.desc())
    ).scalars().first()
    if video is None:
        return None, None
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(hours=settings.resource_link_ttl_hours)
    token = secrets.token_urlsafe(24)
    db.add(
        ResourceGrant(
            user_id=user.id, video_resource_id=video.id, token=token,
            expires_at=expires, created_at=now,
        )
    )
    db.commit()
    base = (settings.app_url or settings.app_base_url or "").rstrip("/")
    return f"{base}/app/resource/{token}", expires


def _assessment_fresh(user: User) -> bool:
    taken = user.assessment_taken_at
    if taken is None:
        return False
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - taken
    return age.days < settings.assessment_fresh_days


def finalize(
    db: Session, user: User, conversation: Conversation, message: str,
    answer_text: str, directive: Directive,
) -> str:
    """Append the real link(s) the directive calls for, persist the stage, log. Never raises."""
    text = answer_text or ""
    try:
        if directive.action == "video":
            url, _exp = _mint_video_url(db, user, directive.topic)
            if url:
                text += (
                    f"\n\n▶ **GND's video on this:** {url}\n"
                    f"*This private link is just for you and expires in "
                    f"{settings.resource_link_ttl_hours} hours.*"
                )
                _log(db, user.id, "escalation_video_sent", {"conversation_id": conversation.id, "topic": directive.topic})
            else:
                # No video curated for this topic yet — fall back to the 1-on-1 path
                # rather than promising something that isn't there.
                text += (
                    "\n\nI don't have a video queued for this just yet — but if you'd like, "
                    "there's a deeper option: a 1-on-1 with GND himself. Would you like to explore it?"
                )
                directive.next_stage = OFFERED_1ON1

        elif directive.action == "oneonone":
            if _assessment_fresh(user):
                url = settings.oneonone_booking_url
                if url:
                    text += f"\n\n🗓 **Book your 1-on-1 with GND here:** {url}"
                    _log(db, user.id, "escalation_booking_shared", {"conversation_id": conversation.id})
                else:
                    text += "\n\nI'll have GND's team share the booking link with you shortly."
            else:
                url = settings.assessment_url
                if url:
                    text += (
                        f"\n\nBefore the call, one thing that makes it far more useful: a quick "
                        f"**emotional assessment** — {url}. The patterns you're describing evolve, so a "
                        f"fresh reading gives GND the most to work with. Take it, and we'll set up the call."
                    )
                    _log(db, user.id, "escalation_assessment_shared", {"conversation_id": conversation.id})
                else:
                    text += "\n\nI'll have GND's team guide you through the next step shortly."

        elif directive.action == "assessment_again":
            url = settings.assessment_url
            if url:
                text += f"\n\nHere it is again whenever you're ready: {url}"

        if directive.next_stage:
            _set_stage(db, user.id, conversation.id, directive.next_stage)
    except Exception:  # noqa: BLE001
        logger.warning("escalation.finalize failed", exc_info=True)
    return text


# --- viewing a granted resource ---------------------------------------------

def grant_for_view(db: Session, user: User, token: str) -> tuple[VideoResource | None, str]:
    """Return (video, status) where status ∈ ok | expired | notfound. Ownership-checked."""
    grant = db.execute(select(ResourceGrant).where(ResourceGrant.token == token)).scalars().first()
    if grant is None or grant.user_id != user.id:
        return None, "notfound"
    expires = grant.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if dt.datetime.now(dt.timezone.utc) >= expires:
        return None, "expired"
    video = db.get(VideoResource, grant.video_resource_id) if grant.video_resource_id else None
    if video is None:
        return None, "notfound"
    return video, "ok"


def _cfg(db: Session):
    from app.services import ai_settings

    return ai_settings.resolved(db)
