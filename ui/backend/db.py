"""SQLite persistence layer for chat history and SSH targets.

Database file location:
  - Default: ./chat_history.db  (next to main.py)
  - Override: DB_PATH environment variable

Tables:
  sessions    — one row per browser session (session_id from localStorage)
  messages    — every chat turn (user + assistant), linked to session
  ssh_targets — remembered SSH host/user/port per session (password never stored)
  cluster_connections — selected kubeconfig/context per browser session
  feedback_events — durable thumbs-up/down audit trail
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, List, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get(
    "DB_PATH",
    str(Path(__file__).parent / "chat_history.db"),
)

# Maximum messages returned when loading history (keeps payloads small)
MAX_HISTORY_MESSAGES = 100


# ── Connection helper ─────────────────────────────────────────────────────────

@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")   # safe concurrent reads
    con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT,
    title       TEXT,
    archived    INTEGER NOT NULL DEFAULT 0,
    anonymous_claim_token_hash TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_active TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT,
    role          TEXT NOT NULL DEFAULT 'user',
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TEXT NOT NULL,
    used_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    role        TEXT    NOT NULL,   -- 'user' | 'assistant'
    content     TEXT    NOT NULL,
    tool_used   TEXT,
    result_json TEXT,               -- JSON-encoded result dict
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_password_reset_token ON password_reset_tokens(token_hash);

CREATE TABLE IF NOT EXISTS ssh_targets (
    session_id  TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    username    TEXT NOT NULL,
    port        INTEGER NOT NULL DEFAULT 22,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS cluster_connections (
    session_id      TEXT PRIMARY KEY,
    mode            TEXT NOT NULL,           -- 'autodetect' | 'kubeconfig-upload'
    context_name    TEXT NOT NULL,
    cluster_name    TEXT NOT NULL DEFAULT '',
    server_url      TEXT NOT NULL DEFAULT '',
    namespace       TEXT NOT NULL DEFAULT 'default',
    kubeconfig_path TEXT,                    -- temp file path for uploads, NULL for autodetect
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

-- Per-session conversation memory (Phase 2.2).
-- One row per session; entities is a JSON object keyed by category
-- ("namespaces", "resources", "tools", "clusters"). Each category is a
-- list of {value, last_seen, count} sorted by recency. Capped per category;
-- older entries fall off as new ones push in.
CREATE TABLE IF NOT EXISTS user_memory (
    session_id  TEXT PRIMARY KEY,
    entities    TEXT NOT NULL DEFAULT '{}',
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id   TEXT NOT NULL,
    session_id   TEXT,
    rating       TEXT NOT NULL,             -- 'up' | 'down' | invalid submitted value
    outcome      TEXT NOT NULL,             -- accepted | rejected | failed
    reason       TEXT,
    prompt_text  TEXT,
    response_text TEXT,
    tool_used    TEXT,
    action_json  TEXT,                      -- compact JSON result from promote/quarantine
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_events_session ON feedback_events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_feedback_events_capture ON feedback_events(capture_id, id);

CREATE TABLE IF NOT EXISTS session_access_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_user_id    TEXT NOT NULL,
    target_session_id TEXT NOT NULL,
    owner_user_id     TEXT,
    access_type       TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (viewer_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (target_session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_session_access_events_viewer ON session_access_events(viewer_user_id, id);
CREATE INDEX IF NOT EXISTS idx_session_access_events_target ON session_access_events(target_session_id, id);

CREATE TABLE IF NOT EXISTS investigations (
    id          TEXT PRIMARY KEY,
    namespace   TEXT,
    severity    TEXT,
    source      TEXT,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    document    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_investigations_namespace ON investigations(namespace);
CREATE INDEX IF NOT EXISTS idx_investigations_severity ON investigations(severity);
CREATE INDEX IF NOT EXISTS idx_investigations_source ON investigations(source);
CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(status);

-- Harness Phase 1: agent run traces. One row per ReAct invocation.
CREATE TABLE IF NOT EXISTS agent_runs (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT,
    user_id            TEXT,
    parent_run_id      TEXT,
    user_message_id    INTEGER,
    status             TEXT NOT NULL,            -- 'running' | 'complete' | 'failed' | 'aborted'
    route              TEXT,                     -- e.g. 'react', 'fast_path', 'cache_hit'
    model              TEXT,
    model_params_json  TEXT,
    system_prompt_sha  TEXT,
    react_system_sha   TEXT,
    tool_registry_sha  TEXT,
    tool_scope_json    TEXT,
    rag_decision_json  TEXT,
    rag_sources_json   TEXT,
    memory_snapshot    TEXT,
    labels_json        TEXT,
    retention_policy   TEXT NOT NULL DEFAULT 'standard',
    concurrency_key    TEXT,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at           TEXT,
    error              TEXT,
    final_answer       TEXT,
    final_tool         TEXT,
    total_tokens_in    INTEGER DEFAULT 0,
    total_tokens_out   INTEGER DEFAULT 0,
    total_cost_usd     REAL DEFAULT 0,
    postmortem         TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    iteration           INTEGER NOT NULL,
    step_kind           TEXT NOT NULL DEFAULT 'tool',  -- 'llm'|'tool'|'answer'|'approval'|'verify'|'compaction'|'error'
    thought             TEXT,
    action              TEXT NOT NULL,
    params_json         TEXT,
    status              TEXT NOT NULL,                 -- 'ok' | 'error' | 'skipped'
    source              TEXT,                          -- e.g. 'react', 'critic', 'fast_path'
    trust_level         TEXT NOT NULL DEFAULT 'system',
    observation_ref     TEXT,                          -- pointer (envelope id, file ref) when offloaded
    observation_preview TEXT,                          -- redacted preview for trace UI
    error_type          TEXT,
    error_message       TEXT,
    tokens_in           INTEGER DEFAULT 0,
    tokens_out          INTEGER DEFAULT 0,
    cost_usd            REAL DEFAULT 0,
    started_at          TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at            TEXT,
    duration_ms         INTEGER,
    approver_user_id    TEXT,
    FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_observations (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    step_id         INTEGER,
    tool            TEXT NOT NULL,
    source          TEXT NOT NULL,
    trust_level     TEXT NOT NULL DEFAULT 'untrusted',
    content_type    TEXT NOT NULL DEFAULT 'application/json',
    content         TEXT NOT NULL,
    summary         TEXT,
    redaction_status TEXT NOT NULL DEFAULT 'redacted',
    bytes_in        INTEGER DEFAULT 0,
    bytes_out       INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
"""


def init_db() -> None:
    """Create tables if they don't exist. Called once at app startup."""
    with _conn() as con:
        con.executescript(_SCHEMA)
        _ensure_columns(con, "sessions", {
            "user_id": "TEXT",
            "title": "TEXT",
            "archived": "INTEGER NOT NULL DEFAULT 0",
            "anonymous_claim_token_hash": "TEXT",
        })
        # Alert dedup. Alertmanager re-sends a firing alert on its repeat
        # interval by design, so without these every delivery of one ongoing
        # problem started a fresh investigation — a full LLM run each time.
        _ensure_columns(con, "investigations", {
            "resolved_at": "TEXT",
            "mttr_seconds": "REAL",
            "fingerprint": "TEXT",
            "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
            "last_seen_at": "TEXT",
        })
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigations_fingerprint "
            "ON investigations(fingerprint, created_at)"
        )
        _ensure_columns(con, "feedback_events", {
            "prompt_text": "TEXT",
            "response_text": "TEXT",
            "tool_used": "TEXT",
        })
        _ensure_columns(con, "agent_runs", {
            "postmortem": "TEXT",
            "total_cached_tokens_in": "INTEGER DEFAULT 0",
            "system_prompt_sha": "TEXT",
            "react_system_sha": "TEXT",
            "tool_registry_sha": "TEXT",
        })
        _ensure_columns(con, "agent_steps", {
            "approver_user_id": "TEXT",
            "cached_tokens_in": "INTEGER DEFAULT 0",
            "step_model": "TEXT",
        })
        _ensure_columns(con, "users", {"email": "TEXT"})
        # Enforce unique email for non-null values (existing users have none).
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, last_active)")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_claim_token ON sessions(anonymous_claim_token_hash)"
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, started_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_user ON agent_runs(user_id, started_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id, started_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, iteration)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_agent_observations_run ON agent_observations(run_id)")
    logger.info(f"SQLite DB ready at {DB_PATH}")


def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, spec in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


# ── Session helpers ───────────────────────────────────────────────────────────

def _new_id() -> str:
    return str(uuid.uuid4())


def _title_from_content(content: str) -> str:
    title = " ".join((content or "").strip().split())
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    return title or "New chat"


def upsert_session(
    session_id: str,
    user_id: Optional[str] = None,
    title: Optional[str] = None,
    anonymous_claim_token_hash: Optional[str] = None,
) -> None:
    """Create session if new, or bump last_active if existing."""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO sessions(session_id, user_id, title, anonymous_claim_token_hash)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                last_active = datetime('now'),
                user_id = COALESCE(excluded.user_id, sessions.user_id),
                title = COALESCE(excluded.title, sessions.title),
                anonymous_claim_token_hash = COALESCE(
                    excluded.anonymous_claim_token_hash,
                    sessions.anonymous_claim_token_hash
                )
            """,
            (session_id, user_id, title, anonymous_claim_token_hash),
        )


def create_session(
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    title: Optional[str] = None,
    anonymous_claim_token_hash: Optional[str] = None,
) -> dict:
    """Create a chat session and return its id/title metadata."""
    sid = session_id or _new_id()
    upsert_session(
        sid,
        user_id=user_id,
        title=title or "New chat",
        anonymous_claim_token_hash=anonymous_claim_token_hash,
    )
    return {"id": sid, "title": title or "New chat", "timestamp": int(datetime.now(timezone.utc).replace(tzinfo=None).timestamp() * 1000)}


def attach_session_to_user(session_id: str, user_id: str) -> None:
    with _conn() as con:
        con.execute(
            """
            UPDATE sessions
            SET user_id = ?, last_active = datetime('now')
            WHERE session_id = ? AND user_id IS NULL
            """,
            (user_id, session_id),
        )


def claim_session(session_id: str, user_id: str, claim_token_hash: str) -> bool:
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE sessions
            SET user_id = ?, anonymous_claim_token_hash = NULL, last_active = datetime('now')
            WHERE session_id = ?
              AND user_id IS NULL
              AND anonymous_claim_token_hash = ?
            """,
            (user_id, session_id, claim_token_hash),
        )
        return cur.rowcount == 1


def user_owns_session(session_id: str, user_id: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM sessions WHERE session_id = ? AND user_id = ? AND archived = 0",
            (session_id, user_id),
        ).fetchone()
    return bool(row)


def get_session_metadata(session_id: str, *, include_archived: bool = False) -> Optional[dict]:
    """Return session ownership metadata without loading message contents."""
    archived_clause = "" if include_archived else "AND s.archived = 0"
    with _conn() as con:
        row = con.execute(
            f"""
            SELECT
                s.session_id,
                s.user_id AS owner_user_id,
                u.username AS owner_username,
                u.display_name AS owner_display_name,
                s.title,
                s.archived,
                s.created_at,
                s.last_active
            FROM sessions s
            LEFT JOIN users u ON u.id = s.user_id
            WHERE s.session_id = ?
            {archived_clause}
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions_for_user(user_id: str, limit: int = 100) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT session_id, title, last_active, created_at
            FROM sessions
            WHERE user_id = ? AND archived = 0
            ORDER BY last_active DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    sessions = []
    for row in rows:
        try:
            timestamp = int(datetime.fromisoformat(row["last_active"]).timestamp() * 1000)
        except Exception:
            timestamp = int(datetime.now(timezone.utc).replace(tzinfo=None).timestamp() * 1000)
        sessions.append({
            "id": row["session_id"],
            "title": row["title"] or "New chat",
            "timestamp": timestamp,
        })
    return sessions


def archive_session(session_id: str, user_id: str) -> bool:
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE sessions
            SET archived = 1, last_active = datetime('now')
            WHERE session_id = ? AND user_id = ?
            """,
            (session_id, user_id),
        )
        return cur.rowcount == 1


# ── Message helpers ───────────────────────────────────────────────────────────

def save_message(
    session_id: str,
    role: str,
    content: str,
    tool_used: Optional[str] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
) -> None:
    """Persist a single chat turn message."""
    upsert_session(session_id)
    result_json = json.dumps(result) if result is not None else None
    with _conn() as con:
        con.execute(
            """
            INSERT INTO messages(session_id, role, content, tool_used, result_json, error)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, tool_used, result_json, error),
        )
        if role == "user":
            con.execute(
                """
                UPDATE sessions
                SET title = CASE
                        WHEN title IS NULL OR title = 'New chat' THEN ?
                        ELSE title
                    END,
                    last_active = datetime('now')
                WHERE session_id = ?
                """,
                (_title_from_content(content), session_id),
            )


def get_history(session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> List[dict]:
    """Return the last `limit` messages for a session, oldest first."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT role, content, tool_used, result_json, error, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()

    # Reverse so messages are oldest-first for the frontend
    result = []
    for row in reversed(rows):
        entry: dict = {
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
        }
        if row["tool_used"]:
            entry["tool_used"] = row["tool_used"]
        if row["result_json"]:
            try:
                entry["result"] = json.loads(row["result_json"])
            except Exception:
                pass
        if row["error"]:
            entry["error"] = row["error"]
        result.append(entry)
    return result


def clear_history(session_id: str) -> None:
    """Delete all messages for a session (used by 'New chat' button)."""
    with _conn() as con:
        con.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))


# ── SSH target helpers ────────────────────────────────────────────────────────

def save_ssh_target(session_id: str, host: str, username: str, port: int = 22) -> None:
    """Remember SSH host/user/port for a session. Password is never stored."""
    upsert_session(session_id)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO ssh_targets(session_id, host, username, port)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                host = excluded.host,
                username = excluded.username,
                port = excluded.port,
                updated_at = datetime('now')
            """,
            (session_id, host, username, port),
        )


def get_ssh_target(session_id: str) -> Optional[dict]:
    """Return saved SSH target for a session, or None if not set."""
    with _conn() as con:
        row = con.execute(
            "SELECT host, username, port FROM ssh_targets WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if row:
        return {"host": row["host"], "username": row["username"], "port": row["port"]}
    return None


def delete_ssh_target(session_id: str) -> None:
    """Clear saved SSH target (called on disconnect)."""
    with _conn() as con:
        con.execute("DELETE FROM ssh_targets WHERE session_id = ?", (session_id,))


# ── Cluster connection helpers ───────────────────────────────────────────────

def save_cluster_connection(
    session_id: str,
    mode: str,
    context_name: str,
    cluster_name: str = "",
    server_url: str = "",
    namespace: str = "default",
    kubeconfig_path: Optional[str] = None,
) -> None:
    """Save an active cluster connection for a browser session."""
    upsert_session(session_id)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO cluster_connections(
                session_id, mode, context_name, cluster_name,
                server_url, namespace, kubeconfig_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                mode = excluded.mode,
                context_name = excluded.context_name,
                cluster_name = excluded.cluster_name,
                server_url = excluded.server_url,
                namespace = excluded.namespace,
                kubeconfig_path = excluded.kubeconfig_path,
                updated_at = datetime('now')
            """,
            (
                session_id,
                mode,
                context_name,
                cluster_name,
                server_url,
                namespace,
                kubeconfig_path,
            ),
        )


def get_cluster_connection(session_id: str) -> Optional[dict]:
    """Return the active cluster connection for a session, if any."""
    with _conn() as con:
        row = con.execute(
            """
            SELECT mode, context_name, cluster_name, server_url, namespace, kubeconfig_path
            FROM cluster_connections
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "mode": row["mode"],
        "context_name": row["context_name"],
        "cluster_name": row["cluster_name"],
        "server_url": row["server_url"],
        "namespace": row["namespace"],
        "kubeconfig_path": row["kubeconfig_path"],
    }


def delete_cluster_connection(session_id: str) -> Optional[str]:
    """Delete the cluster connection and return any temp kubeconfig path."""
    with _conn() as con:
        row = con.execute(
            "SELECT kubeconfig_path FROM cluster_connections WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        con.execute("DELETE FROM cluster_connections WHERE session_id = ?", (session_id,))
    return row["kubeconfig_path"] if row else None


# ── User memory helpers (Phase 2.2) ──────────────────────────────────────────

def get_user_memory(session_id: str) -> dict:
    """Return the entities dict for a session, or {} if none stored."""
    with _conn() as con:
        row = con.execute(
            "SELECT entities FROM user_memory WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["entities"])
    except Exception:
        logger.warning("user_memory: corrupt entities JSON for session=%s", session_id)
        return {}


def save_user_memory(session_id: str, entities: dict) -> None:
    """Replace the entities blob for a session. Caller is responsible for any
    merging/capping; this is a straight overwrite."""
    upsert_session(session_id)
    payload = json.dumps(entities, default=str)
    with _conn() as con:
        con.execute(
            """
            INSERT INTO user_memory(session_id, entities) VALUES(?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                entities = excluded.entities,
                updated_at = datetime('now')
            """,
            (session_id, payload),
        )


def clear_user_memory(session_id: str) -> None:
    """Wipe stored memory for a session (call when user clears chat)."""
    with _conn() as con:
        con.execute("DELETE FROM user_memory WHERE session_id = ?", (session_id,))


# ── Local auth helpers ────────────────────────────────────────────────────────

def create_user(
    *,
    username: str,
    password_hash: str,
    display_name: Optional[str] = None,
    role: str = "user",
    email: Optional[str] = None,
) -> dict:
    user_id = _new_id()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO users(id, username, password_hash, display_name, role, email)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, password_hash, display_name, role, email),
        )
    return {
        "id": user_id,
        "username": username,
        "display_name": display_name,
        "role": role,
        "email": email,
        "disabled": False,
    }


def get_user_by_username(username: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            """
            SELECT id, username, password_hash, display_name, role, email, disabled, created_at, last_login_at
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            """
            SELECT id, username, display_name, role, email, disabled, created_at, last_login_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    """Look up an enabled-or-disabled user by email (for password reset)."""
    with _conn() as con:
        row = con.execute(
            """
            SELECT id, username, password_hash, display_name, role, email, disabled, created_at, last_login_at
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()
    return dict(row) if row else None


def set_user_password(user_id: str, password_hash: str) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def set_user_email(user_id: str, email: Optional[str]) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))


def mark_user_login(user_id: str) -> None:
    with _conn() as con:
        con.execute("UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (user_id,))


def create_auth_session(
    *,
    user_id: str,
    token_hash: str,
    ttl_days: int,
    user_agent: Optional[str] = None,
) -> dict:
    session_id = _new_id()
    expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=ttl_days)).isoformat()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO auth_sessions(id, user_id, token_hash, expires_at, user_agent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, user_id, token_hash, expires_at, user_agent),
        )
    return {"id": session_id, "expires_at": expires_at}


def get_user_for_auth_token(token_hash: str) -> Optional[dict]:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    with _conn() as con:
        row = con.execute(
            """
            SELECT
                u.id, u.username, u.display_name, u.role, u.email, u.disabled,
                u.created_at, u.last_login_at,
                s.id AS auth_session_id, s.expires_at
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0
            """,
            (token_hash, now),
        ).fetchone()
    return dict(row) if row else None


def delete_auth_session(token_hash: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))


def delete_expired_auth_sessions() -> None:
    with _conn() as con:
        con.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),))


def delete_user_auth_sessions(user_id: str, except_token_hash: Optional[str] = None) -> int:
    """Revoke a user's auth sessions (e.g. on password change/reset).

    When ``except_token_hash`` is given, that one session is kept so the acting
    user is not logged out of their current browser. Returns rows removed.
    """
    with _conn() as con:
        if except_token_hash:
            cur = con.execute(
                "DELETE FROM auth_sessions WHERE user_id = ? AND token_hash != ?",
                (user_id, except_token_hash),
            )
        else:
            cur = con.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        return cur.rowcount


# ── Password reset tokens ─────────────────────────────────────────────────────

def create_password_reset_token(user_id: str, token_hash: str, ttl_minutes: int) -> dict:
    token_id = _new_id()
    expires_at = (datetime.utcnow() + timedelta(minutes=ttl_minutes)).isoformat()
    with _conn() as con:
        # Invalidate any outstanding tokens for this user before issuing a new one.
        con.execute(
            "UPDATE password_reset_tokens SET used_at = datetime('now') "
            "WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        con.execute(
            "INSERT INTO password_reset_tokens(id, user_id, token_hash, expires_at) VALUES (?, ?, ?, ?)",
            (token_id, user_id, token_hash, expires_at),
        )
    return {"id": token_id, "expires_at": expires_at}


def get_valid_password_reset(token_hash: str) -> Optional[dict]:
    """Return {user_id} for an unused, unexpired reset token, else None."""
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        row = con.execute(
            """
            SELECT id, user_id, expires_at, used_at
            FROM password_reset_tokens
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
    if not row:
        return None
    if row["used_at"] is not None or row["expires_at"] <= now:
        return None
    return {"id": row["id"], "user_id": row["user_id"]}


def mark_password_reset_used(token_hash: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE password_reset_tokens SET used_at = datetime('now') WHERE token_hash = ?",
            (token_hash,),
        )


def reset_password_with_token(token_hash: str, password_hash: str) -> Optional[str]:
    """Atomically consume a valid reset token, update the password, and revoke sessions.

    Returns the user_id on success, or None when the token is missing, expired,
    already used, or belongs to a disabled account.
    """
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        row = con.execute(
            """
            SELECT t.id, t.user_id
            FROM password_reset_tokens t
            JOIN users u ON u.id = t.user_id
            WHERE t.token_hash = ?
              AND t.used_at IS NULL
              AND t.expires_at > ?
              AND u.disabled = 0
            """,
            (token_hash, now),
        ).fetchone()
        if not row:
            return None
        cur = con.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = datetime('now')
            WHERE id = ? AND used_at IS NULL AND expires_at > ?
            """,
            (row["id"], now),
        )
        if cur.rowcount != 1:
            return None
        con.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, row["user_id"]),
        )
        con.execute("DELETE FROM auth_sessions WHERE user_id = ?", (row["user_id"],))
        return row["user_id"]


# ── Feedback audit helpers ───────────────────────────────────────────────────

def save_feedback_event(
    *,
    capture_id: str,
    rating: str,
    outcome: str,
    session_id: Optional[str] = None,
    reason: Optional[str] = None,
    prompt_text: Optional[str] = None,
    response_text: Optional[str] = None,
    tool_used: Optional[str] = None,
    action_result: Optional[dict] = None,
    error: Optional[str] = None,
) -> int:
    """Persist a thumbs-up/down audit event and return its row id."""
    if session_id:
        upsert_session(session_id)
    action_json = json.dumps(action_result, default=str) if action_result is not None else None
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO feedback_events(
                capture_id, session_id, rating, outcome, reason,
                prompt_text, response_text, tool_used, action_json, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_id,
                session_id,
                rating,
                outcome,
                reason,
                prompt_text,
                response_text,
                tool_used,
                action_json,
                error,
            ),
        )
        return int(cur.lastrowid)


def get_feedback_events(
    *,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    capture_id: Optional[str] = None,
    rating: Optional[str] = None,
    outcome: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    """Return recent feedback audit events, newest first."""
    clauses = []
    params: list = []
    if session_id:
        clauses.append("feedback_events.session_id = ?")
        params.append(session_id)
    if user_id:
        clauses.append("sessions.user_id = ?")
        params.append(user_id)
    if capture_id:
        clauses.append("feedback_events.capture_id = ?")
        params.append(capture_id)
    if rating:
        clauses.append("rating = ?")
        params.append(rating)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _conn() as con:
        rows = con.execute(
            f"""
            SELECT feedback_events.id, capture_id, feedback_events.session_id, rating, outcome, reason,
                   prompt_text, response_text, feedback_events.tool_used, action_json, error,
                   feedback_events.created_at
            FROM feedback_events
            LEFT JOIN sessions ON sessions.session_id = feedback_events.session_id
            {where}
            ORDER BY feedback_events.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    events = []
    for row in rows:
        event = {
            "id": row["id"],
            "capture_id": row["capture_id"],
            "session_id": row["session_id"],
            "rating": row["rating"],
            "outcome": row["outcome"],
            "reason": row["reason"],
            "prompt": row["prompt_text"],
            "response": row["response_text"],
            "tool_used": row["tool_used"],
            "error": row["error"],
            "created_at": row["created_at"],
        }
        if row["action_json"]:
            try:
                event["action_result"] = json.loads(row["action_json"])
            except Exception:
                event["action_result"] = row["action_json"]
        events.append(event)
    return events


# ── Session access audit helpers ──────────────────────────────────────────────

def save_session_access_event(
    *,
    viewer_user_id: str,
    target_session_id: str,
    owner_user_id: Optional[str],
    access_type: str,
) -> int:
    """Persist metadata-only audit for cross-user session reads."""
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO session_access_events(
                viewer_user_id, target_session_id, owner_user_id, access_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (viewer_user_id, target_session_id, owner_user_id, access_type),
        )
        return int(cur.lastrowid)


def get_session_access_events(*, target_session_id: Optional[str] = None, limit: int = 100) -> List[dict]:
    clauses = []
    params: list = []
    if target_session_id:
        clauses.append("target_session_id = ?")
        params.append(target_session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _conn() as con:
        rows = con.execute(
            f"""
            SELECT id, viewer_user_id, target_session_id, owner_user_id, access_type, created_at
            FROM session_access_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


# ── Investigations persistence (Phase 1 Merge) ────────────────────────────────

def sweep_orphaned_investigations() -> None:
    """Reset any running investigations to failed on startup to prevent zombie state."""
    with _conn() as con:
        con.execute(
            "UPDATE investigations SET status = 'failed' WHERE status = 'running'"
        )

# The repository base class lives in the mcp `alerts` package, so
# importing it needs the MCP path on sys.path. In the running app that is
# guaranteed (PYTHONPATH in the image + main.py injects MCP_PATH). Import it
# defensively so db.py stays importable on its own — the Docker build's import
# smoke-test, tooling, and unit tests may import db before the MCP path is set.
try:
    from alerts.repositories.base import InvestigationRepository as _InvestigationRepositoryBase
except ModuleNotFoundError:
    _InvestigationRepositoryBase = object  # type: ignore[assignment,misc]

class SqliteInvestigationRepository(_InvestigationRepositoryBase):
    """Implements InvestigationRepository from mcp/alerts."""
    async def save(self, investigation) -> None:
        doc = investigation.model_dump_json()
        with _conn() as con:
            con.execute(
                """
                INSERT INTO investigations
                    (id, namespace, severity, source, status, created_at, document,
                     fingerprint, occurrence_count, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                    namespace=excluded.namespace,
                    severity=excluded.severity,
                    source=excluded.source,
                    status=excluded.status,
                    document=excluded.document,
                    fingerprint=excluded.fingerprint
                    -- occurrence_count and last_seen_at are owned by
                    -- record_recurrence(); a status update must not reset them.
                """,
                (
                    investigation.investigation_id,
                    investigation.alert.labels.get("namespace", "default"),
                    investigation.alert.severity,
                    investigation.alert.source,
                    investigation.status.value,
                    investigation.created_at.isoformat(),
                    doc,
                    getattr(investigation.alert, "fingerprint", None),
                    investigation.created_at.isoformat(),
                )
            )

    async def get(self, investigation_id: str):
        with _conn() as con:
            row = con.execute("SELECT document FROM investigations WHERE id = ?", (investigation_id,)).fetchone()
            if row:
                from alerts.domain.investigation import Investigation
                return Investigation.model_validate_json(row["document"])
        return None


# ── Alert dedup ───────────────────────────────────────────────────────────────
#
# Alertmanager re-sends a firing alert every `repeat_interval` for as long as
# the condition holds. That is not a storm or an edge case — it is the normal
# path, and without dedup one ongoing problem produced an investigation per
# delivery: a full LLM run, a row, and a notification, every time.
#
# `Alert.fingerprint` already existed and was computed on every alert; nothing
# read it.

# Statuses meaning "this investigation is still the live answer for that
# alert". A repeat arriving while one of these is current is the same
# incident; a repeat after a terminal status is a genuine re-occurrence and
# deserves a fresh investigation.
#
# Derived from InvestigationStatus rather than written out, because guessing
# them is a silent failure: a name that does not exist matches nothing, so
# dedup would appear to work and never fire. Everything not terminal is in
# flight.
TERMINAL_INVESTIGATION_STATUSES = frozenset({"completed", "failed", "resolved"})


def _open_statuses() -> tuple:
    try:
        from alerts.domain.enums import InvestigationStatus
    except Exception:  # pragma: no cover — mcp path not set up
        return ("received", "classified", "running")
    return tuple(
        s.value
        for s in InvestigationStatus
        if s.value not in TERMINAL_INVESTIGATION_STATUSES
    )


OPEN_INVESTIGATION_STATUSES = _open_statuses()


def find_open_investigation(fingerprint: str, within_hours: int = 24) -> Optional[dict]:
    """The live investigation for this fingerprint, if there is one.

    Bounded by age as well as status: an investigation stuck in `investigating`
    because the process died mid-run would otherwise absorb every future
    occurrence of that alert forever, and the alert would silently stop
    producing anything at all.
    """
    if not fingerprint:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    placeholders = ",".join("?" * len(OPEN_INVESTIGATION_STATUSES))
    with _conn() as con:
        row = con.execute(
            f"""
            SELECT id, occurrence_count, status, created_at
            FROM investigations
            WHERE fingerprint = ?
              AND status IN ({placeholders})
              AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (fingerprint, *OPEN_INVESTIGATION_STATUSES, cutoff),
        ).fetchone()
    return dict(row) if row else None


def record_recurrence(investigation_id: str) -> int:
    """Count another delivery of an alert already being investigated.

    Returns the new occurrence count. Incremented in SQL rather than
    read-modify-write so two concurrent deliveries cannot both read 3 and both
    write 4 — Alertmanager sends batches, and this runs per alert in one.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "UPDATE investigations "
            "SET occurrence_count = occurrence_count + 1, last_seen_at = ? "
            "WHERE id = ?",
            (now, investigation_id),
        )
        row = con.execute(
            "SELECT occurrence_count FROM investigations WHERE id = ?",
            (investigation_id,),
        ).fetchone()
    return int(row["occurrence_count"]) if row else 1


def resolve_investigation(investigation_id: str) -> Optional[float]:
    """Close an investigation because its alert stopped firing.

    Returns seconds from first delivery to resolution, or None if there was no
    such open investigation. That number is the only honest measure of time to
    recovery available here: it is the interval over which the alert was
    actually firing, not how long the LLM took to answer.

    Guarded on the current status so a late duplicate `resolved` delivery —
    Alertmanager sends those — cannot rewrite an earlier resolution's clock and
    make the recovery look longer than it was.
    """
    now = datetime.now(timezone.utc)
    placeholders = ",".join("?" * len(OPEN_INVESTIGATION_STATUSES))
    with _conn() as con:
        row = con.execute(
            f"SELECT created_at FROM investigations "
            f"WHERE id = ? AND status IN ({placeholders})",
            (investigation_id, *OPEN_INVESTIGATION_STATUSES),
        ).fetchone()
        if row is None:
            return None

        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        # Clamped at zero: a row written by a host with a skewed clock would
        # otherwise report a negative time to recovery.
        seconds = max(0.0, (now - created).total_seconds())
        con.execute(
            "UPDATE investigations "
            "SET status = 'resolved', resolved_at = ?, mttr_seconds = ?, "
            "    last_seen_at = ? "
            "WHERE id = ?",
            (now.isoformat(), seconds, now.isoformat(), investigation_id),
        )
    return seconds


# ── Agent run helpers (harness Phase 1) ───────────────────────────────────────

AGENT_RUN_STATUSES = ("running", "complete", "failed", "aborted")


def _dump_json(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except Exception:
        return None


def create_agent_run(
    *,
    run_id: Optional[str] = None,
    session_id: Optional[str],
    user_id: Optional[str],
    parent_run_id: Optional[str] = None,
    user_message_id: Optional[int] = None,
    route: Optional[str] = None,
    model: Optional[str] = None,
    model_params: Optional[dict] = None,
    system_prompt_sha: Optional[str] = None,
    react_system_sha: Optional[str] = None,
    tool_registry_sha: Optional[str] = None,
    tool_scope: Optional[Any] = None,  # canonical Phase 7 shape: ScopeDecision.to_dict()
    rag_decision: Optional[dict] = None,
    rag_sources: Optional[list] = None,
    memory_snapshot: Optional[str] = None,
    labels: Optional[dict] = None,
    retention_policy: str = "standard",
    concurrency_key: Optional[str] = None,
) -> str:
    """Open an `agent_runs` row and return the run_id."""
    rid = run_id or _new_id()
    with _conn() as con:
        con.execute(
            """
            INSERT INTO agent_runs(
                id, session_id, user_id, parent_run_id, user_message_id,
                status, route, model, model_params_json,
                system_prompt_sha, react_system_sha, tool_registry_sha,
                tool_scope_json, rag_decision_json, rag_sources_json,
                memory_snapshot, labels_json, retention_policy, concurrency_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rid, session_id, user_id, parent_run_id, user_message_id,
                "running", route, model, _dump_json(model_params),
                system_prompt_sha, react_system_sha, tool_registry_sha,
                _dump_json(tool_scope), _dump_json(rag_decision), _dump_json(rag_sources),
                memory_snapshot, _dump_json(labels), retention_policy, concurrency_key,
            ),
        )
    return rid


def finish_agent_run(
    run_id: str,
    *,
    final_answer: Optional[str],
    final_tool: Optional[str] = None,
    total_tokens_in: int = 0,
    total_tokens_out: int = 0,
    total_cached_tokens_in: int = 0,
    total_cost_usd: float = 0.0,
) -> None:
    """Mark a run complete with the final answer."""
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_runs
            SET status = 'complete',
                ended_at = datetime('now'),
                final_answer = ?,
                final_tool = ?,
                total_tokens_in = ?,
                total_tokens_out = ?,
                total_cached_tokens_in = ?,
                total_cost_usd = ?
            WHERE id = ?
            """,
            (final_answer, final_tool, total_tokens_in, total_tokens_out,
             total_cached_tokens_in, total_cost_usd, run_id),
        )


def fail_agent_run(
    run_id: str,
    *,
    error: str,
    status: str = "failed",
    final_answer: Optional[str] = None,
    final_tool: Optional[str] = None,
    total_tokens_in: int = 0,
    total_tokens_out: int = 0,
    total_cached_tokens_in: int = 0,
    total_cost_usd: float = 0.0,
) -> None:
    """Mark a run failed or aborted with an error message and total cost/token metrics."""
    if status not in ("failed", "aborted"):
        status = "failed"
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_runs
            SET status = ?,
                ended_at = datetime('now'),
                error = ?,
                final_answer = COALESCE(?, final_answer),
                final_tool = COALESCE(?, final_tool),
                total_tokens_in = ?,
                total_tokens_out = ?,
                total_cached_tokens_in = ?,
                total_cost_usd = ?
            WHERE id = ?
            """,
            (
                status,
                error,
                final_answer,
                final_tool,
                total_tokens_in,
                total_tokens_out,
                total_cached_tokens_in,
                total_cost_usd,
                run_id,
            ),
        )


def increment_agent_run_cost(
    run_id: str,
    *,
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Increment cost and token metrics of an agent run."""
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_runs
            SET total_tokens_in = total_tokens_in + ?,
                total_tokens_out = total_tokens_out + ?,
                total_cached_tokens_in = total_cached_tokens_in + ?,
                total_cost_usd = total_cost_usd + ?
            WHERE id = ?
            """,
            (tokens_in, tokens_out, cached_tokens_in, cost_usd, run_id),
        )



def record_agent_step(
    *,
    run_id: str,
    iteration: int,
    action: str,
    status: str,
    step_kind: str = "tool",
    thought: Optional[str] = None,
    params: Optional[dict] = None,
    source: Optional[str] = None,
    trust_level: str = "system",
    observation_ref: Optional[str] = None,
    observation_preview: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    tokens_in: int = 0,
    cached_tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    step_model: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> int:
    """Append one agent_steps row. Returns the inserted rowid."""
    with _conn() as con:
        cur = con.execute(
            """
            INSERT INTO agent_steps(
                run_id, iteration, step_kind, thought, action, params_json,
                status, source, trust_level, observation_ref, observation_preview,
                error_type, error_message, tokens_in, cached_tokens_in, tokens_out,
                cost_usd, step_model, ended_at, duration_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
            """,
            (
                run_id, iteration, step_kind, thought, action, _dump_json(params),
                status, source, trust_level, observation_ref, observation_preview,
                error_type, error_message, tokens_in, cached_tokens_in, tokens_out,
                cost_usd, step_model, duration_ms,
            ),
        )
        return int(cur.lastrowid)


def list_agent_runs(
    *,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 50,
) -> List[dict]:
    """List runs scoped to user (and optionally session). Most-recent first."""
    clauses: list[str] = []
    params: list = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _conn() as con:
        rows = con.execute(
            f"""
            SELECT id, session_id, user_id, status, route, model,
                   started_at, ended_at, final_tool, error
            FROM agent_runs
            {where}
            ORDER BY started_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_agent_run(run_id: str) -> Optional[dict]:
    """Return a single run with JSON fields parsed back into dicts."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM agent_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    out = dict(row)
    for k in ("model_params_json", "tool_scope_json", "rag_decision_json",
              "rag_sources_json", "labels_json"):
        if out.get(k):
            try:
                out[k] = json.loads(out[k])
            except Exception:
                pass
    return out


def get_agent_steps(run_id: str) -> List[dict]:
    """Return all steps for a run in iteration order."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT id, run_id, iteration, step_kind, thought, action, params_json,
                   status, source, trust_level, observation_ref, observation_preview,
                   error_type, error_message, tokens_in, cached_tokens_in, tokens_out,
                   cost_usd, step_model,
                   started_at, ended_at, duration_ms, approver_user_id
            FROM agent_steps
            WHERE run_id = ?
            ORDER BY iteration ASC, id ASC
            """,
            (run_id,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        if d.get("params_json"):
            try:
                d["params_json"] = json.loads(d["params_json"])
            except Exception:
                pass
        out.append(d)
    return out


def prune_agent_runs(retention_days: int) -> int:
    """Delete runs older than retention_days unless retention_policy != 'standard'.
    Returns rows deleted. Cascades to agent_steps via FK."""
    if retention_days <= 0:
        return 0
    with _conn() as con:
        cur = con.execute(
            """
            DELETE FROM agent_runs
            WHERE retention_policy = 'standard'
              AND started_at < datetime('now', ?)
            """,
            (f"-{retention_days} days",),
        )
        return cur.rowcount or 0


def update_agent_run_retention(run_id: str, retention_policy: str) -> bool:
    """Update the retention policy of an agent run (e.g. to 'golden' or 'standard').
    Returns True if the run was found and updated."""
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE agent_runs
            SET retention_policy = ?
            WHERE id = ?
            """,
            (retention_policy, run_id),
        )
        return (cur.rowcount or 0) > 0


def update_agent_run_postmortem(run_id: str, postmortem: str) -> bool:
    """Update the postmortem report of an agent run.
    Returns True if the run was found and updated."""
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE agent_runs
            SET postmortem = ?
            WHERE id = ?
            """,
            (postmortem, run_id),
        )
        return (cur.rowcount or 0) > 0


def save_agent_observation(
    *,
    id: str,
    run_id: str,
    step_id: Optional[int] = None,
    tool: str,
    source: str,
    trust_level: str = "untrusted",
    content_type: str = "application/json",
    content: str,
    summary: Optional[str] = None,
    redaction_status: str = "redacted",
    bytes_in: int = 0,
    bytes_out: int = 0,
) -> None:
    """Save raw/compacted tool observation to the agent_observations table."""
    with _conn() as con:
        con.execute(
            """
            INSERT INTO agent_observations (
                id, run_id, step_id, tool, source, trust_level,
                content_type, content, summary, redaction_status,
                bytes_in, bytes_out, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                id, run_id, step_id, tool, source, trust_level,
                content_type, content, summary, redaction_status,
                bytes_in, bytes_out,
            ),
        )


def get_agent_observation(observation_id: str) -> Optional[dict]:
    """Retrieve an observation by its unique ID."""
    with _conn() as con:
        row = con.execute(
            """
            SELECT id, run_id, step_id, tool, source, trust_level,
                   content_type, content, summary, redaction_status,
                   bytes_in, bytes_out, created_at
            FROM agent_observations
            WHERE id = ?
            """,
            (observation_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)


def suspend_agent_run(run_id: str) -> None:
    """Mark a run suspended (pending human approval)."""
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_runs
            SET status = 'suspended'
            WHERE id = ?
            """,
            (run_id,),
        )


def approve_agent_step(run_id: str, step_id: int, approver_user_id: Optional[str] = None) -> None:
    """Mark a pending approval step as approved."""
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_steps
            SET status = 'ok', approver_user_id = ?
            WHERE run_id = ? AND id = ?
            """,
            (approver_user_id, run_id, step_id),
        )


def resume_agent_run(run_id: str) -> bool:
    """Flip a suspended run back to 'running'. Returns True only if the flip
    actually happened (i.e. status was 'suspended'). Idempotent under concurrent approvals."""
    with _conn() as con:
        cur = con.execute(
            """
            UPDATE agent_runs
            SET status = 'running', error = NULL
            WHERE id = ? AND status = 'suspended'
            """,
            (run_id,),
        )
        return (cur.rowcount or 0) == 1


def reject_agent_step(run_id: str, step_id: int) -> None:
    """Mark a pending approval step as rejected."""
    with _conn() as con:
        con.execute(
            """
            UPDATE agent_steps
            SET status = 'error',
                error_type = 'approval_rejected',
                error_message = 'User rejected the operation.'
            WHERE run_id = ? AND id = ?
            """,
            (run_id, step_id),
        )


def aggregate_run_costs(
    group_by: str = "user",
    since: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """Aggregate token usage and cost metrics from agent_runs.

    group_by can be "user", "day", or "model".
    since is an ISO timestamp / date string.
    user_id is an optional filter.
    """
    if group_by == "day":
        group_col = "date(started_at)"
        group_name = "day"
    elif group_by == "model":
        group_col = "model"
        group_name = "model"
    else:  # default to user
        group_col = "user_id"
        group_name = "user_id"

    where_clauses = []
    params = []

    if since:
        where_clauses.append("started_at >= ?")
        params.append(since)
    if user_id:
        where_clauses.append("user_id = ?")
        params.append(user_id)

    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)

    query = f"""
        SELECT 
            coalesce({group_col}, 'unknown') AS {group_name},
            SUM(coalesce(total_tokens_in, 0)) AS total_tokens_in,
            SUM(coalesce(total_tokens_out, 0)) AS total_tokens_out,
            SUM(coalesce(total_cached_tokens_in, 0)) AS total_cached_tokens_in,
            SUM(coalesce(total_cost_usd, 0.0)) AS total_cost_usd,
            COUNT(*) AS run_count
        FROM agent_runs
        {where_str}
        GROUP BY {group_col}
        ORDER BY {group_name} ASC
    """

    with _conn() as con:
        rows = con.execute(query, tuple(params)).fetchall()

    result_rows = [dict(row) for row in rows]

    # Calculate totals
    totals = {
        "total_tokens_in": sum(r["total_tokens_in"] for r in result_rows),
        "total_tokens_out": sum(r["total_tokens_out"] for r in result_rows),
        "total_cached_tokens_in": sum(r["total_cached_tokens_in"] for r in result_rows),
        "total_cost_usd": sum(r["total_cost_usd"] for r in result_rows),
        "run_count": sum(r["run_count"] for r in result_rows),
    }

    return {
        "rows": result_rows,
        "totals": totals
    }
