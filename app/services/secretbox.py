"""Encryption for every secret stored in the DB settings (shared).

One place derives the cipher so provider keys, integration secrets, etc. are all
encrypted the same way. The primary key comes from ``SETTINGS_ENCRYPTION_KEY``;
if that isn't set we fall back to ``JWT_SECRET`` (so a fresh deploy still works
and, crucially, values previously encrypted with the JWT-derived key stay
readable). Decryption uses a MultiFernet over [primary, JWT-legacy], so rotating
to a dedicated key is seamless — old values decrypt, new writes use the primary,
and everything re-encrypts to the primary on its next save.

Never log a decrypted value. Legacy plaintext (no prefix) is read transparently
and re-encrypted on the next write.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings as env

logger = logging.getLogger("app.secretbox")

ENC_PREFIX = "enc:v1:"


def _fernet_from(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256((secret or "").encode("utf-8")).digest()))


def primary_source() -> str:
    """The secret the cipher is derived from: SETTINGS_ENCRYPTION_KEY if set, else
    JWT_SECRET (keeps a keyless deploy working)."""
    return env.settings_encryption_key or env.jwt_secret


def using_dedicated_key() -> bool:
    return bool(env.settings_encryption_key)


def _cipher() -> MultiFernet:
    # Encrypt with the primary; also keep JWT_SECRET in the decrypt set so values
    # written before a dedicated key existed still open (and so changing the env
    # var later never breaks anything already keyed to JWT_SECRET).
    ciphers = []
    seen: set[str] = set()
    for k in (primary_source(), env.jwt_secret):
        if k and k not in seen:
            ciphers.append(_fernet_from(k))
            seen.add(k)
    return MultiFernet(ciphers or [_fernet_from(env.jwt_secret)])


def encrypt(value: str) -> str:
    return ENC_PREFIX + _cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(ENC_PREFIX):
        try:
            return _cipher().decrypt(value[len(ENC_PREFIX):].encode("utf-8")).decode("utf-8")
        except InvalidToken:
            logger.warning("A stored secret failed to decrypt (SETTINGS_ENCRYPTION_KEY/JWT_SECRET changed?).")
            return None
    return value  # legacy plaintext — readable, re-encrypted on next save
