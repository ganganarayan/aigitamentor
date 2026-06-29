"""FastAPI application entrypoint — the modular monolith.

Internal modules (auth, knowledge studio, mentor chat, public KG, billing) are
mounted here as routers. They share one process and one database; they are NOT
independently deployed services (Section 0).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.admin import router as admin_router
from app.auth import router as auth_router
from app.auth.deps import RedirectToLogin
from app.config import settings
from app.db import startup_db_check
from app.mentor import router as mentor_router
from app.routers import health, public

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s v%s (%s)", settings.app_name, __version__, settings.environment)
    startup_db_check()  # schema is applied by `alembic upgrade head` in the start command
    yield
    logger.info("Shutting down %s", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="The digital extension of Ganga Narayan Das — Gita wisdom via the Neuro-Acoustic Protocol.",
    lifespan=lifespan,
    docs_url="/api/docs" if not settings.is_production else None,
    redoc_url=None,
)

# Static assets (created lazily so a missing dir never breaks boot).
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Bounce unauthenticated browsers to the login page (preserving the target).
@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)


# Routers — public surface, auth, gated mentor, and the admin Knowledge Studio.
app.include_router(health.router)
app.include_router(public.router)
app.include_router(auth_router.router)
app.include_router(mentor_router.router)
app.include_router(admin_router.router)
