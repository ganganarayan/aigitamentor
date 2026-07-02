"""FastAPI application entrypoint — the modular monolith.

Internal modules (auth, knowledge studio, mentor chat, public KG, billing) are
mounted here as routers. They share one process and one database; they are NOT
independently deployed services (Section 0).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.admin import router as admin_router
from app.auth import router as auth_router
from app.auth.deps import RedirectToLogin
from app.billing import router as billing_router
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

# Host split: marketing/KB on the public host (`ai.`), auth + gated app on
# `app.`. They are one service on two hosts; this router keeps each host to its
# job. No-op when APP_URL is unset (single-host local dev).
#   - public host (ai.): gated paths      -> same path on app host
#   - app host   (app.): marketing root   -> /app (the product, not the landing)
#                        legal/marketing  -> same path on the public host
_PUBLIC_HOST = (urlparse(settings.app_base_url).hostname or "").lower()
_APP_HOST = (urlparse(settings.app_url).hostname or "").lower() if settings.app_url else ""
_GATED_PREFIXES = ("/app", "/admin", "/login", "/signup", "/logout", "/me", "/auth", "/api")
_PUBLIC_PATHS = ("/privacy", "/terms", "/refund", "/shipping", "/contact")


def _with_query(base: str, request: Request) -> str:
    return base + (("?" + request.url.query) if request.url.query else "")


@app.middleware("http")
async def host_router(request: Request, call_next):
    if settings.app_url and _PUBLIC_HOST:
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.path
        if host == _PUBLIC_HOST:
            # Gated path on the public host → bounce to the app host.
            if any(path == p or path.startswith(p + "/") for p in _GATED_PREFIXES):
                return RedirectResponse(
                    _with_query(settings.app_url.rstrip("/") + path, request), status_code=308
                )
        elif _APP_HOST and host == _APP_HOST:
            # The app host is the product, not the marketing site.
            if path == "/":
                return RedirectResponse("/app", status_code=307)
            if path in _PUBLIC_PATHS or path == "/learn" or path.startswith("/learn/"):
                return RedirectResponse(settings.app_base_url.rstrip("/") + path, status_code=308)
    return await call_next(request)


# Cache-Control policy (CDN habit). The gated app is personalized and must NEVER
# be cached by any CDN/proxy; the public KB (/learn) is safe to cache and benefits
# from a CDN in front of the `ai.` host. The landing page is left uncached because
# it embeds a per-request Meta Pixel event_id (a shared cache entry would collapse
# distinct PageViews into one via dedup).
_NOSTORE_PATHS = (
    "/app", "/admin", "/api", "/auth", "/billing", "/login", "/signup", "/logout", "/me",
)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _NOSTORE_PATHS):
        response.headers["Cache-Control"] = "private, no-store"
    elif path == "/learn" or path.startswith("/learn/"):
        response.headers.setdefault("Cache-Control", "public, max-age=300, s-maxage=3600")
    return response


# Bounce unauthenticated browsers to the login page (preserving the target).
@app.exception_handler(RedirectToLogin)
async def _redirect_to_login(request: Request, exc: RedirectToLogin):
    return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)


# Routers — public surface, auth, gated mentor, and the admin Knowledge Studio.
app.include_router(health.router)
app.include_router(public.router)
app.include_router(auth_router.router)
app.include_router(mentor_router.router)
app.include_router(billing_router.router)
app.include_router(admin_router.router)
