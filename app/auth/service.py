"""User account operations backing auth."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password
from app.config import settings
from app.models import User


def _role_for(email: str) -> str:
    return "admin" if email.lower() in settings.admin_email_set else "user"


def get_by_email(db: Session, email: str) -> User | None:
    return db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()


def get_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def create_email_user(db: Session, email: str, password: str, name: str | None = None) -> User:
    email = email.lower()
    user = User(
        email=email,
        name=name,
        password_hash=hash_password(password),
        role=_role_for(email),
        tier="seeker",
        status="active",
        last_login_at=dt.datetime.now(tz=dt.timezone.utc),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = get_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return None
    _touch_login(db, user)
    return user


def upsert_oauth_user(
    db: Session, *, email: str, name: str | None, provider: str, subject: str
) -> User:
    """Create or update a user authenticated via an OAuth provider."""
    email = email.lower()
    user = get_by_email(db, email)
    if user is None:
        user = User(email=email, name=name, tier="seeker", status="active")
        db.add(user)
    user.oauth_provider = provider
    user.oauth_subject = subject
    if name and not user.name:
        user.name = name
    # Re-evaluate admin grant in case ADMIN_EMAILS changed.
    if user.role != "admin":
        user.role = _role_for(email)
    user.last_login_at = dt.datetime.now(tz=dt.timezone.utc)
    db.commit()
    db.refresh(user)
    return user


def _touch_login(db: Session, user: User) -> None:
    user.last_login_at = dt.datetime.now(tz=dt.timezone.utc)
    db.commit()
