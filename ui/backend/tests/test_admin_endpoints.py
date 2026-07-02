"""Phase 1B — Cost Summary Admin Endpoint Tests.

Verifies:
- Endpoint is admin-restricted when auth is enabled.
- Endpoint is open to all when auth is disabled.
- Grouping by user, day, and model returns the correct aggregated values.
- Since and user_id filters are applied properly in SQL.
- Totals are computed correctly.
"""

from pathlib import Path
import sys
from datetime import datetime, timedelta

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for path in (BACKEND_DIR, MCP_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest
from fastapi.testclient import TestClient

import db
import auth
from main import app

client = TestClient(app)


def _init_temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "admin-endpoints.db"))
    db.init_db()


def _seed_runs_data():
    # Create two users
    u_alice = db.create_user(username="alice", password_hash="hash")
    u_bob = db.create_user(username="bob", password_hash="hash")

    # Create sessions
    db.upsert_session("s_alice", user_id=u_alice["id"])
    db.upsert_session("s_bob", user_id=u_bob["id"])

    # We need to insert directly into agent_runs to control started_at, model, tokens, and cost.
    # We will insert:
    # 1. alice, gemini-2.5-flash, 2026-06-20T10:00:00, in=1000, out=500, cost=0.002
    # 2. alice, gemini-2.5-pro,   2026-06-21T10:00:00, in=2000, out=1000, cost=0.007
    # 3. bob,   gemini-2.5-flash, 2026-06-21T11:00:00, in=1500, out=600, cost=0.003
    # 4. bob,   gemini-2.5-flash, 2026-06-22T08:00:00, in=3000, out=1200, cost=0.006

    with db._conn() as con:
        # Alice Run 1
        con.execute(
            """
            INSERT INTO agent_runs (id, session_id, user_id, route, model, status, started_at, ended_at, total_tokens_in, total_tokens_out, total_cached_tokens_in, total_cost_usd)
            VALUES ('run1', 's_alice', ?, 'react', 'gemini-2.5-flash', 'complete', '2026-06-20T10:00:00', '2026-06-20T10:01:00', 1000, 500, 200, 0.002)
            """,
            (u_alice["id"],),
        )
        # Alice Run 2
        con.execute(
            """
            INSERT INTO agent_runs (id, session_id, user_id, route, model, status, started_at, ended_at, total_tokens_in, total_tokens_out, total_cached_tokens_in, total_cost_usd)
            VALUES ('run2', 's_alice', ?, 'react', 'gemini-2.5-pro', 'complete', '2026-06-21T10:00:00', '2026-06-21T10:02:00', 2000, 1000, 400, 0.007)
            """,
            (u_alice["id"],),
        )
        # Bob Run 1
        con.execute(
            """
            INSERT INTO agent_runs (id, session_id, user_id, route, model, status, started_at, ended_at, total_tokens_in, total_tokens_out, total_cached_tokens_in, total_cost_usd)
            VALUES ('run3', 's_bob', ?, 'react', 'gemini-2.5-flash', 'complete', '2026-06-21T11:00:00', '2026-06-21T11:01:30', 1500, 600, 300, 0.003)
            """,
            (u_bob["id"],),
        )
        # Bob Run 2
        con.execute(
            """
            INSERT INTO agent_runs (id, session_id, user_id, route, model, status, started_at, ended_at, total_tokens_in, total_tokens_out, total_cached_tokens_in, total_cost_usd)
            VALUES ('run4', 's_bob', ?, 'react', 'gemini-2.5-flash', 'complete', '2026-06-22T08:00:00', '2026-06-22T08:01:45', 3000, 1200, 600, 0.006)
            """,
            (u_bob["id"],),
        )

    return u_alice["id"], u_bob["id"]


# ── Cost Summary Authorization Tests ──────────────────────────────────────────

def test_cost_summary_auth_disabled(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    _seed_runs_data()

    # Disable auth
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary")
    assert response.status_code == 200
    assert "rows" in response.json()


def test_cost_summary_auth_enabled_admin_allowed(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice_id, _ = _seed_runs_data()

    # Enable auth
    monkeypatch.setenv("AUTH_ENABLED", "true")

    # Mock get_current_user_optional to return an admin
    monkeypatch.setattr(auth, "get_current_user_optional", lambda req: {"id": alice_id, "role": "admin"})

    response = client.get("/api/admin/cost-summary")
    assert response.status_code == 200
    data = response.json()
    assert "rows" in data
    assert "totals" in data


def test_cost_summary_auth_enabled_user_forbidden(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice_id, _ = _seed_runs_data()

    # Enable auth
    monkeypatch.setenv("AUTH_ENABLED", "true")

    # Mock get_current_user_optional to return a normal user
    monkeypatch.setattr(auth, "get_current_user_optional", lambda req: {"id": alice_id, "role": "user"})

    response = client.get("/api/admin/cost-summary")
    assert response.status_code == 403
    assert "Admin permissions required" in response.json()["detail"]


# ── Cost Summary Query / Grouping Tests ───────────────────────────────────────

def test_cost_summary_group_by_user(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice_id, bob_id = _seed_runs_data()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary?group_by=user")
    assert response.status_code == 200
    data = response.json()

    # We expect 2 rows: one for alice, one for bob
    rows = data["rows"]
    assert len(rows) == 2

    # Order is by user_id ASC (i.e. alice first, then bob, usually)
    row_alice = next(r for r in rows if r["user_id"] == alice_id)
    row_bob = next(r for r in rows if r["user_id"] == bob_id)

    # Alice: run1 (in=1000, out=500, cached=200, cost=0.002) + run2 (in=2000, out=1000, cached=400, cost=0.007)
    assert row_alice["total_tokens_in"] == 3000
    assert row_alice["total_tokens_out"] == 1500
    assert row_alice["total_cached_tokens_in"] == 600
    assert abs(row_alice["total_cost_usd"] - 0.009) < 1e-9
    assert row_alice["run_count"] == 2

    # Bob: run3 (in=1500, out=600, cached=300, cost=0.003) + run4 (in=3000, out=1200, cached=600, cost=0.006)
    assert row_bob["total_tokens_in"] == 4500
    assert row_bob["total_tokens_out"] == 1800
    assert row_bob["total_cached_tokens_in"] == 900
    assert abs(row_bob["total_cost_usd"] - 0.009) < 1e-9
    assert row_bob["run_count"] == 2

    # Totals
    totals = data["totals"]
    assert totals["total_tokens_in"] == 7500
    assert totals["total_tokens_out"] == 3300
    assert totals["total_cached_tokens_in"] == 1500
    assert abs(totals["total_cost_usd"] - 0.018) < 1e-9
    assert totals["run_count"] == 4


def test_cost_summary_group_by_day(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    _seed_runs_data()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary?group_by=day")
    assert response.status_code == 200
    data = response.json()

    rows = data["rows"]
    # 2026-06-20, 2026-06-21, 2026-06-22
    assert len(rows) == 3
    assert rows[0]["day"] == "2026-06-20"
    assert rows[1]["day"] == "2026-06-21"
    assert rows[2]["day"] == "2026-06-22"

    # Day 1: 2026-06-20 (run1) -> tokens_in=1000
    assert rows[0]["total_tokens_in"] == 1000
    # Day 2: 2026-06-21 (run2 + run3) -> tokens_in = 2000 + 1500 = 3500
    assert rows[1]["total_tokens_in"] == 3500
    # Day 3: 2026-06-22 (run4) -> tokens_in=3000
    assert rows[2]["total_tokens_in"] == 3000


def test_cost_summary_group_by_model(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    _seed_runs_data()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary?group_by=model")
    assert response.status_code == 200
    data = response.json()

    rows = data["rows"]
    # gemini-2.5-flash, gemini-2.5-pro
    assert len(rows) == 2

    row_flash = next(r for r in rows if r["model"] == "gemini-2.5-flash")
    row_pro = next(r for r in rows if r["model"] == "gemini-2.5-pro")

    assert row_pro["run_count"] == 1
    assert row_flash["run_count"] == 3


def test_cost_summary_with_filters(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice_id, _ = _seed_runs_data()
    monkeypatch.setenv("AUTH_ENABLED", "false")

    # Filter since 2026-06-21
    response = client.get("/api/admin/cost-summary?since=2026-06-21T00:00:00")
    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["run_count"] == 3  # excludes run1 (2026-06-20)

    # Filter by user_id = alice
    response = client.get(f"/api/admin/cost-summary?user_id={alice_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["run_count"] == 2
    assert all(r["user_id"] == alice_id for r in data["rows"])


def test_cost_summary_invalid_group_by(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary?group_by=garbage")
    assert response.status_code == 400
    assert "Invalid group_by" in response.json()["detail"]


def test_cost_summary_auth_enabled_user_forbidden_even_with_invalid_params(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    alice_id, _ = _seed_runs_data()

    # Enable auth
    monkeypatch.setenv("AUTH_ENABLED", "true")

    # Mock get_current_user_optional to return a normal user (not admin)
    monkeypatch.setattr(auth, "get_current_user_optional", lambda req: {"id": alice_id, "role": "user"})

    # Even with invalid group_by, it should check auth first and return 403, not 400.
    response = client.get("/api/admin/cost-summary?group_by=garbage")
    assert response.status_code == 403
    assert "Admin permissions required" in response.json()["detail"]


def test_cost_summary_invalid_since_format(monkeypatch, tmp_path):
    _init_temp_db(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "false")

    response = client.get("/api/admin/cost-summary?since=garbage")
    assert response.status_code == 400
    assert "Invalid 'since' format" in response.json()["detail"]

    # Correct ISO 8601 should succeed (empty db, returns 200 with 0 runs)
    response = client.get("/api/admin/cost-summary?since=2026-06-22T20:26:56Z")
    assert response.status_code == 200

