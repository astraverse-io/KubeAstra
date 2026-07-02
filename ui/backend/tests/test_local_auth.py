"""Local auth persistence and ownership tests."""

from pathlib import Path
import sys
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import auth  # noqa: E402
import db  # noqa: E402
from routers import auth as auth_router  # noqa: E402


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "auth-test.db"))
    db.init_db()


def test_password_hash_is_verified_without_storing_raw_password(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    password = "correct horse battery staple"

    password_hash = auth.hash_password(password)
    user = db.create_user(username="alice", password_hash=password_hash, role="admin")

    stored = db.get_user_by_username("alice")
    assert user["username"] == "alice"
    assert stored["password_hash"] != password
    assert auth.verify_password(password, stored["password_hash"]) is True
    assert auth.verify_password("wrong password", stored["password_hash"]) is False


def test_auth_session_resolves_user_by_token_hash(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    user = db.create_user(
        username="bob",
        password_hash=auth.hash_password("long-password"),
        email="bob@example.com",
    )
    token = auth.new_token()

    db.create_auth_session(user_id=user["id"], token_hash=auth.token_hash(token), ttl_days=14)

    resolved = db.get_user_for_auth_token(auth.token_hash(token))
    assert resolved["id"] == user["id"]
    assert resolved["username"] == "bob"
    assert resolved["email"] == "bob@example.com"

    db.delete_auth_session(auth.token_hash(token))
    assert db.get_user_for_auth_token(auth.token_hash(token)) is None


def test_sessions_are_owned_by_one_user(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice = db.create_user(username="alice", password_hash=auth.hash_password("long-password"))
    bob = db.create_user(username="bob", password_hash=auth.hash_password("long-password"))
    session = db.create_session(user_id=alice["id"], title="Kafka crashloop")

    assert db.user_owns_session(session["id"], alice["id"]) is True
    assert db.user_owns_session(session["id"], bob["id"]) is False
    assert [item["id"] for item in db.list_sessions_for_user(alice["id"])] == [session["id"]]
    assert db.list_sessions_for_user(bob["id"]) == []


def _request_for(user):
    return SimpleNamespace(state=SimpleNamespace(user=auth.public_user(user)), cookies={})


def test_session_read_access_allows_owner_and_admin_only(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    owner = db.create_user(username="owner", password_hash=auth.hash_password("long-password"))
    admin = db.create_user(username="admin", password_hash=auth.hash_password("long-password"), role="admin")
    other = db.create_user(username="other", password_hash=auth.hash_password("long-password"))
    session = db.create_session(user_id=owner["id"], title="Kafka crashloop")

    owner_access = auth.require_session_read_access(_request_for(owner), session["id"])
    admin_access = auth.require_session_read_access(_request_for(admin), session["id"])

    assert owner_access["access_mode"] == "owned"
    assert owner_access["readonly"] is False
    assert admin_access["access_mode"] == "admin_readonly"
    assert admin_access["readonly"] is True
    assert admin_access["session"]["owner_user_id"] == owner["id"]

    try:
        auth.require_session_read_access(_request_for(other), session["id"])
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
        assert getattr(exc, "detail", "") == "Session not found"
    else:
        raise AssertionError("regular users must not read another user's session")


def test_admin_read_access_does_not_grant_write_or_archived_access(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    owner = db.create_user(username="owner", password_hash=auth.hash_password("long-password"))
    admin = db.create_user(username="admin", password_hash=auth.hash_password("long-password"), role="admin")
    empty_session = db.create_session(user_id=owner["id"], title="Empty chat")
    archived_session = db.create_session(user_id=owner["id"], title="Archived chat")
    assert db.archive_session(archived_session["id"], owner["id"]) is True

    assert auth.require_session_read_access(_request_for(admin), empty_session["id"])["access_mode"] == "admin_readonly"
    assert db.get_history(empty_session["id"]) == []

    try:
        auth.require_session_read_access(_request_for(admin), archived_session["id"])
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("archived sessions should not be readable by shared links")

    try:
        auth.require_owned_session(_request_for(admin), empty_session["id"])
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("admin read access must not grant session write access")


def test_session_access_audit_does_not_copy_message_contents(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    owner = db.create_user(username="owner", password_hash=auth.hash_password("long-password"))
    admin = db.create_user(username="admin", password_hash=auth.hash_password("long-password"), role="admin")
    session = db.create_session(user_id=owner["id"], title="Secret incident")
    db.save_message(session["id"], "user", "pod log contains sensitive token abc123")

    event_id = db.save_session_access_event(
        viewer_user_id=admin["id"],
        target_session_id=session["id"],
        owner_user_id=owner["id"],
        access_type="admin_read_history",
    )
    events = db.get_session_access_events(target_session_id=session["id"])

    assert events[0]["id"] == event_id
    assert events[0]["viewer_user_id"] == admin["id"]
    assert events[0]["owner_user_id"] == owner["id"]
    assert events[0]["access_type"] == "admin_read_history"
    assert "sensitive token" not in str(events[0])


def test_anonymous_session_claim_requires_matching_token(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    user = db.create_user(username="carol", password_hash=auth.hash_password("long-password"))
    raw_claim_token = auth.new_token()
    session = db.create_session(anonymous_claim_token_hash=auth.token_hash(raw_claim_token))

    assert db.claim_session(session["id"], user["id"], auth.token_hash("wrong-token")) is False
    assert db.claim_session(session["id"], user["id"], auth.token_hash(raw_claim_token)) is True
    assert db.user_owns_session(session["id"], user["id"]) is True


def test_signup_rejects_existing_username(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_ALLOW_SIGNUP", "true")

    request = SimpleNamespace(headers={}, cookies={})
    first_response = SimpleNamespace(set_cookie=lambda *args, **kwargs: None)
    duplicate_response = SimpleNamespace(set_cookie=lambda *args, **kwargs: None)

    auth_router.signup(
        auth_router.AuthRequest(username="Alice", password="correct horse battery staple"),
        request,
        first_response,
    )

    try:
        auth_router.signup(
            auth_router.AuthRequest(username=" alice ", password="another correct battery staple"),
            request,
            duplicate_response,
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert getattr(exc, "detail", "") == "username or email already in use"
    else:
        raise AssertionError("duplicate signup should be rejected")
