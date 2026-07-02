"""Tests for self-service password change and email-based password reset."""

from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import auth  # noqa: E402
import db  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from routers import auth as auth_router  # noqa: E402
import mailer  # noqa: E402


def _init(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "reset-test.db"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    auth_router.rate_limiter.clear()  # avoid cross-test contamination of the limiter
    db.init_db()


def _authed_request(user, token):
    return SimpleNamespace(
        state=SimpleNamespace(user=auth.public_user(user)),
        cookies={auth.get_auth_settings().cookie_name: token},
    )


def _anon_request(ip="1.2.3.4"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def _make_user(username="alice", password="correct horse battery", email=None):
    user = db.create_user(username=username, password_hash=auth.hash_password(password), email=email)
    return user


# ── self-service change ───────────────────────────────────────────────────────

def test_change_password_updates_hash_and_revokes_other_sessions(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123")
    cur_token, other_token = auth.new_token(), auth.new_token()
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(cur_token), ttl_days=14)
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(other_token), ttl_days=14)

    auth_router.change_password(
        auth_router.ChangePasswordRequest(current_password="old-password-123", new_password="brand-new-password-9"),
        _authed_request(user, cur_token),
    )

    stored = db.get_user_by_username("alice")
    assert auth.verify_password("brand-new-password-9", stored["password_hash"]) is True
    assert auth.verify_password("old-password-123", stored["password_hash"]) is False
    # Current session kept, the other revoked.
    assert db.get_user_for_auth_token(auth.token_hash(cur_token)) is not None
    assert db.get_user_for_auth_token(auth.token_hash(other_token)) is None


def test_change_password_rejects_wrong_current(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123")
    token = auth.new_token()
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(token), ttl_days=14)
    with pytest.raises(HTTPException) as exc:
        auth_router.change_password(
            auth_router.ChangePasswordRequest(current_password="WRONG", new_password="brand-new-password-9"),
            _authed_request(user, token),
        )
    assert exc.value.status_code == 400


def test_change_password_rejects_short_new(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123")
    token = auth.new_token()
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(token), ttl_days=14)
    with pytest.raises(HTTPException) as exc:
        auth_router.change_password(
            auth_router.ChangePasswordRequest(current_password="old-password-123", new_password="short"),
            _authed_request(user, token),
        )
    assert exc.value.status_code == 400


# ── email handling ──────────────────────────────────────────────────────────

def test_email_unique_and_lookup(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(email="alice@example.com")
    assert db.get_user_by_email("alice@example.com")["id"] == user["id"]
    assert db.get_user_by_email("nobody@example.com") is None
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.create_user(username="alice2", password_hash=auth.hash_password("x"), email="alice@example.com")


# ── forgot-password (no enumeration) ──────────────────────────────────────────

def _reset_token_count(user_id):
    with db._conn() as con:
        return con.execute(
            "SELECT COUNT(*) AS c FROM password_reset_tokens WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        ).fetchone()["c"]


def test_forgot_password_same_response_and_token_only_for_known_email(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(email="alice@example.com")

    known = auth_router.forgot_password(auth_router.ForgotPasswordRequest(email="alice@example.com"), _anon_request())
    unknown = auth_router.forgot_password(auth_router.ForgotPasswordRequest(email="ghost@example.com"), _anon_request())

    assert known == unknown                       # identical response -> no enumeration
    assert _reset_token_count(user["id"]) == 1     # token created only for the real account


def test_mailer_does_not_log_reset_link_when_configured_smtp_fails(monkeypatch, caplog):
    class FailingSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self, *args, **kwargs):
            pass

        def login(self, *args, **kwargs):
            pass

        def send_message(self, msg):
            raise RuntimeError("smtp down")

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setattr(mailer.smtplib, "SMTP", FailingSMTP)

    reset_url = "https://app.example.com/reset-password?token=SECRET-TOKEN"
    assert mailer.send_password_reset_email("alice@example.com", reset_url, 30) is False
    assert "SECRET-TOKEN" not in caplog.text
    assert "reset-password?token=" not in caplog.text


# ── reset-password ────────────────────────────────────────────────────────────

def test_reset_password_valid_token_resets_and_revokes_all(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123", email="alice@example.com")
    sess = auth.new_token()
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(sess), ttl_days=14)

    raw = auth.new_token()
    db.create_password_reset_token(user["id"], auth.token_hash(raw), ttl_minutes=30)

    auth_router.reset_password(auth_router.ResetPasswordRequest(token=raw, new_password="brand-new-password-9"), _anon_request())

    stored = db.get_user_by_username("alice")
    assert auth.verify_password("brand-new-password-9", stored["password_hash"]) is True
    assert db.get_user_for_auth_token(auth.token_hash(sess)) is None   # all sessions revoked
    # Token is single-use now.
    assert db.get_valid_password_reset(auth.token_hash(raw)) is None


@pytest.mark.parametrize("bad_token", ["nonexistent-token", ""])
def test_reset_password_rejects_invalid_token(monkeypatch, tmp_path, bad_token):
    _init(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as exc:
        auth_router.reset_password(auth_router.ResetPasswordRequest(token=bad_token, new_password="brand-new-password-9"), _anon_request())
    assert exc.value.status_code == 400


def test_reset_password_rejects_expired_token(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(email="alice@example.com")
    raw = auth.new_token()
    db.create_password_reset_token(user["id"], auth.token_hash(raw), ttl_minutes=30)
    # Age the token into the past.
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    with db._conn() as con:
        con.execute("UPDATE password_reset_tokens SET expires_at = ? WHERE token_hash = ?",
                    (past, auth.token_hash(raw)))
    assert db.get_valid_password_reset(auth.token_hash(raw)) is None
    with pytest.raises(HTTPException):
        auth_router.reset_password(auth_router.ResetPasswordRequest(token=raw, new_password="brand-new-password-9"), _anon_request())


def test_reset_password_rejects_token_for_disabled_user(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123", email="alice@example.com")
    raw = auth.new_token()
    db.create_password_reset_token(user["id"], auth.token_hash(raw), ttl_minutes=30)
    with db._conn() as con:
        con.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user["id"],))

    with pytest.raises(HTTPException) as exc:
        auth_router.reset_password(auth_router.ResetPasswordRequest(token=raw, new_password="brand-new-password-9"), _anon_request())

    assert exc.value.status_code == 400
    stored = db.get_user_by_username("alice")
    assert auth.verify_password("old-password-123", stored["password_hash"]) is True


def test_creating_new_reset_token_invalidates_prior(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(email="alice@example.com")
    first = auth.new_token()
    db.create_password_reset_token(user["id"], auth.token_hash(first), ttl_minutes=30)
    second = auth.new_token()
    db.create_password_reset_token(user["id"], auth.token_hash(second), ttl_minutes=30)
    assert db.get_valid_password_reset(auth.token_hash(first)) is None    # superseded
    assert db.get_valid_password_reset(auth.token_hash(second)) is not None


# ── update-email requires current password ────────────────────────────────────

def test_update_email_requires_current_password(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    user = _make_user(password="old-password-123")
    token = auth.new_token()
    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(token), ttl_days=14)

    with pytest.raises(HTTPException) as exc:  # wrong password rejected
        auth_router.update_email(
            auth_router.UpdateEmailRequest(email="new@example.com", current_password="WRONG"),
            _authed_request(user, token),
        )
    assert exc.value.status_code == 400
    assert db.get_user_by_id(user["id"]).get("email") is None  # unchanged

    auth_router.update_email(  # correct password sets it
        auth_router.UpdateEmailRequest(email="new@example.com", current_password="old-password-123"),
        _authed_request(user, token),
    )
    assert db.get_user_by_email("new@example.com")["id"] == user["id"]


# ── rate limiting ─────────────────────────────────────────────────────────────

def test_forgot_password_is_rate_limited(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_FORGOT_MAX", "3")
    req = _anon_request("9.9.9.9")
    for _ in range(3):
        auth_router.forgot_password(auth_router.ForgotPasswordRequest(email="x@example.com"), req)
    with pytest.raises(HTTPException) as exc:
        auth_router.forgot_password(auth_router.ForgotPasswordRequest(email="x@example.com"), req)
    assert exc.value.status_code == 429


def test_reset_password_is_rate_limited(monkeypatch, tmp_path):
    _init(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_RESET_MAX", "2")
    req = _anon_request("8.8.8.8")
    for _ in range(2):  # bad token -> 400, but still counts toward the limit
        with pytest.raises(HTTPException):
            auth_router.reset_password(
                auth_router.ResetPasswordRequest(token="bad", new_password="brand-new-password-9"), req)
    with pytest.raises(HTTPException) as exc:
        auth_router.reset_password(
            auth_router.ResetPasswordRequest(token="bad", new_password="brand-new-password-9"), req)
    assert exc.value.status_code == 429
