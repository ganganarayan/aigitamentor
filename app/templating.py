"""Shared Jinja2 templates instance."""

from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _dynamic_globals(request):
    """Per-render globals that come from the DB-backed settings (cached)."""
    from app.services import settings_store

    return {"meta_pixel_id": settings_store.get("meta_pixel_id") or ""}


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_dynamic_globals])


def _markdown_lite(text: str | None) -> Markup:
    """Escape, then render **bold** and *italic* only. Safe for LLM output."""
    if not text:
        return Markup("")
    s = html.escape(text)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", s)
    return Markup(s)


templates.env.filters["md"] = _markdown_lite

# Absolute origin of the gated app (the `app.` host). Public-site CTAs prefix
# auth/app links with this so users land on `app.` for sign-in. Empty locally →
# links resolve relative to the current host.
templates.env.globals["app_url"] = (settings.app_url or "").rstrip("/")
