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
            # Was this answer any good? Chat has had thumbs for a while and
            # promotes good answers into the runbook collection; alert-driven
            # investigations produced RCAs nobody could rate, so a playbook
            # that consistently produced a wrong answer looked the same as one
            # that always worked.
            # Why an investigation is in the status it is in — currently only
            # written for `needs_config`, where "nothing was investigated" is
            # useless without "because no cluster is registered as prod-eu".
            # Kept separate from feedback_notes: a routing reason is not a
            # human's verdict, and sharing the column would have mixed the two
            # in any query that read either.
            "status_reason": "TEXT",
            "feedback_rating": "TEXT",
            "feedback_notes": "TEXT",
            "feedback_at": "TEXT",
            "feedback_by": "TEXT",
            "incident_id": "TEXT",
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
        # Silences. A known-broken condition — a bad rollout being rolled back,
        # a namespace under maintenance — otherwise produces an LLM-backed
        # investigation per alert per repeat interval, for a cause the operator
        # already knows.
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_silences (
                id            TEXT PRIMARY KEY,
                matchers      TEXT NOT NULL,
                reason        TEXT NOT NULL,
                created_by    TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                revoked_at    TEXT,
                matched_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_alert_silences_expiry
                ON alert_silences(expires_at);

            -- Incidents. Three alerts about one crashlooping pod are one
            -- problem; investigating each separately costs three LLM runs and
            -- produces three answers, none of which mention the others.
            CREATE TABLE IF NOT EXISTS incidents (
                id             TEXT PRIMARY KEY,
                namespace      TEXT NOT NULL,
                workload       TEXT NOT NULL,
                opened_at      TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                closed_at      TEXT,
                alert_count    INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_open
                ON incidents(namespace, workload, closed_at, last_active_at);

            -- Which cluster an alert is about, and how to reach it. No secret
            -- material lives here: `credential_ref` names a mounted secret,
            -- so a database dump does not hand over cluster access.
            -- Proposed fixes awaiting a human. A row here has changed
            -- nothing: it is a suggestion, and only `approved_at` plus a
            -- deliberate execute call can make it act.
            CREATE TABLE IF NOT EXISTS remediation_proposals (
                id                TEXT PRIMARY KEY,
                investigation_id  TEXT NOT NULL,
                cluster_id        TEXT,
                action            TEXT NOT NULL,
                arguments         TEXT NOT NULL,
                rationale         TEXT,
                status            TEXT NOT NULL DEFAULT 'pending',
                proposed_at       TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                decided_at        TEXT,
                decided_by        TEXT,
                decision_note     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_remediation_pending
                ON remediation_proposals(status, expires_at);

            CREATE TABLE IF NOT EXISTS cluster_registry (
                id              TEXT PRIMARY KEY,
                display_name    TEXT,
                ssh_host        TEXT NOT NULL,
                ssh_port        INTEGER NOT NULL DEFAULT 22,
                ssh_user        TEXT NOT NULL,
                credential_ref  TEXT NOT NULL,
                kubectl_context TEXT,
                status          TEXT NOT NULL DEFAULT 'active',
                registered_at   TEXT NOT NULL
            );
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_investigations_incident "
            "ON investigations(incident_id)"
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


def list_cluster_connections() -> list[dict]:
    """Every session's cluster connection.

    Used by the startup prune to decide which uploaded kubeconfigs are still
    referenced. Returns rows, not paths, so callers can filter as they need.
    """
    with _conn() as con:
        rows = con.execute(
            """
            SELECT session_id, mode, context_name, cluster_name, kubeconfig_path
            FROM cluster_connections
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
# Every InvestigationStatus must appear in exactly one of these two sets. They
# are written out rather than derived from each other because deriving one from
# the other is self-consistent for any enum: a new status added to the enum
# would quietly fall into "open" and be treated as in-flight forever. Spelling
# both out means a new status fails the partition test until somebody decides
# which it is.
TERMINAL_INVESTIGATION_STATUSES = frozenset(
    {"completed", "failed", "resolved", "needs_config"}
)
ACTIVE_INVESTIGATION_STATUSES = frozenset({"received", "classified", "running"})


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
            SELECT id, occurrence_count, status, created_at, incident_id
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


# ── Alert silences ────────────────────────────────────────────────────────
#
# A silence stops an alert from starting an investigation. It does not stop
# Alertmanager delivering it, and deliberately so: an operator often wants the
# raw signal to keep reaching on-call while the assistant stops spending tokens
# on a cause they already understand. It also covers every source we ingest,
# not just Alertmanager.
#
# Matches are counted rather than discarded, because a silence nobody can see
# working is one nobody trusts — and a silence matching far more than expected
# is the first sign its matchers are too broad.


def create_silence(
    silence_id: str,
    matchers: list[dict],
    reason: str,
    created_by: str,
    ttl_seconds: int,
) -> dict:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    with _conn() as con:
        con.execute(
            "INSERT INTO alert_silences "
            "(id, matchers, reason, created_by, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                silence_id,
                json.dumps(matchers),
                reason,
                created_by,
                now.isoformat(),
                expires.isoformat(),
            ),
        )
    return {
        "id": silence_id,
        "matchers": matchers,
        "reason": reason,
        "created_by": created_by,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "revoked_at": None,
        "matched_count": 0,
    }


def _row_to_silence(row) -> dict:
    silence = dict(row)
    try:
        silence["matchers"] = json.loads(silence["matchers"])
    except (TypeError, ValueError):
        # A row we cannot parse must not match anything. Returning it with no
        # matchers would make it match *everything* under AND semantics.
        silence["matchers"] = None
    return silence


def list_active_silences() -> list[dict]:
    """Silences in force right now: not revoked and not expired.

    Expiry is evaluated in the query rather than by a sweeper, so a silence
    stops applying the moment its TTL passes even if nothing has run since.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM alert_silences "
            "WHERE revoked_at IS NULL AND expires_at > ? "
            "ORDER BY created_at DESC",
            (now,),
        ).fetchall()
    return [_row_to_silence(r) for r in rows]


def list_all_silences() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM alert_silences ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_silence(r) for r in rows]


def get_silence(silence_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM alert_silences WHERE id = ?", (silence_id,)
        ).fetchone()
    return _row_to_silence(row) if row else None


def revoke_silence(silence_id: str) -> bool:
    """End a silence early. Returns False if it was already over.

    Kept as a revocation stamp rather than a delete: "who silenced this, and
    for how long" is the first question after an alert nobody saw.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE alert_silences SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL AND expires_at > ?",
            (now, silence_id, now),
        )
        return cur.rowcount > 0


def record_silence_match(silence_id: str) -> None:
    """Incremented in SQL, not read-modify-write: Alertmanager posts batches
    and this runs once per alert in one."""
    with _conn() as con:
        con.execute(
            "UPDATE alert_silences SET matched_count = matched_count + 1 "
            "WHERE id = ?",
            (silence_id,),
        )


# ── Incidents ─────────────────────────────────────────────────────────────
#
# Adjacent-in-time alerts about the same workload almost always share a root
# cause: a crashlooping pod fires CrashLoopBackOff, then OOMKilled, then a
# probe failure. Attaching them to one incident is what lets the UI show one
# problem instead of three, and what makes "how many incidents this week" a
# number that means something.
#
# Correlation is fail-soft everywhere. It is an enhancement to ingestion, and
# no failure of it should stop an alert being investigated.


def find_or_open_incident(
    namespace: str,
    workload: str,
    window_minutes: int,
    max_lifetime_hours: int,
) -> Optional[str]:
    """The open incident for this workload, opening one if there is none.

    Returns None when the alert cannot be correlated, so the caller leaves
    `incident_id` unset rather than inventing a grouping.

    The window slides on last activity rather than on when the incident opened.
    A workload that keeps firing for an hour is one incident; anchoring to the
    open time would start a fresh one every window period and reintroduce
    exactly the fragmentation this removes.

    `max_lifetime_hours` is the backstop for an incident that never closes.
    Alertmanager can be configured with `send_resolved: false`, in which case
    nothing ever tells us the condition ended — and without a cap that incident
    would keep absorbing alerts forever.
    """
    if not namespace or not workload:
        return None

    now = datetime.now(timezone.utc)
    active_since = (now - timedelta(minutes=window_minutes)).isoformat()
    opened_since = (now - timedelta(hours=max_lifetime_hours)).isoformat()

    with _conn() as con:
        row = con.execute(
            """
            SELECT id FROM incidents
            WHERE namespace = ? AND workload = ?
              AND closed_at IS NULL
              AND last_active_at >= ?
              AND opened_at >= ?
            ORDER BY last_active_at DESC
            LIMIT 1
            """,
            (namespace, workload, active_since, opened_since),
        ).fetchone()

        if row:
            con.execute(
                "UPDATE incidents "
                "SET last_active_at = ?, alert_count = alert_count + 1 "
                "WHERE id = ?",
                (now.isoformat(), row["id"]),
            )
            return row["id"]

        incident_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO incidents "
            "(id, namespace, workload, opened_at, last_active_at, alert_count) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (incident_id, namespace, workload, now.isoformat(), now.isoformat()),
        )
        return incident_id


def attach_to_incident(investigation_id: str, incident_id: str) -> None:
    """Set once at ingest.

    Kept out of the orchestrator's document save for the same reason as
    occurrence_count: that save rewrites the row from an object built before
    correlation ran, and would clobber it back to NULL.
    """
    with _conn() as con:
        con.execute(
            "UPDATE investigations SET incident_id = ? WHERE id = ?",
            (incident_id, investigation_id),
        )


def close_incident_if_settled(incident_id: str) -> bool:
    """Close the incident once every investigation attached to it is terminal.

    Returns True if this call closed it. An incident with no investigations is
    left open — it was just created, and closing it would immediately reopen a
    new one for the next alert of the same problem.
    """
    placeholders = ",".join("?" * len(OPEN_INVESTIGATION_STATUSES))
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        row = con.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IN ({placeholders}) THEN 1 ELSE 0 END) AS open
            FROM investigations WHERE incident_id = ?
            """,
            (*OPEN_INVESTIGATION_STATUSES, incident_id),
        ).fetchone()

        if not row or not row["total"] or row["open"]:
            return False

        cur = con.execute(
            "UPDATE incidents SET closed_at = ? "
            "WHERE id = ? AND closed_at IS NULL",
            (now, incident_id),
        )
        return cur.rowcount > 0


def get_incident(incident_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if not row:
            return None
        incident = dict(row)
        incident["investigations"] = [
            dict(r)
            for r in con.execute(
                "SELECT id, status, severity, source, created_at "
                "FROM investigations WHERE incident_id = ? ORDER BY created_at",
                (incident_id,),
            ).fetchall()
        ]
    return incident


def list_incidents(limit: int = 50, include_closed: bool = False) -> list[dict]:
    clause = "" if include_closed else "WHERE closed_at IS NULL"
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM incidents {clause} "
            f"ORDER BY last_active_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        incidents = []
        for row in rows:
            incident = dict(row)
            incident["investigation_ids"] = [
                r["id"]
                for r in con.execute(
                    "SELECT id FROM investigations WHERE incident_id = ? "
                    "ORDER BY created_at",
                    (incident["id"],),
                ).fetchall()
            ]
            incidents.append(incident)
    return incidents


# ── Feedback on investigations ────────────────────────────────────────────
#
# An alert investigation produces a root-cause answer chosen by a playbook.
# Until now nothing recorded whether that answer was right, so a playbook that
# was consistently wrong was indistinguishable from one that always worked —
# and the only way to find out was for somebody to remember.
#
# A rating alone is a number. What makes a bad answer fixable is the note
# beside it, which is why `feedback_notes` is stored verbatim and surfaced in
# the summary rather than being reduced to a count.

FEEDBACK_RATINGS = ("up", "down")


def record_investigation_feedback(
    investigation_id: str,
    rating: str,
    notes: str = "",
    rated_by: str = "",
) -> bool:
    """Attach a verdict to an investigation. Returns False if there is no such
    investigation.

    A rating can be changed — someone marks an answer wrong, then finds it was
    right after all — so this overwrites rather than appending. The timestamp
    moves with it, so the summary reflects the current view rather than the
    first impression.
    """
    if rating not in FEEDBACK_RATINGS:
        raise ValueError(
            f"rating must be one of {FEEDBACK_RATINGS}, got {rating!r}"
        )

    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE investigations "
            "SET feedback_rating = ?, feedback_notes = ?, feedback_at = ?, "
            "    feedback_by = ? "
            "WHERE id = ?",
            (rating, notes or None, now, rated_by or None, investigation_id),
        )
        return cur.rowcount > 0


def _playbook_of(document: Optional[str]) -> str:
    """Which playbook produced this answer.

    Read from the stored document rather than a column because that is where
    the orchestrator already records it. An investigation that failed before
    classification has none, and is grouped under "" — worth seeing separately,
    since a pile of unclassified failures is its own problem.
    """
    if not document:
        return ""
    try:
        return str(json.loads(document).get("selected_playbook") or "")
    except (TypeError, ValueError):
        return ""


def investigation_feedback_summary(within_days: int = 30) -> list[dict]:
    """Per-playbook verdict counts, worst first.

    Ordered by down-rate so the playbook most in need of editing is the first
    thing read. Ties break on volume: a playbook wrong twice out of two matters
    less than one wrong thirty times out of forty.

    Sample notes travel with the counts. A rate tells you *that* a playbook is
    failing; only the text tells you how.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
    with _conn() as con:
        rows = con.execute(
            "SELECT document, feedback_rating, feedback_notes "
            "FROM investigations "
            "WHERE feedback_rating IS NOT NULL AND feedback_at >= ?",
            (cutoff,),
        ).fetchall()

    by_playbook: dict[str, dict] = {}
    for row in rows:
        playbook = _playbook_of(row["document"])
        entry = by_playbook.setdefault(
            playbook, {"playbook": playbook, "up": 0, "down": 0, "sample_notes": []}
        )
        entry[row["feedback_rating"]] += 1
        if row["feedback_rating"] == "down" and row["feedback_notes"]:
            if len(entry["sample_notes"]) < 5:
                entry["sample_notes"].append(row["feedback_notes"])

    summary = []
    for entry in by_playbook.values():
        total = entry["up"] + entry["down"]
        entry["total"] = total
        entry["down_rate"] = round(entry["down"] / total, 3) if total else 0.0
        summary.append(entry)

    summary.sort(key=lambda e: (-e["down_rate"], -e["total"]))
    return summary


# ── Cluster registry ──────────────────────────────────────────────────────
#
# One assistant, several clusters, each with its own Prometheus pointing here.
# Without a registry every alert was investigated against whatever the backend
# happened to be aimed at — so an alert from staging produced a confident,
# fully-evidenced answer about production. Wrong answers about the wrong
# cluster are worse than no answer, because nothing about them looks wrong.
#
# An empty registry means single-cluster mode and changes nothing: existing
# deployments keep using the default target and cluster labels are ignored.
#
# No credential material is stored. `credential_ref` names a secret mounted
# into the pod, so this table can be dumped without handing over any cluster.


CLUSTER_STATUSES = ("active", "disabled")


def register_cluster(
    cluster_id: str,
    ssh_host: str,
    ssh_user: str,
    credential_ref: str,
    display_name: str = "",
    ssh_port: int = 22,
    kubectl_context: str = "",
    status: str = "active",
) -> dict:
    """Add or replace a cluster. The id is the value alerts carry in their
    `cluster` label, which is what makes routing possible at all."""
    if status not in CLUSTER_STATUSES:
        raise ValueError(f"status must be one of {CLUSTER_STATUSES}, got {status!r}")

    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute(
            "INSERT INTO cluster_registry "
            "(id, display_name, ssh_host, ssh_port, ssh_user, credential_ref, "
            " kubectl_context, status, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  display_name=excluded.display_name, ssh_host=excluded.ssh_host, "
            "  ssh_port=excluded.ssh_port, ssh_user=excluded.ssh_user, "
            "  credential_ref=excluded.credential_ref, "
            "  kubectl_context=excluded.kubectl_context, status=excluded.status",
            (
                cluster_id,
                display_name or cluster_id,
                ssh_host,
                ssh_port,
                ssh_user,
                credential_ref,
                kubectl_context or None,
                status,
                now,
            ),
        )
    return get_cluster(cluster_id)


def get_cluster(cluster_id: str) -> Optional[dict]:
    if not cluster_id:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM cluster_registry WHERE id = ?", (cluster_id,)
        ).fetchone()
    return dict(row) if row else None


def list_clusters(include_disabled: bool = True) -> list[dict]:
    clause = "" if include_disabled else "WHERE status = 'active'"
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM cluster_registry {clause} ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def registry_is_empty() -> bool:
    """True when no cluster has ever been registered.

    This is the switch between single-cluster and multi-cluster behaviour, and
    it is deliberately "has any row" rather than "has any active row":
    disabling every cluster should not silently send all alerts back to the
    default target, which is the one outcome nobody would intend by disabling
    things.
    """
    with _conn() as con:
        return con.execute("SELECT 1 FROM cluster_registry LIMIT 1").fetchone() is None


def remove_cluster(cluster_id: str) -> bool:
    with _conn() as con:
        return con.execute(
            "DELETE FROM cluster_registry WHERE id = ?", (cluster_id,)
        ).rowcount > 0


def mark_investigation_needs_config(investigation_id: str, reason: str) -> None:
    """Record that an alert arrived for a cluster we cannot reach.

    The investigation still exists so the operator sees the alert — the point
    is that nothing was investigated, not that nothing happened.
    """
    with _conn() as con:
        con.execute(
            "UPDATE investigations SET status = 'needs_config', "
            "status_reason = ? WHERE id = ?",
            (reason, investigation_id),
        )


# ── Remediation proposals ─────────────────────────────────────────────────
#
# A proposal is a suggestion, not an action. Creating one changes nothing in
# any cluster; only an explicit approval followed by an explicit execute can.
# The two are separate rows-worth of intent on purpose, so that no single call
# — and no single bug — takes something from "the model suggested this" to "the
# cluster was changed".
#
# Approvals expire. An approval given an hour ago was given about a cluster
# that no longer looks the way it did, and acting on it would be acting on a
# judgement nobody would make now.

PROPOSAL_STATUSES = ("pending", "approved", "rejected", "executed", "expired")


def create_remediation_proposal(
    proposal_id: str,
    investigation_id: str,
    action: str,
    arguments: dict,
    rationale: str,
    ttl_seconds: int,
    cluster_id: str = "",
) -> dict:
    now = datetime.now(timezone.utc)
    with _conn() as con:
        con.execute(
            "INSERT INTO remediation_proposals "
            "(id, investigation_id, cluster_id, action, arguments, rationale, "
            " status, proposed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                proposal_id,
                investigation_id,
                cluster_id or None,
                action,
                json.dumps(arguments),
                rationale,
                now.isoformat(),
                (now + timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )
    return get_remediation_proposal(proposal_id)


def _row_to_proposal(row) -> dict:
    proposal = dict(row)
    try:
        proposal["arguments"] = json.loads(proposal["arguments"])
    except (TypeError, ValueError):
        # Unparsable arguments must not be executable. Surfacing None rather
        # than {} keeps an empty-args action from being run by accident.
        proposal["arguments"] = None
    now = datetime.now(timezone.utc).isoformat()
    if proposal["status"] in ("pending", "approved") and proposal["expires_at"] <= now:
        # Evaluated on read rather than by a sweeper, so an expiry is in force
        # the moment it passes even if nothing has run since.
        proposal["status"] = "expired"
    return proposal


def get_remediation_proposal(proposal_id: str) -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM remediation_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
    return _row_to_proposal(row) if row else None


def list_remediation_proposals(
    investigation_id: str = "", pending_only: bool = False
) -> list[dict]:
    clauses, params = [], []
    if investigation_id:
        clauses.append("investigation_id = ?")
        params.append(investigation_id)
    if pending_only:
        clauses.append("status = 'pending'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _conn() as con:
        rows = con.execute(
            f"SELECT * FROM remediation_proposals {where} ORDER BY proposed_at DESC",
            params,
        ).fetchall()
    proposals = [_row_to_proposal(r) for r in rows]
    if pending_only:
        # A row can go stale between the query and the read, so filter again on
        # the computed status rather than trusting the stored one.
        proposals = [p for p in proposals if p["status"] == "pending"]
    return proposals


def decide_remediation_proposal(
    proposal_id: str, approve: bool, decided_by: str, note: str = ""
) -> Optional[dict]:
    """Approve or reject. Returns None if it was not decidable.

    Guarded on `pending` and on not having expired, so a proposal cannot be
    approved twice, approved after rejection, or revived after expiry — each of
    which would turn a stale judgement into a live authorisation.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE remediation_proposals "
            "SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? "
            "WHERE id = ? AND status = 'pending' AND expires_at > ?",
            (
                "approved" if approve else "rejected",
                now,
                decided_by,
                note or None,
                proposal_id,
                now,
            ),
        )
        if cur.rowcount == 0:
            return None
    return get_remediation_proposal(proposal_id)


def mark_remediation_executed(proposal_id: str) -> bool:
    """Consume an approval. Returns False unless it was approved and unexpired.

    One-shot: an executed proposal cannot be executed again, so a retried
    request cannot restart a deployment twice.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        return con.execute(
            "UPDATE remediation_proposals SET status = 'executed' "
            "WHERE id = ? AND status = 'approved' AND expires_at > ?",
            (proposal_id, now),
        ).rowcount > 0


def count_recent_remediations(within_minutes: int = 60) -> int:
    """Executed remediations in the window, across everything.

    Deliberately global rather than per-deployment: the failure being bounded
    is "the system is acting far more than anyone intended", and a per-target
    cap misses a storm spread across fifty targets.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) AS c FROM remediation_proposals "
            "WHERE status = 'executed' AND decided_at >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["c"]) if row else 0


def record_remediation_result(proposal_id: str, note: str) -> None:
    """Attach the outcome to the proposal, so the row is the whole story:
    what was proposed, who approved it, and what happened."""
    with _conn() as con:
        con.execute(
            "UPDATE remediation_proposals SET decision_note = "
            "COALESCE(decision_note || ' | ', '') || ? WHERE id = ?",
            (note, proposal_id),
        )
