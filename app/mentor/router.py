"""Gated AI Mentor routes (authenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.deps import require_user
from app.models import User
from app.templating import templates

router = APIRouter(tags=["mentor"])


@router.get("/app", response_class=HTMLResponse)
def app_home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        "app/home.html", {"request": request, "user": user}
    )
