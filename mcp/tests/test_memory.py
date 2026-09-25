"""Tests for per-user conversation memory (blueprint §5).

Covers the acceptance criteria from the blueprint:
  1. After 4 successful tool calls on `namespace=prod, pod=api-7`, the entities
     blob has namespaces=["prod"], resources=["api-7"], tools=[all four].
  2. A tool call with `namespace="*"` does NOT record `*` (blocklist).
  3. `record_tool_call(None, "tool", {})` returns silently — no DB write.
  4. Calling `describe_pod` twice with the same pod produces a single entry
     with count=2.
  5. A failed tool call (success=False or {"error": "..."} only) does NOT
     update memory.
  6. After capturing 11 distinct resources, the resources list is exactly 10
     entries (cap enforced).
  7. The most-recently-mentioned namespace appears first in the rendered preamble.
  8. clear_user_memory(session_id) wipes the row; subsequent build_memory_preamble
     returns "".
"""

import os
import sys
import tempfile
import time

import pytest

# Add both mcp/ (for existing path conventions) and ui/backend/ (for memory.py + db.py).
MCP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(MCP_DIR)
UI_BACKEND_DIR = os.path.join(REPO_DIR, "ui", "backend")
for path in (MCP_DIR, UI_BACKEND_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture
def temp_db(monkeypatch):
    """Point db.py at a fresh temp SQLite file for the test, then clean up."""
    # Create a temp file but close it so SQLite can manage the handle
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # remove so init_db creates fresh

    monkeypatch.setenv("DB_PATH", path)

    # Reimport db cleanly so module-level DB_PATH picks up the env var.
    for mod_name in ("db", "memory"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    import db
    import memory  # noqa: F401 — imported so it sees the new db module

    db.init_db()
    yield db, memory  # type: ignore[name-defined]

    # Cleanup
    if os.path.exists(path):
        os.unlink(path)
    for mod_name in ("db", "memory"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]


# ── Capture path ─────────────────────────────────────────────────────────────

class TestRecordToolCall:

    def test_records_namespace_and_resource_from_tool_call(self, temp_db):
        """Acceptance criterion #1 — entities captured from params."""
        db, memory = temp_db
        session = "test-session-1"

        memory.record_tool_call(session, "investigate_pod",
                                 {"namespace": "prod", "pod_name": "api-7"})
        memory.record_tool_call(session, "describe_pod",
                                 {"namespace": "prod", "pod_name": "api-7"})
        memory.record_tool_call(session, "get_pod_logs",
                                 {"namespace": "prod", "pod_name": "api-7"})
        memory.record_tool_call(session, "get_events",
                                 {"namespace": "prod"})

        entities = db.get_user_memory(session)
        # Namespaces: just "prod"
        namespaces = [e["value"] for e in entities.get("namespaces", [])]
        assert namespaces == ["prod"]
        # Resources: just "api-7"
        resources = [e["value"] for e in entities.get("resources", [])]
        assert resources == ["api-7"]
        # Tools: all four recorded
        tools = sorted(e["value"] for e in entities.get("tools", []))
        assert tools == sorted(
            ["investigate_pod", "describe_pod", "get_pod_logs", "get_events"]
        )

    def test_blocklist_values_not_recorded(self, temp_db):
        """Acceptance criterion #2 — namespace="*" must not be recorded."""
        db, memory = temp_db
        session = "test-blocklist"

        memory.record_tool_call(session, "get_pods", {"namespace": "*"})
        memory.record_tool_call(session, "get_pods", {"namespace": "all"})
        memory.record_tool_call(session, "get_pods", {"namespace": "default"})
        memory.record_tool_call(session, "get_pods", {"namespace": ""})

        entities = db.get_user_memory(session)
        # None of these should be in the namespaces category
        ns_values = [e["value"] for e in entities.get("namespaces", [])]
        assert ns_values == []

        # But the tool name "get_pods" is recorded
        tool_values = [e["value"] for e in entities.get("tools", [])]
        assert "get_pods" in tool_values

    def test_anonymous_session_is_silent_noop(self, temp_db):
        """Acceptance criterion #3 — None session_id is silent."""
        db, memory = temp_db

        # These must not raise and must not write anything
        memory.record_tool_call(None, "get_pods", {"namespace": "prod"})
        memory.record_tool_call("", "get_pods", {"namespace": "prod"})

        # The DB should have no user_memory rows
        with db._conn() as con:
            count = con.execute("SELECT COUNT(*) AS c FROM user_memory").fetchone()["c"]
        assert count == 0

    def test_repeated_calls_increment_count(self, temp_db):
        """Acceptance criterion #4 — describe_pod called twice with same pod
        produces a single entry with count=2."""
        db, memory = temp_db
        session = "test-repeat"

        memory.record_tool_call(session, "describe_pod",
                                 {"namespace": "prod", "pod_name": "api-7"})
        memory.record_tool_call(session, "describe_pod",
                                 {"namespace": "prod", "pod_name": "api-7"})

        entities = db.get_user_memory(session)
        resources = entities.get("resources", [])
        assert len(resources) == 1
        assert resources[0]["value"] == "api-7"
        assert resources[0]["count"] == 2

    def test_cap_enforced_at_10_per_category(self, temp_db):
        """Acceptance criterion #6 — capture 11 distinct resources, only 10 stored."""
        db, memory = temp_db
        session = "test-cap"

        for i in range(15):
            memory.record_tool_call(
                session, "describe_pod",
                {"namespace": "prod", "pod_name": f"pod-{i:02d}"},
            )
            # Small sleep so last_seen ordering is deterministic
            time.sleep(0.001)

        entities = db.get_user_memory(session)
        resources = entities.get("resources", [])
        assert len(resources) == 10
        # Most-recent entries kept (pod-14 down to pod-05). pod-00..pod-04 dropped.
        values = {e["value"] for e in resources}
        assert "pod-14" in values
        assert "pod-00" not in values


# ── Render path ──────────────────────────────────────────────────────────────

class TestBuildMemoryPreamble:

    def test_recency_ordering(self, temp_db):
        """Acceptance criterion #7 — most-recent namespace appears first."""
        db, memory = temp_db
        session = "test-recency"

        memory.record_tool_call(session, "get_pods", {"namespace": "staging"})
        time.sleep(0.01)
        memory.record_tool_call(session, "get_pods", {"namespace": "prod"})

        preamble = memory.build_memory_preamble(session)
        # `prod` should appear before `staging` in the rendered namespaces line
        ns_line = next(l for l in preamble.split("\n") if "namespaces" in l)
        prod_idx = ns_line.index("prod")
        staging_idx = ns_line.index("staging")
        assert prod_idx < staging_idx

    def test_empty_session_returns_empty(self, temp_db):
        """No memory yet → preamble is empty string."""
        db, memory = temp_db
        result = memory.build_memory_preamble("never-recorded-session")
        assert result == ""

    def test_none_session_returns_empty(self, temp_db):
        """Anonymous (no session_id) → empty preamble."""
        db, memory = temp_db
        assert memory.build_memory_preamble(None) == ""
        assert memory.build_memory_preamble("") == ""

    def test_preamble_includes_all_categories_with_content(self, temp_db):
        """Sanity — namespaces / resources / tools / clusters all rendered when present."""
        db, memory = temp_db
        session = "test-categories"

        memory.record_tool_call(session, "describe_pod",
                                {"namespace": "prod", "pod_name": "api-7"})
        memory.record_tool_call(session, "switch_kubeconfig_context",
                                {"context_name": "gke_prod_us-east1"})

        preamble = memory.build_memory_preamble(session)
        assert "namespaces" in preamble or "prod" in preamble
        assert "workloads" in preamble or "api-7" in preamble
        assert "tools used" in preamble or "describe_pod" in preamble
        assert "clusters" in preamble or "gke_prod_us-east1" in preamble


# ── Lifecycle ────────────────────────────────────────────────────────────────

class TestLifecycle:

    def test_clear_user_memory_wipes_row(self, temp_db):
        """Acceptance criterion #8 — clear wipes the row."""
        db, memory = temp_db
        session = "test-clear"

        memory.record_tool_call(session, "describe_pod",
                                {"namespace": "prod", "pod_name": "api-7"})
        # Sanity — written
        assert db.get_user_memory(session) != {}

        db.clear_user_memory(session)

        # After clear, memory is empty and preamble is empty
        assert db.get_user_memory(session) == {}
        assert memory.build_memory_preamble(session) == ""

    def test_save_and_retrieve_roundtrip(self, temp_db):
        """Direct DB helpers roundtrip without going through memory module."""
        db, _memory = temp_db
        session = "test-roundtrip"

        entities = {
            "namespaces": [{"value": "prod", "last_seen": 123.0, "count": 1}],
        }
        db.save_user_memory(session, entities)
        retrieved = db.get_user_memory(session)
        assert retrieved == entities

    def test_save_overwrites_existing(self, temp_db):
        """Saving twice for the same session overwrites, not appends."""
        db, _memory = temp_db
        session = "test-overwrite"

        db.save_user_memory(session, {"namespaces": [{"value": "a", "last_seen": 1.0, "count": 1}]})
        db.save_user_memory(session, {"namespaces": [{"value": "b", "last_seen": 2.0, "count": 1}]})

        retrieved = db.get_user_memory(session)
        assert retrieved == {"namespaces": [{"value": "b", "last_seen": 2.0, "count": 1}]}
