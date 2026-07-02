"""Local account authentication endpoints."""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import auth
import db
import mailer
from ratelimit import RateLimiter

router = APIRouter()

# Per-process limiter for the unauthenticated reset endpoints (see ratelimit.py).
rate_limiter = RateLimiter()


def _rate_limit(request: Request, kind: str, default_max: int, default_window: int) -> None:
    """Throttle an endpoint by client IP. Limits configurable via
    AUTH_<KIND>_MAX / AUTH_<KIND>_WINDOW_SECONDS env vars."""
    limit = int(os.environ.get(f"AUTH_{kind.upper()}_MAX", str(default_max)) or default_max)
    window = int(os.environ.get(f"AUTH_{kind.upper()}_WINDOW_SECONDS", str(default_window)) or default_window)
    client = getattr(request, "client", None)
    ip = (getattr(client, "host", None) if client else None) or "unknown"
    if not rate_limiter.allow(f"{kind}:{ip}", limit, window):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")


class AuthRequest(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None


class ClaimSessionRequest(BaseModel):
    session_id: str
    claim_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class UpdateEmailRequest(BaseModel):
    email: Optional[str] = None
    current_password: str


def _validate_new_password(password: str, settings: auth.AuthSettings) -> None:
    if len(password or "") < settings.password_min_length:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {settings.password_min_length} characters",
        )


def _auth_payload(user: Optional[dict]) -> dict:
    settings = auth.get_auth_settings()
    return {
        "auth_enabled": settings.enabled,
        "allow_signup": settings.allow_signup,
        "user": auth.public_user(user),
    }


def _create_login_session(response: Response, request: Request, user: dict) -> None:
    settings = auth.get_auth_settings()
    token = auth.new_token()
    db.create_auth_session(
        user_id=user["id"],
        token_hash=auth.token_hash(token),
        ttl_days=settings.session_ttl_days,
        user_agent=request.headers.get("user-agent"),
    )
    db.mark_user_login(user["id"])
    auth.set_auth_cookie(response, token)


@router.get("/auth/me")
def me(request: Request):
    return _auth_payload(auth.get_current_user_optional(request))


@router.post("/auth/signup")
def signup(req: AuthRequest, request: Request, response: Response):
    settings = auth.get_auth_settings()
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Local auth is disabled")
    if not settings.allow_signup:
        raise HTTPException(status_code=403, detail="Signup is disabled")

    username = auth.normalize_username(req.username)
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    _validate_new_password(req.password, settings)
    try:
        email = auth.normalize_email(req.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        user = db.create_user(
            username=username,
            password_hash=auth.hash_password(req.password),
            display_name=req.display_name,
            email=email,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="username or email already in use")

    _create_login_session(response, request, user)
    return _auth_payload(user)


@router.post("/auth/login")
def login(req: AuthRequest, request: Request, response: Response):
    settings = auth.get_auth_settings()
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Local auth is disabled")

    username = auth.normalize_username(req.username)
    user = db.get_user_by_username(username)
    if not user or user.get("disabled") or not auth.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")

    db.delete_expired_auth_sessions()
    _create_login_session(response, request, user)
    return _auth_payload(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    settings = auth.get_auth_settings()
    token = request.cookies.get(settings.cookie_name)
    if token:
        db.delete_auth_session(auth.token_hash(token))
    auth.clear_auth_cookie(response)
    return {"ok": True}


@router.post("/auth/change-password")
def change_password(req: ChangePasswordRequest, request: Request):
    """Authenticated self-service password change (current + new)."""
    settings = auth.get_auth_settings()
    user = auth.require_current_user(request)
    full = db.get_user_by_username(user["username"])
    if not full or not auth.verify_password(req.current_password, full["password_hash"]):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    _validate_new_password(req.new_password, settings)

    db.set_user_password(user["id"], auth.hash_password(req.new_password))
    # Revoke other sessions but keep the caller logged in on this browser.
    token = request.cookies.get(settings.cookie_name)
    db.delete_user_auth_sessions(
        user["id"], except_token_hash=auth.token_hash(token) if token else None
    )
    return {"ok": True}


@router.post("/auth/update-email")
def update_email(req: UpdateEmailRequest, request: Request):
    """Set/clear the current user's email (needed for password-reset delivery).

    Requires the current password — email is the reset channel, so changing it is
    security-relevant and must not be possible from a bare hijacked session."""
    user = auth.require_current_user(request)
    full = db.get_user_by_username(user["username"])
    if not full or not auth.verify_password(req.current_password, full["password_hash"]):
        raise HTTPException(status_code=400, detail="current password is incorrect")
    try:
        email = auth.normalize_email(req.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        db.set_user_email(user["id"], email)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="email already in use")
    return _auth_payload(db.get_user_by_id(user["id"]))


@router.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Begin an email-based reset. Always returns the same response regardless of
    whether the email maps to an account (no user/email enumeration)."""
    settings = auth.get_auth_settings()
    generic = {"ok": True, "message": "If an account with that email exists, a reset link has been sent."}
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Local auth is disabled")
    _rate_limit(request, "forgot", default_max=5, default_window=900)
    try:
        email = auth.normalize_email(req.email)
    except ValueError:
        return generic  # malformed email — don't reveal anything
    if not email:
        return generic

    user = db.get_user_by_email(email)
    if user and not user.get("disabled"):
        token = auth.new_token()
        db.create_password_reset_token(
            user["id"], auth.token_hash(token), settings.reset_token_ttl_minutes
        )
        reset_url = f"{settings.app_base_url}/reset-password?token={token}"
        mailer.send_password_reset_email(email, reset_url, settings.reset_token_ttl_minutes)
    return generic


@router.post("/auth/reset-password")
def reset_password(req: ResetPasswordRequest, request: Request):
    """Complete an email-based reset using the emailed token."""
    settings = auth.get_auth_settings()
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Local auth is disabled")
    _rate_limit(request, "reset", default_max=10, default_window=900)
    _validate_new_password(req.new_password, settings)

    token_hash = auth.token_hash(req.token or "")
    user_id = db.reset_password_with_token(token_hash, auth.hash_password(req.new_password))
    if not user_id:
        raise HTTPException(status_code=400, detail="reset link is invalid or has expired")
    return {"ok": True}


@router.post("/auth/claim-session")
def claim_session(req: ClaimSessionRequest, request: Request):
    user = auth.require_current_user(request)
    ok = db.claim_session(
        req.session_id,
        user["id"],
        auth.token_hash(req.claim_token),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True, "session_id": req.session_id}
