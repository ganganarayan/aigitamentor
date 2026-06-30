"""Billing routes — upgrade, subscribe (Razorpay hosted), cancel, webhook."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_user
from app.config import settings
from app.db import get_db
from app.models import User
from app.services import billing
from app.templating import templates

logger = logging.getLogger("app.billing.router")
router = APIRouter(tags=["billing"])


@router.get("/app/upgrade", response_class=HTMLResponse)
def upgrade_page(
    request: Request,
    error: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    current = billing.active_subscription(db, user.id)
    return templates.TemplateResponse(
        "app/upgrade.html",
        {
            "request": request,
            "user": user,
            "prices": billing.TIER_PRICE,
            "razorpay_enabled": settings.razorpay_enabled,
            "active_sub": current,
            "error": error,
        },
    )


@router.post("/billing/subscribe")
def subscribe(
    tier: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if tier not in billing.PAID_TIERS:
        return RedirectResponse("/app/upgrade?error=Unknown+plan", status_code=status.HTTP_303_SEE_OTHER)
    try:
        sub = billing.create_subscription(db, user, tier)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Subscription create failed: %s", exc)
        return RedirectResponse(
            "/app/upgrade?error=Checkout+is+not+available+right+now.+Please+try+again+soon.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    short_url = sub.get("short_url")
    if not short_url:
        return RedirectResponse("/app/upgrade?error=Could+not+start+checkout", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(short_url, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/billing/cancel")
def cancel(user: User = Depends(require_user), db: Session = Depends(get_db)):
    billing.cancel_active(db, user)
    return RedirectResponse("/app/upgrade", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/razorpay/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    handled = billing.handle_webhook(db, body, signature)
    return JSONResponse({"ok": handled}, status_code=200 if handled else 400)
