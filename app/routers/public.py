"""System A — public, crawlable surface + marketing landing.

robots.txt allows the AI crawlers on /learn/* and disallows /app, /admin, /api
(Section 9). sitemap.xml is generated from published articles. The /learn pages
themselves are built out in a later phase; the index renders whatever is
published so the route and crawler contract exist from day one.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.config import settings
from app.db import SessionLocal
from app.models import Verse
from app.services import meta, public_kb
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
def landing(request: Request, background: BackgroundTasks):
    pv = meta.new_event_id()  # dedup id shared by client Pixel + server CAPI PageView
    background.add_task(meta.track_page_view, request, pv)
    return templates.TemplateResponse(
        "landing.html",
        {"request": request, "app_name": settings.app_name, "pv_event_id": pv},
    )


# Policy / legal pages (linked from the footer). Explicit routes only — never a
# catch-all, which would shadow /login, /signup, /app, /robots.txt, etc.
def _legal(template: str):
    def _render(request: Request):
        return templates.TemplateResponse(template, {"request": request})

    return _render


# Public Knowledge Graph — crawlable /learn (Phase 7). Seeker-depth only; built
# from published seeker answers, meant to be cited. pgvector is never exposed here.
@router.get("/learn", response_class=HTMLResponse)
def learn_index(request: Request):
    articles = []
    if SessionLocal is not None:
        try:
            with SessionLocal() as db:
                articles = public_kb.published_articles(db)
        except Exception:  # noqa: BLE001 — a public page must not 500 on a cold DB
            articles = []
    return templates.TemplateResponse(
        "learn/index.html",
        {"request": request, "app_name": settings.app_name, "articles": articles,
         "app_base_url": settings.app_base_url.rstrip("/")},
    )


@router.get("/learn/{slug}", response_class=HTMLResponse)
def learn_article(slug: str, request: Request):
    article = verse = None
    related: list = []
    if SessionLocal is not None:
        try:
            with SessionLocal() as db:
                article = public_kb.get_published(db, slug)
                if article is not None:
                    verse = db.get(Verse, article.primary_verse_id) if article.primary_verse_id else None
                    related = [r for r in public_kb.published_articles(db, limit=7) if r.id != article.id][:6]
                db.expunge_all()  # detach for template use after the session closes
        except Exception:  # noqa: BLE001
            article = None
    if article is None:
        return templates.TemplateResponse(
            "learn/not_found.html", {"request": request}, status_code=404
        )
    return templates.TemplateResponse(
        "learn/article.html",
        {"request": request, "a": article, "verse": verse, "related": related,
         "app_url": (settings.app_url or settings.app_base_url).rstrip("/"),
         "app_base_url": settings.app_base_url.rstrip("/")},
    )


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
