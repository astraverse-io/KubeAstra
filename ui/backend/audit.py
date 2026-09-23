"""Audit trail — an append-only record of what was done, and by whom.

Auto-remediation executes against real clusters behind an approval gate. The
gate says a human agreed; it does not say what ran, against which cluster, or
what came back. This is that record.

Rows form a hash chain: each row's `hash` covers its own content plus the
previous row's hash, so removing or editing a row breaks every hash after it.
That makes tampering *evident*. It does not make it impossible — anyone who
can write the database can rewrite the whole chain — and the docstring says so
rather than implying a guarantee the storage cannot provide.

Four things here deliberately differ from FEATURE_ROADMAP.md § 3:

* **It lives at `ui/backend/audit.py`, not `ui/backend/services/audit.py`.**
  `main.py` puts `mcp/` on sys.path, and `mcp/services/` is a real package
  with an `__init__.py`, so `services` resolves there and
  `from services import audit` raises ImportError at runtime. Unit tests that
  do not replicate main.py's path setup pass anyway. `ui/backend` is flat —
  db.py, auth.py, log_safety.py, cluster_session.py — and this follows that.

* **`seq`, not `ts`, orders the chain.** Two events can share an ISO timestamp
  to the microsecond. Ordering a tamper-evidence chain by a non-unique column
  makes "which row came first" ambiguous, and the verifier would report
  breakage that is really just a tie.

* **`BEGIN IMMEDIATE` around read-then-write.** The spec reads the previous
  hash and then inserts. SQLite's default transaction is deferred, so a SELECT
  takes no write lock: two concurrent emits read the same `prev_hash` and both
  insert, forking the chain. The verifier then reports tampering on a log
  nobody touched — the worst failure this component can have, because it
  destroys trust in the honest case.

* **No `ulid` dependency.** The spec's own dependency section says no new
  backend deps and then imports `python-ulid`. `seq` already provides
  ordering, so the id only needs to be unique.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
import log_safety
from redaction_kv import redact_value

logger = logging.getLogger(__name__)

# 0 disables pruning. A cluster operator who wants an indefinite record should
# not have to discover that 90 was silently applied.
RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "90"))

# Payload values are operational detail, not prose. Long values are truncated
# rather than stored whole: an audit row carrying a full pod log is how the
# table becomes the largest thing in the database.
_VALUE_CAP = 2048

VALID_ACTOR_TYPES = frozenset({"user", "agent", "system"})
VALID_SEVERITIES = frozenset({"info", "warn", "critical"})


class EventType:
    """The catalogue. String constants rather than an enum so an unknown value
    from an older row still reads correctly."""

    # Session and access
    SESSION_CREATED = "session.created"
    SESSION_DELETED = "session.deleted"
    USER_SIGNED_IN = "user.signed_in"
    USER_SIGNED_OUT = "user.signed_out"
    CLUSTER_CONNECTED = "cluster.connected"
    CLUSTER_DISCONNECTED = "cluster.disconnected"
    CLUSTER_SWITCHED = "cluster.switched"

    # Investigation
    INVESTIGATION_STARTED = "investigation.started"
    TOOL_CALL_PROPOSED = "tool_call.proposed"
    TOOL_CALL_EXECUTED = "tool_call.executed"
    ROOT_CAUSE_CLASSIFIED = "root_cause.classified"
    ANSWER_STREAMED_COMPLETE = "answer.streamed_complete"
    FEEDBACK_RECORDED = "feedback.recorded"

    # Approval and mutation — the reason this component exists
    CONFIRMATION_TOKEN_ISSUED = "confirmation_token.issued"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    MUTATION_EXECUTED = "mutation.executed"
    ROLLBACK_TRIGGERED = "rollback.triggered"
    # Opening a PR mutates no cluster, so it is deliberately NOT
    # mutation.executed — that event is what an auditor reads to answer "what
    # changed my cluster", and a PR proposal must not pollute it.
    GITOPS_PR_OPENED = "gitops.pr_opened"

    # System
    ALERTMANAGER_WEBHOOK_RECEIVED = "alertmanager.webhook_received"
    PLAYBOOK_MATCHED = "playbook.matched"
    LLM_PROVIDER_FALLBACK = "llm.provider_fallback"
    RATE_LIMIT_HIT = "rate_limit.hit"
    ERROR = "error"


def redact_payload(payload: Optional[dict]) -> dict:
    """Strip secrets from a payload before it is written.

    redaction_kv exposes redact_value(key, value, cap) and no dict form — the
    roadmap imports a `redact_dict` that does not exist. This is that helper,
    built on the primitive that does.

    Keys are preserved: knowing a token *was* present is part of the record.
    """
    if not payload:
        return {}

    def clean(key: str, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(k, v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(key, v) for v in value]
        if isinstance(value, str):
            return redact_value(key, value, _VALUE_CAP)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return redact_value(key, str(value), _VALUE_CAP)

    return {k: clean(k, v) for k, v in payload.items()}


def _row_hash(
    event_id: str, ts: str, event_type: str, actor_id: str,
    payload_json: str, prev_hash: Optional[str],
) -> str:
    # `|` separated with no escaping is ambiguous — a payload containing the
    # separator could, in principle, be arranged to collide with a different
    # row. json.dumps of a list gives each field an unambiguous encoding.
    material = json.dumps(
        [event_id, ts, event_type, actor_id, payload_json, prev_hash or ""],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def emit(
    event_type: str,
    *,
    actor_type: str = "system",
    actor_id: str = "system",
    session_id: Optional[str] = None,
    cluster: Optional[str] = None,
    subject: Optional[str] = None,
    payload: Optional[dict] = None,
    severity: str = "info",
) -> str:
    """Write one event. Returns its id, or "" if the write failed.

    Never raises. An audit trail that can break the thing it observes is worse
    than one with a gap: the caller is usually mid-mutation, and failing there
    would turn a logging problem into an outage. Failures are logged loudly.
    """
    if actor_type not in VALID_ACTOR_TYPES:
        logger.warning("audit: unknown actor_type %r, recording as system", actor_type)
        actor_type = "system"
    if severity not in VALID_SEVERITIES:
        logger.warning("audit: unknown severity %r, recording as info", severity)
        severity = "info"

    event_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(redact_payload(payload), sort_keys=True)

    try:
        Path(db.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db.DB_PATH, check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            # The write lock must be held across the read of prev_hash and the
            # insert that depends on it. Under the default deferred BEGIN, two
            # emits read the same previous hash and both insert — a forked
            # chain, which verify() reports as tampering on an honest log.
            con.execute("BEGIN IMMEDIATE")
            prev = con.execute(
                "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev[0] if prev else None
            row_hash = _row_hash(
                event_id, ts, event_type, actor_id, payload_json, prev_hash
            )
            con.execute(
                """INSERT INTO audit_events
                   (id, ts, actor_type, actor_id, session_id, cluster,
                    event_type, subject, payload, severity, hash, prev_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, ts, actor_type, actor_id, session_id, cluster,
                 event_type, subject, payload_json, severity, row_hash, prev_hash),
            )
            con.commit()
        finally:
            con.close()
        return event_id
    except Exception as error:  # noqa: BLE001 — see the docstring
        # event_type and subject are caller-supplied. A newline in either lets
        # the writer append a line that reads like the application produced it.
        # Pointed, in a module whose whole purpose is records that cannot be
        # forged: the failure path was writing forgeable ones.
        logger.error(
            "audit: failed to record %s (%s): %s",
            log_safety.one_line(event_type),
            log_safety.one_line(subject),
            log_safety.one_line(error),
        )
        return ""


def verify_chain(limit: Optional[int] = None) -> dict:
    """Recompute every hash in order and report the first break.

    Returns {"ok": bool, "checked": int, "broken_at": seq|None, "reason": str}.

    A break means the row's stored hash does not match its content, or its
    prev_hash does not match the row before it. Both mean the log no longer
    reads as it was written.
    """
    with db._conn() as con:
        sql = "SELECT * FROM audit_events ORDER BY seq ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = con.execute(sql).fetchall()

    expected_prev: Optional[str] = None
    for index, row in enumerate(rows):
        recomputed = _row_hash(
            row["id"], row["ts"], row["event_type"], row["actor_id"],
            row["payload"], row["prev_hash"],
        )
        if recomputed != row["hash"]:
            return {
                "ok": False, "checked": index, "broken_at": row["seq"],
                "reason": "row content does not match its hash",
            }
        if row["prev_hash"] != expected_prev:
            return {
                "ok": False, "checked": index, "broken_at": row["seq"],
                "reason": "prev_hash does not match the preceding row — a row "
                          "was removed, reordered, or inserted",
            }
        expected_prev = row["hash"]

    return {"ok": True, "checked": len(rows), "broken_at": None, "reason": ""}


def query(
    *,
    session_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    cluster: Optional[str] = None,
    event_type: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Filtered read, newest first. Filters are ANDed."""
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("session_id", session_id), ("actor_id", actor_id),
        ("cluster", cluster), ("event_type", event_type),
        ("severity", severity),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([max(1, min(limit, 1000)), max(0, offset)])

    with db._conn() as con:
        rows = con.execute(
            f"SELECT * FROM audit_events {where} ORDER BY seq DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

    return [_as_dict(row) for row in rows]


def replay(session_id: str) -> list[dict]:
    """Every event for one session, oldest first — the order it happened in."""
    with db._conn() as con:
        rows = con.execute(
            "SELECT * FROM audit_events WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
        ).fetchall()
    return [_as_dict(row) for row in rows]


def _as_dict(row) -> dict:
    event = dict(row)
    try:
        event["payload"] = json.loads(event["payload"]) if event["payload"] else {}
    except (TypeError, ValueError):
        # A row whose payload will not parse is still a row worth returning.
        event["payload"] = {"_unparseable": True}
    return event


def prune(retention_days: Optional[int] = None) -> int:
    """Delete rows older than the retention window. Returns rows removed.

    Pruning necessarily breaks the chain at the seam — the oldest surviving row
    will reference a predecessor that no longer exists. verify_chain() reports
    that honestly rather than pretending otherwise; a log that quietly repaired
    its own history would be worth less than one with a visible seam.
    """
    days = RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()

    with db._conn() as con:
        cursor = con.execute("DELETE FROM audit_events WHERE ts < ?", (cutoff_iso,))
        return cursor.rowcount or 0
