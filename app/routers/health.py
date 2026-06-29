"""Liveness and readiness probes.

``/healthz`` must never touch the database — it is the Railway health check and
has to pass even before the DB env is wired. ``/readyz`` reports DB reachability.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.db import ping

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "ai-gita-mentor", "version": __version__}


@router.get("/readyz")
def readyz() -> dict:
    db_ok = ping()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok}
