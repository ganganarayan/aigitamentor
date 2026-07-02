"""Database engine, session, and connectivity helpers.

Schema is owned by Alembic migrations (see migrations/). The container runs
``alembic upgrade head`` before the web process starts, so the app itself never
creates tables — it only opens sessions and reports readiness.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

logger = logging.getLogger("app.db")


def _normalize_url(url: str) -> str:
    """Coerce a Railway/Heroku-style URL onto the psycopg (v3) driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine() -> Engine | None:
    if not settings.database_url:
        logger.warning("DATABASE_URL is not set — running without a database.")
        return None
    # Bounded connection pool (never one raw connection per request). pre_ping
    # revalidates a connection before use and pool_recycle drops connections
    # Railway/Postgres may have closed while idle — both prevent stale-connection
    # errors under real traffic. ~15 concurrent DB ops per replica is ample for an
    # I/O-bound async-served app; add replicas, not pool size, to scale.
    return create_engine(
        _normalize_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=1800,
        echo=settings.debug,
        future=True,
    )


engine: Engine | None = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True) if engine else None


def get_db():
    """FastAPI dependency yielding a scoped session."""
    if SessionLocal is None:
        raise RuntimeError("Database is not configured (DATABASE_URL missing).")
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ping() -> bool:
    """Lightweight readiness probe."""
    if engine is None:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


def startup_db_check() -> None:
    """Log database reachability at boot. Never raises — migrations run in the
    start command, so a transient blip here must not stop the web process."""
    if engine is None:
        logger.warning("No database configured at startup.")
        return
    logger.info("Database reachable: %s", ping())
