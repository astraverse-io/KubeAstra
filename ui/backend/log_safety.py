"""Make an untrusted value safe to put in a log line.

Log files are parsed — by humans scrolling them, and by whatever ships them to
a log store. A value containing a newline can forge a whole record, so an
attacker who controls a silence reason or an alert name can write log entries
that look like they came from the application:

    silence abc created by attacker for 60s: routine
    2026-08-06 12:00:01 WARNING  auth: admin login from 10.0.0.1

Only the first of those lines is real. The second was the tail of the reason.

This does not make the value trustworthy — it makes it occupy exactly one line,
which is all a log record can safely promise.
"""

from __future__ import annotations

# Long enough for a genuine reason or alert name, short enough that a
# multi-kilobyte field cannot push real records out of a rotated log.
DEFAULT_LIMIT = 200

TRUNCATION_MARKER = "…[truncated]"


def one_line(value: object, limit: int = DEFAULT_LIMIT) -> str:
    """Collapse `value` to a single printable line, bounded in length.

    Newlines, carriage returns, tabs and other control characters become
    spaces rather than being stripped, so that `a\\nb` reads as `a b` instead
    of silently becoming the single token `ab`.
    """
    text = str(value)
    # The two that actually forge a record are handled explicitly first. The
    # comprehension below would catch them too, but a static analyser reading
    # this for a log-injection sanitizer is looking for exactly this, and an
    # unrecognised sanitizer is an alert that never clears.
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    # Everything else non-printable — tabs, NUL, escape, the C1 range — is a
    # display problem rather than a forgery, but has no business in a log line
    # either. `isprintable()` is True for ordinary text including non-ASCII, so
    # this does not mangle a reason written in another language.
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    if len(cleaned) > limit:
        return cleaned[:limit] + TRUNCATION_MARKER
    return cleaned
