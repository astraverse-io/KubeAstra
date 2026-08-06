"""Untrusted values must not be able to forge log records.

A silence reason and an alert name both arrive from outside and both get
logged. A newline in either lets the writer append a line that reads like the
application produced it — an "auth: admin login" entry that was really the tail
of somebody's alert description.

CodeQL flagged three of these on the silences router. The fix is here rather
than at each call site so there is one place to reason about, and one place to
test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import log_safety  # noqa: E402


@pytest.mark.parametrize("raw", ["a\nb", "a\rb", "a\r\nb", "a\tb", "a\x00b", "a\x1bb"])
def test_nothing_that_could_start_a_new_record_survives(raw: str):
    cleaned = log_safety.one_line(raw)

    assert "\n" not in cleaned
    assert "\r" not in cleaned
    assert cleaned.isprintable()


def test_a_forged_record_is_flattened_into_one_line():
    reason = "routine\n2026-08-06 12:00:01 WARNING auth: admin login from 10.0.0.1"

    cleaned = log_safety.one_line(reason)

    assert "\n" not in cleaned
    assert cleaned.count("admin login") == 1  # still visible, just not its own line


def test_control_characters_become_spaces_not_nothing():
    """Stripping would turn `a\\nb` into the single token `ab`, which reads as a
    value nobody typed. A space keeps the seam visible."""
    assert log_safety.one_line("a\nb") == "a b"


def test_ordinary_text_is_untouched():
    assert log_safety.one_line("rolling back image v2.1") == "rolling back image v2.1"


def test_non_ascii_text_is_kept():
    """A reason written in another language is not an attack, and mangling it
    would make the log useless to whoever wrote it."""
    assert log_safety.one_line("återställning på gång") == "återställning på gång"


def test_a_long_value_is_truncated():
    """An unbounded field can push real records out of a rotated log."""
    cleaned = log_safety.one_line("x" * 5000)

    assert len(cleaned) < 5000
    assert cleaned.endswith(log_safety.TRUNCATION_MARKER)


def test_truncation_is_marked_so_it_is_not_read_as_the_whole_value():
    assert log_safety.one_line("y" * 300, limit=10) == "y" * 10 + log_safety.TRUNCATION_MARKER


def test_a_non_string_is_accepted():
    """Call sites pass ids and counts through the same helper rather than
    remembering which fields are strings."""
    assert log_safety.one_line(42) == "42"
    assert log_safety.one_line(None) == "None"


# ── the call sites that CodeQL flagged ────────────────────────────────────


def test_the_silences_router_sanitises_what_it_logs():
    """Guards the specific finding: a reason goes straight into a log line."""
    source = (BACKEND_DIR / "routers" / "alert_silences.py").read_text()

    assert "log_safety.one_line(body.reason)" in source
    assert "log_safety.one_line(silence_id)" in source


def test_the_webhook_sanitises_alert_names():
    """Alert names come from an untrusted webhook payload and are logged on
    every dedup, resolve and silence path."""
    source = (BACKEND_DIR / "routers" / "alerts.py").read_text()

    assert "alert.name," not in source, (
        "an alert name is being logged unsanitised — wrap it in "
        "log_safety.one_line()"
    )
