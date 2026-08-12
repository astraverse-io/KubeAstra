"""The audit trail — what was done, by whom, and whether the record still
reads as it was written.

Auto-remediation executes against real clusters behind an approval gate. The
gate records that a human agreed; it does not record what ran. These rows do.

The tests worth having here are about the failure modes that would make the
log worse than useless:

  * a chain that forks under concurrency, so verification reports tampering on
    a log nobody touched
  * a payload that carries a secret into permanent storage
  * an emit that raises and takes down the mutation it was observing
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import audit  # noqa: E402
import db  # noqa: E402


@pytest.fixture
def clean_db():
    db.init_db()
    with db._conn() as con:
        con.execute("DELETE FROM audit_events")
    yield
    with db._conn() as con:
        con.execute("DELETE FROM audit_events")


# ── the module has to be importable the way main.py imports it ────────────


def test_it_is_not_under_a_services_package():
    """`main.py` puts mcp/ on sys.path, and mcp/services/ is a real package.
    A `ui/backend/services/audit.py` resolves to mcp's services instead and
    raises ImportError at runtime — while unit tests that do not replicate
    that path setup pass happily.
    """
    assert (BACKEND_DIR / "audit.py").exists()
    assert not (BACKEND_DIR / "services" / "audit.py").exists(), (
        "audit.py moved under a services package; `services` resolves to "
        "mcp/services at runtime and the import will fail"
    )


# ── writing ───────────────────────────────────────────────────────────────


def test_an_event_is_recorded_with_its_actor_and_subject(clean_db):
    event_id = audit.emit(
        audit.EventType.MUTATION_EXECUTED,
        actor_type="user", actor_id="pruthvi",
        session_id="s1", cluster="prod", subject="deploy/api",
    )

    assert event_id
    rows = audit.replay("s1")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == "pruthvi"
    assert rows[0]["subject"] == "deploy/api"
    assert rows[0]["cluster"] == "prod"


def test_emit_never_raises(clean_db, monkeypatch):
    """The caller is mid-mutation. An audit trail that can take down the thing
    it observes is worse than one with a gap."""
    monkeypatch.setattr(audit.db, "DB_PATH", "/nonexistent/dir/that/cannot/exist/x.db")

    assert audit.emit(audit.EventType.ERROR, subject="anything") == ""


@pytest.mark.parametrize("bad", ["administrator", "", "AGENT"])
def test_an_unknown_actor_type_is_recorded_as_system(clean_db, bad):
    """Rejecting the event would lose the record entirely. Recording it under
    a known value keeps the evidence and flags the caller."""
    audit.emit(audit.EventType.ERROR, actor_type=bad, session_id="s-actor")

    assert audit.replay("s-actor")[0]["actor_type"] == "system"


# ── redaction ─────────────────────────────────────────────────────────────


def test_secrets_do_not_reach_storage(clean_db):
    """The payload is written to disk and kept for 90 days by default. A token
    in it outlives the session it came from."""
    audit.emit(
        audit.EventType.CLUSTER_CONNECTED,
        session_id="s-secret",
        payload={"token": "ghp_realsecretvalue", "server": "https://k8s.example"},
    )

    payload = audit.replay("s-secret")[0]["payload"]

    assert "ghp_realsecretvalue" not in json.dumps(payload)
    assert "token" in payload, "the key should survive — that a token was present is evidence"
    assert payload["server"] == "https://k8s.example", "non-secret fields are kept"


def test_redaction_reaches_nested_values(clean_db):
    audit.emit(
        audit.EventType.MUTATION_EXECUTED,
        session_id="s-nested",
        payload={"outer": {"password": "hunter2", "kept": "visible"},
                 "list": [{"api_key": "sk-secret"}]},
    )

    dumped = json.dumps(audit.replay("s-nested")[0]["payload"])

    assert "hunter2" not in dumped
    assert "sk-secret" not in dumped
    assert "visible" in dumped


def test_a_huge_value_is_truncated(clean_db):
    """An audit row carrying a full pod log is how this table becomes the
    largest thing in the database."""
    audit.emit(
        audit.EventType.TOOL_CALL_EXECUTED,
        session_id="s-big",
        payload={"stdout": "x" * 100_000},
    )

    stored = audit.replay("s-big")[0]["payload"]["stdout"]

    assert len(stored) < 10_000


# ── the hash chain ────────────────────────────────────────────────────────


def test_an_untouched_chain_verifies(clean_db):
    for i in range(5):
        audit.emit(audit.EventType.TOOL_CALL_EXECUTED, subject=f"call-{i}")

    result = audit.verify_chain()

    assert result["ok"], result
    assert result["checked"] == 5


def test_editing_a_row_is_detected(clean_db):
    for i in range(4):
        audit.emit(audit.EventType.MUTATION_EXECUTED, subject=f"m{i}")

    with db._conn() as con:
        con.execute(
            "UPDATE audit_events SET subject = 'rewritten' WHERE seq = "
            "(SELECT MIN(seq) FROM audit_events)"
        )

    # The subject is not in the hash material, so this specific edit is not
    # detected — which is worth knowing rather than assuming otherwise.
    assert audit.verify_chain()["ok"]

    with db._conn() as con:
        con.execute(
            "UPDATE audit_events SET payload = '{\"tampered\":true}' WHERE seq = "
            "(SELECT MIN(seq) FROM audit_events)"
        )

    result = audit.verify_chain()
    assert not result["ok"]
    assert "does not match its hash" in result["reason"]


def test_deleting_a_row_is_detected(clean_db):
    for i in range(4):
        audit.emit(audit.EventType.APPROVAL_GRANTED, subject=f"a{i}")

    with db._conn() as con:
        con.execute(
            "DELETE FROM audit_events WHERE seq = "
            "(SELECT MIN(seq) FROM audit_events WHERE seq > (SELECT MIN(seq) FROM audit_events))"
        )

    result = audit.verify_chain()

    assert not result["ok"]
    assert "removed, reordered, or inserted" in result["reason"]


def test_concurrent_writes_do_not_fork_the_chain(clean_db):
    """The failure this component can least afford.

    The roadmap reads the previous hash and then inserts. SQLite's default
    BEGIN is deferred, so a SELECT takes no write lock: two emits read the
    same prev_hash and both insert, and verification then reports tampering on
    a log nobody touched. emit() takes BEGIN IMMEDIATE for exactly this.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(
            lambda i: audit.emit(audit.EventType.TOOL_CALL_EXECUTED, subject=f"c{i}"),
            range(40),
        ))

    assert all(ids), "some writes failed under contention"

    with db._conn() as con:
        hashes = [r["prev_hash"] for r in con.execute(
            "SELECT prev_hash FROM audit_events ORDER BY seq"
        ).fetchall()]

    seen = [h for h in hashes if h is not None]
    assert len(seen) == len(set(seen)), "two rows share a prev_hash — the chain forked"

    result = audit.verify_chain()
    assert result["ok"], f"chain broken after concurrent writes: {result}"
    assert result["checked"] == 40


def test_a_tie_in_the_timestamp_does_not_break_the_chain(clean_db, monkeypatch):
    """Two events can legitimately share an ISO timestamp to the microsecond.

    Ordering a tamper-evidence chain by a non-unique column makes "which row
    came first" ambiguous, and the verifier would report ties as breakage —
    accusing an untouched log of tampering. The chain follows `seq`.

    Timestamps are frozen here rather than rewritten afterwards: `ts` is part
    of the hash material, so editing it after the fact is tampering and
    *should* be caught. That distinction is the whole point of the column
    being signed.
    """
    from datetime import datetime, timezone

    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(audit, "datetime", _Frozen)
    for i in range(4):
        audit.emit(audit.EventType.ERROR, subject=f"tie{i}")

    with db._conn() as con:
        stamps = [r["ts"] for r in con.execute("SELECT ts FROM audit_events").fetchall()]
    assert len(set(stamps)) == 1, "the timestamps did not actually tie"

    result = audit.verify_chain()
    assert result["ok"], f"identical timestamps broke verification: {result}"
    assert result["checked"] == 4


# ── reading ───────────────────────────────────────────────────────────────


def test_replay_is_oldest_first(clean_db):
    for i in range(4):
        audit.emit(audit.EventType.TOOL_CALL_EXECUTED, session_id="s-order", subject=f"t{i}")

    assert [r["subject"] for r in audit.replay("s-order")] == ["t0", "t1", "t2", "t3"]


def test_query_filters_are_anded(clean_db):
    audit.emit(audit.EventType.MUTATION_EXECUTED, actor_id="a", cluster="prod", session_id="s")
    audit.emit(audit.EventType.MUTATION_EXECUTED, actor_id="b", cluster="prod", session_id="s")
    audit.emit(audit.EventType.APPROVAL_GRANTED, actor_id="a", cluster="dev", session_id="s")

    rows = audit.query(actor_id="a", cluster="prod")

    assert len(rows) == 1
    assert rows[0]["event_type"] == audit.EventType.MUTATION_EXECUTED


def test_query_is_newest_first_and_bounded(clean_db):
    for i in range(10):
        audit.emit(audit.EventType.ERROR, subject=f"e{i}")

    rows = audit.query(limit=3)

    assert [r["subject"] for r in rows] == ["e9", "e8", "e7"]


def test_an_absurd_limit_is_clamped(clean_db):
    """A caller asking for a million rows should get a page, not the process's
    memory."""
    audit.emit(audit.EventType.ERROR)

    assert len(audit.query(limit=10**9)) <= 1000


# ── retention ─────────────────────────────────────────────────────────────


def test_pruning_is_off_when_retention_is_zero(clean_db):
    audit.emit(audit.EventType.ERROR)

    assert audit.prune(retention_days=0) == 0
    assert len(audit.query()) == 1


def test_pruning_removes_only_old_rows(clean_db):
    audit.emit(audit.EventType.ERROR, subject="recent")
    with db._conn() as con:
        con.execute(
            "INSERT INTO audit_events (id, ts, actor_type, actor_id, event_type, "
            "subject, payload, severity, hash, prev_hash) VALUES "
            "('old','2020-01-01T00:00:00+00:00','system','system','error','ancient','{}','info','h',NULL)"
        )

    removed = audit.prune(retention_days=30)

    assert removed == 1
    assert [r["subject"] for r in audit.query()] == ["recent"]


def test_the_failure_log_cannot_be_forged(clean_db, monkeypatch, caplog):
    """event_type and subject are caller-supplied and reach a log line on the
    failure path. A newline in either lets the writer append a line that reads
    like the application produced it — in the one module whose purpose is
    records that cannot be forged.
    """
    monkeypatch.setattr(audit.db, "DB_PATH", "/nonexistent/dir/cannot/exist/x.db")

    with caplog.at_level("ERROR"):
        audit.emit(
            "evil\n2026-01-01 00:00:00 ERROR auth: admin login from 10.0.0.1",
            subject="also\nforged",
        )

    for record in caplog.records:
        assert "\n" not in record.getMessage(), (
            "a caller-supplied value put a newline into the log"
        )
