"""Billing routes — upgrade, subscribe (Razorpay hosted), cancel, webhook."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_user
from app.db import get_db
from app.models import User
from app.services import billing, settings_store
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
            "razorpay_enabled": settings_store.razorpay_enabled(db),
            "active_sub": current,
            "error": error,
        },
    )


@router.post("/billing/subscribe")
def subscribe(
    request: Request,
    tier: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if tier not in billing.PAID_TIERS:
        return RedirectResponse("/app/upgrade?error=Unknown+plan", status_code=status.HTTP_303_SEE_OTHER)
    try:
        order = billing.create_order(db, user, tier)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Order create failed: %s", exc)
        if getattr(user, "role", "") == "admin":  # admins see the real error
            from urllib.parse import quote

            msg = quote(f"[admin] {exc}")
        else:
            msg = "Checkout+is+not+available+right+now.+Please+try+again+soon."
        return RedirectResponse(f"/app/upgrade?error={msg}", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        "app/checkout.html",
        {
            "request": request, "user": user, "tier": tier,
            "order_id": order.get("id"), "amount": order.get("amount"),
            "key_id": settings_store.get("razorpay_key_id", db),
        },
    )


@router.post("/billing/verify")
async def verify(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    form = await request.form()
    ok, result = billing.verify_and_activate(
        db, user,
        order_id=str(form.get("razorpay_order_id", "")),
        payment_id=str(form.get("razorpay_payment_id", "")),
        signature=str(form.get("razorpay_signature", "")),
    )
    if ok:
        return RedirectResponse("/app?welcome=1", status_code=status.HTTP_303_SEE_OTHER)
    from urllib.parse import quote

    return RedirectResponse(f"/app/upgrade?error={quote(result)}", status_code=status.HTTP_303_SEE_OTHER)


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
