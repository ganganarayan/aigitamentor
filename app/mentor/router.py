"""Gated AI Mentor routes — the chat product (authenticated)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_user
from app.db import get_db
from app.models import Conversation, Generation, Message
from app.services import chat
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
def app_home(request: Request, c: int | None = None, user=Depends(require_user), db: Session = Depends(get_db)):
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
        },
    )


class ChatIn(BaseModel):
    message: str
    conversation_id: int | None = None


@router.post("/app/chat")
def chat_send(payload: ChatIn, user=Depends(require_user), db: Session = Depends(get_db)):
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

    # Tier rate limit.
    allowed, count, cap = chat.check_and_increment_usage(db, user)
    if not allowed:
        return JSONResponse(
            {
                "limited": True,
                "cap": cap,
                "conversation_id": conv.id,
                "answer": (
                    "You've reached today's limit on the free tier. The work continues — "
                    "and your journey is remembered — in Abhyāsi. Come back tomorrow, or step up when you're ready."
                ),
            }
        )

    # Persist the user's message; title the conversation from the first line.
    db.add(Message(conversation_id=conv.id, role="user", content=message))
    if not conv.title:
        conv.title = message[:60]
    db.commit()

    # Generate the grounded answer.
    try:
        gen, answer_text = chat.answer(db, user, conv, message)
        generation_id = gen.id
    except Exception as exc:  # noqa: BLE001 — surface a friendly message, never 500 the chat
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
            "count": count,
            "cap": cap,
        }
    )


class FeedbackIn(BaseModel):
    generation_id: int
    kind: str  # liked | copied | shared


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
