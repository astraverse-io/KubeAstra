"""Local username/password authentication helpers.

Auth is intentionally self-contained: users and auth sessions live in SQLite,
passwords are bcrypt-hashed, and browsers authenticate with an HttpOnly cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, Response

import db


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    allow_signup: bool
    session_ttl_days: int
    cookie_name: str
    cookie_secure: bool
    password_min_length: int
    allowed_origins: tuple[str, ...]
    reset_token_ttl_minutes: int
    app_base_url: str


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_auth_settings() -> AuthSettings:
    allowed = tuple(
        origin.strip().rstrip("/")
        for origin in os.environ.get("AUTH_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    return AuthSettings(
        enabled=_env_bool("AUTH_ENABLED", False),
        allow_signup=_env_bool("AUTH_ALLOW_SIGNUP", False),
        session_ttl_days=max(1, int(os.environ.get("AUTH_SESSION_TTL_DAYS", "14") or "14")),
        cookie_name=os.environ.get("AUTH_COOKIE_NAME", "k8s_devops_auth"),
        cookie_secure=_env_bool("AUTH_COOKIE_SECURE", False),
        password_min_length=max(1, int(os.environ.get("AUTH_PASSWORD_MIN_LENGTH", "12") or "12")),
        allowed_origins=allowed,
        reset_token_ttl_minutes=max(1, int(os.environ.get("AUTH_RESET_TOKEN_TTL_MINUTES", "30") or "30")),
        app_base_url=os.environ.get("APP_BASE_URL", "http://localhost:3000").rstrip("/"),
    )


def auth_enabled() -> bool:
    return get_auth_settings().enabled


def normalize_username(username: str) -> str:
    return " ".join((username or "").strip().lower().split())


def public_user(user: Optional[dict]) -> Optional[dict]:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "display_name": user.get("display_name"),
        "role": user.get("role", "user"),
        "email": user.get("email"),
    }


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{1,63}$")


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Trim/lowercase an email and validate a basic shape. Returns None if blank,
    raises ValueError if non-blank but malformed."""
    if email is None:
        return None
    value = email.strip().lower()
    if not value:
        return None
    if not _EMAIL_RE.match(value):
        raise ValueError("invalid email address")
    return value


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_auth_cookie(response: Response, token: str) -> None:
    settings = get_auth_settings()
    response.set_cookie(
        settings.cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    settings = get_auth_settings()
    response.delete_cookie(settings.cookie_name, path="/", samesite="lax")


def user_from_request(request: Request) -> Optional[dict]:
    settings = get_auth_settings()
    token = request.cookies.get(settings.cookie_name)
    if not token:
        return None
    user = db.get_user_for_auth_token(token_hash(token))
    return public_user(user)


def get_current_user_optional(request: Request) -> Optional[dict]:
    cached = getattr(request.state, "user", None)
    if cached:
        return cached
    user = user_from_request(request)
    if user:
        request.state.user = user
    return user


def require_current_user(request: Request) -> dict:
    if not auth_enabled():
        return {}
    user = get_current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_owned_session(request: Request, session_id: Optional[str]) -> Optional[dict]:
    if not auth_enabled():
        return None
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    user = require_current_user(request)
    if not db.user_owns_session(session_id, user["id"]):
        raise HTTPException(status_code=404, detail="Session not found")
    return user


def is_admin(user: Optional[dict]) -> bool:
    return bool(user and user.get("role") == "admin")


def require_session_read_access(request: Request, session_id: Optional[str]) -> dict:
    """Allow owners to read their session and admins to read any active session."""
    if not auth_enabled():
        return {
            "user": None,
            "session": None,
            "access_mode": "owned",
            "readonly": False,
        }
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    user = require_current_user(request)
    session = db.get_session_metadata(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.get("owner_user_id") == user["id"]:
        return {
            "user": user,
            "session": session,
            "access_mode": "owned",
            "readonly": False,
        }

    if is_admin(user):
        return {
            "user": user,
            "session": session,
            "access_mode": "admin_readonly",
            "readonly": True,
        }

    raise HTTPException(status_code=404, detail="Session not found")


def is_public_path(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    public_exact = {
        "/health",
        "/api/health",
        "/api/auth/login",
        "/api/auth/signup",
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
        "/api/models",
        # Machine-to-machine alert ingestion (Alertmanager/Grafana/Loki). Exempt
        # from interactive user-session auth; secured by ALERT_WEBHOOK_TOKEN in
        # the alerts router instead.
        "/api/v1/alerts/webhook",
        # Prometheus scrape — public on the assumption the cluster network is
        # trusted; restrict via NetworkPolicy if scrapers are outside the cluster.
        "/api/v1/metrics",
        "/api/agent-runs/prune",
        # Registered only when AGENT_API_TOKEN is configured; the router
        # performs its own bearer-token authentication.
        "/api/v1/agent/invoke",
    }
    return path in public_exact


def origin_allowed(request: Request) -> bool:
    settings = get_auth_settings()
    if request.method.upper() in SAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    if not settings.allowed_origins:
        # Enforce only when configured so local/proxy deployments do not break
        # unexpectedly. Helm/docs should set this for shared deployments.
        return True
    return any(hmac.compare_digest(origin.rstrip("/"), allowed) for allowed in settings.allowed_origins)
