"""In-memory cache for the DB settings rows (ai_runtime, app_config, escalation).

Every request that needs a setting reads from here, not the DB — this matters for
the per-turn Anthropic-key lookup and the Razorpay webhook's synchronous signature
check. Writes call ``invalidate`` so a change in Admin → Settings takes effect on
the next call (including a key rotation mid-retry, since the client is rebuilt per
call from this cache). A short TTL also lets changes propagate to other replicas
without a restart.

Callers that mutate the returned dict must copy it first — the cached object is
shared.
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import select

from app.models import Setting

_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def get(key: str, db=None) -> dict:
    """Return the settings dict for ``key`` (empty if unset). Uses ``db`` if given,
    else opens a short-lived session on a cache miss."""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and (now - hit[0]) < _TTL_SECONDS:
            return hit[1]

    own = db is None
    session = None
    try:
        if own:
            from app.db import SessionLocal

            if SessionLocal is None:
                return {}
            session = SessionLocal()
        else:
            session = db
        row = session.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
        val = dict(row.value) if row and isinstance(row.value, dict) else {}
    except Exception:  # noqa: BLE001 — a settings read must never crash a request
        return {}
    finally:
        if own and session is not None:
            session.close()

    with _lock:
        _cache[key] = (now, val)
    return val


def invalidate(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def preload(keys: list[str], db=None) -> None:
    for k in keys:
        try:
            get(k, db)
        except Exception:  # noqa: BLE001
            pass
