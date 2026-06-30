"""Shared Jinja2 templates instance."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import settings

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Absolute origin of the gated app (the `app.` host). Public-site CTAs prefix
# auth/app links with this so users land on `app.` for sign-in. Empty locally →
# links resolve relative to the current host.
templates.env.globals["app_url"] = (settings.app_url or "").rstrip("/")
