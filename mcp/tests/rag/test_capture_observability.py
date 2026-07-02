"""Observability tests for session capture decisions."""

from pathlib import Path
import logging
import sys

MCP_DIR = Path(__file__).resolve().parents[2]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from services.rag import capture  # noqa: E402


class DisabledSettings:
    session_capture_enabled = False


class EnabledSettings:
    session_capture_enabled = True
    session_capture_transcript_chars = 4000
    session_capture_redact_secrets = True
    session_capture_ttl_days = 90


def test_capture_logs_disabled_skip_reason(monkeypatch, caplog):
    monkeypatch.setattr(capture, "get_settings", lambda: DisabledSettings())

    with caplog.at_level(logging.DEBUG, logger="services.rag.capture"):
        capture_id = capture.maybe_capture(
            question="why is kafka crashing?",
            answer="Kafka cannot reach ZooKeeper because the service is missing.",
            tool_used="investigate_pod",
            react_steps=[],
            session_id="session-a",
        )

    assert capture_id is None
    assert "session_capture_disabled" in caplog.text
    assert "session-a" in caplog.text


def test_capture_logs_short_answer_skip_reason(monkeypatch, caplog):
    monkeypatch.setattr(capture, "get_settings", lambda: EnabledSettings())

    with caplog.at_level(logging.INFO, logger="services.rag.capture"):
        capture_id = capture.maybe_capture(
            question="why is kafka crashing?",
            answer="too short",
            tool_used="investigate_pod",
            react_steps=[],
            session_id="session-b",
        )

    assert capture_id is None
    assert "answer_too_short_or_backend_unreachable" in caplog.text
    assert "session-b" in caplog.text
