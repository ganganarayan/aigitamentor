"""FastAPI auth dependencies.

``current_user`` resolves the session cookie to a User (or None). ``require_user``
and ``require_admin`` enforce access — the latter gates the whole /admin surface.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.security import COOKIE_NAME, decode_session_token
from app.auth.service import get_by_id
from app.db import get_db
from app.models import User


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    payload = decode_session_token(token)
    if not payload:
        return None
    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        return None
    user = get_by_id(db, user_id)
    if user is None or user.status != "active":
        return None
    # Auto-downgrade once an admin-set/subscription plan end date has passed.
    if user.tier != "seeker" and user.plan_expires_at is not None:
        import datetime as _dt

        if user.plan_expires_at < _dt.datetime.now(tz=_dt.timezone.utc):
            user.tier = "seeker"
            db.commit()
    return user


class RedirectToLogin(HTTPException):
    """Raised to bounce an unauthenticated browser to the login page."""

    def __init__(self, next_url: str = "/"):
        super().__init__(status_code=status.HTTP_307_TEMPORARY_REDIRECT)
        self.next_url = next_url


def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise RedirectToLogin(next_url=str(request.url.path))
    return user


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise RedirectToLogin(next_url=str(request.url.path))
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return user
