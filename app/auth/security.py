"""Password hashing and JWT session tokens.

bcrypt is used directly (not via passlib) to avoid the passlib/bcrypt-4 version
shim noise and to keep the dependency surface honest. bcrypt caps input at 72
bytes, so we pre-hash nothing — we just truncate defensively at the byte level.
"""

from __future__ import annotations

import datetime as dt

import bcrypt
import jwt

from app.config import settings

COOKIE_NAME = "agm_session"
_MAX_BCRYPT_BYTES = 72


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_MAX_BCRYPT_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        pw = password.encode("utf-8")[:_MAX_BCRYPT_BYTES]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_session_token(user_id: int, email: str, role: str) -> str:
    now = dt.datetime.now(tz=dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_session_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
