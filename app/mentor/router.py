"""Gated AI Mentor routes — the chat product (authenticated)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_user
from app.auth.security import COOKIE_NAME
from app.db import get_db
from app.models import (
    Contact,
    Conversation,
    ConversationSummary,
    Generation,
    Message,
    Payment,
    Subscription,
    UserPattern,
)
from app.services import chat, memory, metering, safety
from app.templating import templates

logger = logging.getLogger("app.mentor")
router = APIRouter(tags=["mentor"])


def _conversations(db: Session, user_id: int) -> list[Conversation]:
    return list(
        db.execute(
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.archived.is_(False))
            .order_by(Conversation.updated_at.desc())
            .limit(50)
        ).scalars()
    )


@router.get("/app", response_class=HTMLResponse)
def app_home(
    request: Request,
    c: int | None = None,
    welcome: int = 0,
    reg: str | None = None,
    trial: str | None = None,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    # No PageView inside the app — the app is a single gated surface; the signals
    # that matter here are CompleteRegistration / StartTrial (welcome) + Purchase.
    conversations = _conversations(db, user.id)
    active = None
    if c is not None:
        active = db.get(Conversation, c)
        if active is None or active.user_id != user.id:
            active = None
    if active is None and conversations:
        active = conversations[0]
    messages = []
    if active is not None:
        messages = list(
            db.execute(
                select(Message)
                .where(Message.conversation_id == active.id, Message.role.in_(("user", "assistant")))
                .order_by(Message.id)
            ).scalars()
        )
    return templates.TemplateResponse(
        "app/chat.html",
        {
            "request": request,
            "user": user,
            "conversations": conversations,
            "active": active,
            "messages": messages,
            "welcome": bool(welcome),
            "reg_event_id": reg,
            "trial_event_id": trial,
        },
    )


class ChatIn(BaseModel):
    message: str
    conversation_id: int | None = None


@router.post("/app/chat")
def chat_send(
    payload: ChatIn,
    background: BackgroundTasks,
    user=Depends(require_user),
    db: Session = Depends(get_db),
):
    message = (payload.message or "").strip()
    if not message:
        return JSONResponse({"error": "Empty message."}, status_code=400)

    # Resolve or create the conversation.
    conv = None
    if payload.conversation_id is not None:
        conv = db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != user.id:
            conv = None
    if conv is None:
        conv = Conversation(user_id=user.id, title=message[:60])
        db.add(conv)
        db.commit()
        db.refresh(conv)

    # Persist the user's message first; title the conversation from the first line.
    db.add(Message(conversation_id=conv.id, role="user", content=message))
    if not conv.title:
        conv.title = message[:60]
    db.commit()

    # Safety: log crisis signals for admin review (the prompt handles the reply).
    safety.maybe_flag(db, user.id, conv.id, message)

    # First-contact onboarding — the personalization differentiator.
    if not user.onboarded:
        prior_assistant = db.execute(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == user.id, Message.role == "assistant")
        ).scalar_one()
        if prior_assistant == 0:
            # Their very first message → ask for age/profession/gender before answering.
            db.add(Message(conversation_id=conv.id, role="assistant", content=chat.ONBOARDING_PROMPT))
            db.commit()
            return JSONResponse(
                {"answer": chat.ONBOARDING_PROMPT, "conversation_id": conv.id, "onboarding": True}
            )
        # We already asked → this message carries their profile. Store it, then answer.
        profile = chat.extract_profile(db, message)
        if profile.get("age") is not None:
            user.age = profile["age"]
        if profile.get("profession"):
            user.profession = profile["profession"]
        if profile.get("gender"):
            user.gender = profile["gender"]
        user.onboarded = True
        db.commit()

    # Token metering — governing = min(daily, monthly); free = daily only.
    if not metering.allowed(db, user):
        return JSONResponse(
            {"limited": True, "conversation_id": conv.id, "answer": metering.limit_message(user.tier)}
        )

    # Generate the grounded answer.
    try:
        gen, answer_text = chat.answer(db, user, conv, message)
        generation_id = gen.id
        metering.add_usage(db, user, (gen.tokens_in or 0) + (gen.tokens_out or 0))
        # Refresh summary + pattern in the background (memory engine).
        background.add_task(memory.refresh_after_turn, user.id, conv.id)
    except Exception:  # noqa: BLE001 — surface a friendly message, never 500 the chat
        logger.exception("Chat generation failed")
        answer_text = (
            "I'm not able to answer right now — the mentor isn't fully configured yet "
            "(an AI key may be missing in Settings → AI). Please try again shortly."
        )
        generation_id = None

    db.add(
        Message(conversation_id=conv.id, role="assistant", content=answer_text, generation_id=generation_id)
    )
    db.commit()
    return JSONResponse(
        {
            "answer": answer_text,
            "conversation_id": conv.id,
            "generation_id": generation_id,
            "usage": metering.status(db, user),
        }
    )


class FeedbackIn(BaseModel):
    generation_id: int
    kind: str  # liked | copied | shared


@router.get("/app/usage")
def app_usage(user=Depends(require_user), db: Session = Depends(get_db)):
    return JSONResponse(metering.status(db, user))


@router.post("/app/feedback")
def chat_feedback(payload: FeedbackIn, user=Depends(require_user), db: Session = Depends(get_db)):
    gen = db.get(Generation, payload.generation_id)
    if gen is None or gen.user_id != user.id:
        return JSONResponse({"ok": False}, status_code=404)
    if payload.kind in ("liked", "copied", "shared"):
        fb = dict(gen.feedback or {})
        fb[payload.kind] = True
        gen.feedback = fb
        db.commit()
    return JSONResponse({"ok": True})


# --- Account: export + deletion (privacy) ----------------------------------

@router.get("/app/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse("app/account.html", {"request": request, "user": user})


@router.get("/app/account/export", response_class=JSONResponse)
def account_export(user=Depends(require_user), db: Session = Depends(get_db)):
    convs = list(db.execute(select(Conversation).where(Conversation.user_id == user.id)).scalars())
    data = {
        "profile": {
            "email": user.email, "name": user.name, "phone": user.phone,
            "age": user.age, "gender": user.gender, "profession": user.profession,
            "tier": user.tier, "assessment_band": user.assessment_band,
            "referral_ai_source": user.referral_ai_source,
        },
        "conversations": [
            {
                "id": c.id, "title": c.title,
                "messages": [
                    {"role": m.role, "content": m.content}
                    for m in db.execute(
                        select(Message).where(Message.conversation_id == c.id).order_by(Message.id)
                    ).scalars()
                ],
            }
            for c in convs
        ],
        "subscriptions": [
            {"tier": s.tier, "status": s.status, "razorpay_subscription_id": s.razorpay_subscription_id}
            for s in db.execute(select(Subscription).where(Subscription.user_id == user.id)).scalars()
        ],
        "payments": [
            {"amount": float(p.amount or 0), "currency": p.currency, "status": p.status}
            for p in db.execute(select(Payment).where(Payment.user_id == user.id)).scalars()
        ],
    }
    return JSONResponse(
        data, headers={"Content-Disposition": 'attachment; filename="my-ai-gita-mentor-data.json"'}
    )


@router.post("/app/account/delete")
def account_delete(user=Depends(require_user), db: Session = Depends(get_db)):
    """Delete personal data + conversations; retain legally-required billing by
    anonymising the account (keeps subscriptions/payments)."""
    uid = user.id
    db.execute(delete(Generation).where(Generation.user_id == uid))
    db.execute(delete(ConversationSummary).where(ConversationSummary.user_id == uid))
    db.execute(delete(Conversation).where(Conversation.user_id == uid))  # cascades messages
    db.execute(delete(UserPattern).where(UserPattern.user_id == uid))
    db.execute(delete(Contact).where(Contact.user_id == uid))
    user.email = f"deleted-{uid}@removed.invalid"
    user.name = user.phone = user.age = user.gender = user.profession = None
    user.password_hash = user.oauth_subject = user.assessment_band = None
    user.status = "deleted"
    user.tier = "seeker"
    db.commit()
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
