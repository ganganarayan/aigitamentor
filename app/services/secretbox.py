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


def _dedicated_keys() -> list[str]:
    """SETTINGS_ENCRYPTION_KEY, parsed as a comma-separated list. The FIRST is the
    primary (used to encrypt new writes); any others are kept only for decryption.
    This is what makes rotation / platform migration safe: run the new platform
    with ``NEW,OLD`` set, boot, hit 'Re-encrypt' in admin, then drop OLD — no value
    is ever stranded."""
    raw = env.settings_encryption_key or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def primary_source() -> str:
    """The secret the primary cipher is derived from. First dedicated key if set,
    else JWT_SECRET (keeps existing data readable and a keyless deploy working)."""
    keys = _dedicated_keys()
    return keys[0] if keys else env.jwt_secret


def using_dedicated_key() -> bool:
    return bool(_dedicated_keys())


def _cipher() -> MultiFernet:
    ciphers = []
    seen: set[str] = set()
    # Primary first, then any additional dedicated keys (decrypt-only), then the
    # JWT-derived legacy cipher so values written before a dedicated key still open.
    for k in _dedicated_keys() + [env.jwt_secret]:
        if k and k not in seen:
            ciphers.append(_fernet_from(k))
            seen.add(k)
    return MultiFernet(ciphers or [_fernet_from(env.jwt_secret)])


def reencrypt(stored: str | None) -> str | None:
    """Decrypt with any available key and re-encrypt with the current primary.
    Returns the value unchanged if it can't be decrypted — never destroys a value
    we can't read (so a wrong key can't cascade into data loss)."""
    if not stored:
        return stored
    plain = decrypt(stored)
    if plain is None:
        return stored
    return encrypt(plain)


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
