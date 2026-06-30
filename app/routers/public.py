"""System A — public, crawlable surface + marketing landing.

robots.txt allows the AI crawlers on /learn/* and disallows /app, /admin, /api
(Section 9). sitemap.xml is generated from published articles. The /learn pages
themselves are built out in a later phase; the index renders whatever is
published so the route and crawler contract exist from day one.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.config import settings
from app.db import SessionLocal
from app.templating import templates

router = APIRouter(tags=["public"])

# Crawlers we explicitly welcome on the public knowledge graph.
ALLOWED_BOTS = [
    "GPTBot",
    "ClaudeBot",
    "Claude-Web",
    "PerplexityBot",
    "Google-Extended",
    "Googlebot",
    "Bingbot",
]


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {"request": request, "app_name": settings.app_name},
    )


# Policy / legal pages (linked from the footer). Explicit routes only — never a
# catch-all, which would shadow /login, /signup, /app, /robots.txt, etc.
def _legal(template: str):
    def _render(request: Request):
        return templates.TemplateResponse(template, {"request": request})

    return _render


router.add_api_route("/privacy", _legal("legal/privacy.html"), response_class=HTMLResponse, tags=["public"])
router.add_api_route("/terms", _legal("legal/terms.html"), response_class=HTMLResponse, tags=["public"])
router.add_api_route("/refund", _legal("legal/refund.html"), response_class=HTMLResponse, tags=["public"])
router.add_api_route("/shipping", _legal("legal/shipping.html"), response_class=HTMLResponse, tags=["public"])
router.add_api_route("/contact", _legal("legal/contact.html"), response_class=HTMLResponse, tags=["public"])


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    lines: list[str] = []
    for bot in ALLOWED_BOTS:
        lines.append(f"User-agent: {bot}")
        lines.append("Allow: /learn/")
        lines.append("Disallow: /app/")
        lines.append("Disallow: /admin/")
        lines.append("Disallow: /api/")
        lines.append("")
    # Default for any other agent: keep gated areas out of the index.
    lines.append("User-agent: *")
    lines.append("Allow: /learn/")
    lines.append("Disallow: /app/")
    lines.append("Disallow: /admin/")
    lines.append("Disallow: /api/")
    lines.append("")
    lines.append(f"Sitemap: {settings.app_base_url.rstrip('/')}/sitemap.xml")
    return "\n".join(lines) + "\n"


@router.get("/sitemap.xml")
def sitemap() -> Response:
    base = settings.app_base_url.rstrip("/")
    urls: list[str] = [f"{base}/"]

    if SessionLocal is not None:
        try:
            from sqlalchemy import select

            from app.models import PublicKbArticle

            with SessionLocal() as db:
                slugs = db.execute(
                    select(PublicKbArticle.slug).where(PublicKbArticle.published.is_(True))
                ).scalars()
                urls.extend(f"{base}/learn/{slug}" for slug in slugs)
        except Exception:  # noqa: BLE001 — sitemap must not 500 if DB is cold
            pass

    body = ['<?xml version="1.0" encoding="UTF-8"?>']
    body.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for url in urls:
        body.append(f"  <url><loc>{url}</loc></url>")
    body.append("</urlset>")
    return Response(content="\n".join(body), media_type="application/xml")
