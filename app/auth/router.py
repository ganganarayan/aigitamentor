"""Auth routes: email/password + Google OAuth (Section 13).

Server-rendered forms set an HTTP-only session cookie on success. Google OAuth
is the authorization-code flow; it is only wired when GOOGLE_OAUTH_* is present,
otherwise the button is hidden and the routes return a friendly notice.
"""

from __future__ import annotations

import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from app.auth import service
from app.auth.deps import current_user
from app.auth.security import COOKIE_NAME, create_session_token
from app.config import settings
from app.db import get_db
from app.models import User
from app.services import meta
from app.templating import templates

router = APIRouter(tags=["auth"])

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_OAUTH_STATE_COOKIE = "agm_oauth_state"


def set_session_cookie(response: Response, user: User) -> None:
    token = create_session_token(user.id, user.email, user.role)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path="/",
    )


def _safe_next(next_url: str | None) -> str:
    # Only allow same-site relative paths to avoid open-redirects.
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/app"


# --- Email / password -------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/app", error: str | None = None):
    return templates.TemplateResponse(
        "auth/login.html",
        {
            "request": request,
            "next": _safe_next(next),
            "error": error,
            "google_enabled": settings.google_oauth_enabled,
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/app"),
    db: Session = Depends(get_db),
):
    user = service.authenticate(db, email, password)
    if user is None:
        return templates.TemplateResponse(
            "auth/login.html",
            {
                "request": request,
                "next": _safe_next(next),
                "error": "Invalid email or password.",
                "google_enabled": settings.google_oauth_enabled,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    resp = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(resp, user)
    return resp


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, next: str = "/app", error: str | None = None):
    return templates.TemplateResponse(
        "auth/signup.html",
        {
            "request": request,
            "next": _safe_next(next),
            "error": error,
            "google_enabled": settings.google_oauth_enabled,
        },
    )


@router.post("/signup")
def signup_submit(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(""),
    phone: str = Form(""),
    referral: str = Form(""),
    referrer: str = Form(""),
    next: str = Form("/app"),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    error = None
    if "@" not in email or len(password) < 8:
        error = "Enter a valid email and a password of at least 8 characters."
    elif not phone.strip():
        error = "Please enter your phone number."
    elif service.get_by_email(db, email):
        error = "An account with that email already exists. Try logging in."
    if error:
        return templates.TemplateResponse(
            "auth/signup.html",
            {
                "request": request,
                "next": _safe_next(next),
                "error": error,
                "google_enabled": settings.google_oauth_enabled,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    # AI-referral source: the self-report field, else derive from the referrer.
    source = (referral.strip() or _referral_from_referrer(referrer)) or None
    user = service.create_email_user(db, email, password, name.strip() or None, phone.strip() or None, source)
    resp = RedirectResponse(_welcome_dest(next, request, user, background), status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(resp, user)
    return resp


def _welcome_dest(next: str, request: Request, user: User, background: BackgroundTasks) -> str:
    """Fire deduped CompleteRegistration + StartTrial (CAPI) and build the welcome
    redirect carrying the matching event_ids for the client Pixel."""
    reg_id = meta.new_event_id()
    trial_id = meta.new_event_id()
    background.add_task(
        meta.track_complete_registration, user.email, request=request, phone=user.phone, external_id=user.id, event_id=reg_id
    )
    background.add_task(
        meta.track_start_trial, user.email, request=request, phone=user.phone, external_id=user.id, event_id=trial_id
    )
    dest = _safe_next(next)
    sep = "&" if "?" in dest else "?"
    return f"{dest}{sep}welcome=1&reg={reg_id}&trial={trial_id}"


_AI_REFERRERS = {
    "chatgpt": "ChatGPT", "openai": "ChatGPT", "claude": "Claude", "anthropic": "Claude",
    "gemini": "Gemini", "google": "Google", "perplexity": "Perplexity", "bing": "Bing", "grok": "Grok",
}


def _referral_from_referrer(referrer: str) -> str | None:
    r = (referrer or "").lower()
    for needle, label in _AI_REFERRERS.items():
        if needle in r:
            return label
    return None


@router.post("/logout")
@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- Google OAuth -----------------------------------------------------------

def _state_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.jwt_secret, salt="google-oauth-state")


@router.get("/auth/google/login")
def google_login(next: str = "/app"):
    if not settings.google_oauth_enabled:
        return RedirectResponse("/login?error=Google+sign-in+is+not+configured+yet.")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    url = f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
    resp = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    signed = _state_serializer().dumps({"state": state, "next": _safe_next(next)})
    resp.set_cookie(
        _OAUTH_STATE_COOKIE, signed, max_age=600, httponly=True,
        secure=settings.is_production, samesite="lax", path="/",
    )
    return resp


@router.get("/auth/google/callback")
async def google_callback(
    request: Request,
    background: BackgroundTasks,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    if not settings.google_oauth_enabled:
        return RedirectResponse("/login?error=Google+sign-in+is+not+configured+yet.")
    cookie = request.cookies.get(_OAUTH_STATE_COOKIE)
    if not code or not state or not cookie:
        return RedirectResponse("/login?error=Sign-in+was+interrupted.+Please+try+again.")
    try:
        data = _state_serializer().loads(cookie)
    except BadSignature:
        return RedirectResponse("/login?error=Sign-in+verification+failed.")
    if data.get("state") != state:
        return RedirectResponse("/login?error=Sign-in+verification+failed.")

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            return RedirectResponse("/login?error=Google+sign-in+failed.")
        access_token = token_resp.json().get("access_token")
        info_resp = await client.get(
            _GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if info_resp.status_code != 200:
            return RedirectResponse("/login?error=Could+not+read+Google+profile.")
        info = info_resp.json()

    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified", True):
        return RedirectResponse("/login?error=Your+Google+email+is+not+verified.")
    was_new = service.get_by_email(db, email) is None
    user = service.upsert_oauth_user(
        db, email=email, name=info.get("name"), provider="google", subject=info.get("sub", "")
    )
    # New Google signup = registration + start of trial (deduped CAPI + Pixel).
    dest = _welcome_dest(data.get("next"), request, user, background) if was_new else _safe_next(data.get("next"))
    resp = RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(resp, user)
    resp.delete_cookie(_OAUTH_STATE_COOKIE, path="/")
    return resp


@router.get("/me")
def me(user: User | None = Depends(current_user)):
    if user is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "tier": user.tier,
    }
