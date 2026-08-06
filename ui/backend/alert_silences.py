"""Matching an alert against a silence.

Deliberately mirrors Alertmanager's matcher semantics — label, operator, value,
with `=`, `!=`, `=~`, `!~` and AND across all matchers — because operators
already know that model and a second, subtly different one is a trap.

Kept separate from the router so the matching rules can be tested without HTTP,
and separate from db.py because it is pure logic over a dict.
"""

from __future__ import annotations

import re
from typing import Any

OPERATORS = ("=", "!=", "=~", "!~")

# A regex matcher is operator-supplied, but it is stored and then evaluated
# against every incoming alert — so a catastrophically backtracking pattern
# would be a self-inflicted denial of service on the ingest path. Length is a
# blunt but effective first bound; compilation is checked at create time.
MAX_PATTERN_LENGTH = 200


class InvalidMatcher(ValueError):
    """A matcher that cannot be stored — reported at create time, not ingest."""


def validate_matchers(matchers: Any) -> list[dict]:
    """Normalise and reject bad matchers before they reach the database.

    An empty matcher list is refused: under AND semantics it matches every
    alert, so a silence with no matchers silently turns the whole pipeline off.
    That is the single most damaging mistake available here, and it looks like
    a harmless empty form.
    """
    if not isinstance(matchers, list) or not matchers:
        raise InvalidMatcher(
            "at least one matcher is required — a silence with none would "
            "match every alert and stop all investigation"
        )

    normalised = []
    for raw in matchers:
        if not isinstance(raw, dict):
            raise InvalidMatcher("each matcher must be an object")

        label = str(raw.get("label", "")).strip()
        op = str(raw.get("op", "=")).strip()
        value = raw.get("value", "")
        value = "" if value is None else str(value)

        if not label:
            raise InvalidMatcher("each matcher needs a label")
        if op not in OPERATORS:
            raise InvalidMatcher(
                f"unknown operator {op!r}; use one of {', '.join(OPERATORS)}"
            )
        if op in ("=~", "!~"):
            if len(value) > MAX_PATTERN_LENGTH:
                raise InvalidMatcher(
                    f"regex is longer than {MAX_PATTERN_LENGTH} characters"
                )
            try:
                re.compile(value)
            except re.error as exc:
                # Caught here rather than at ingest: a silence that raises on
                # every incoming alert would take alert ingestion down with it.
                raise InvalidMatcher(f"invalid regex {value!r}: {exc}") from exc

        normalised.append({"label": label, "op": op, "value": value})

    return normalised


def _matches_one(matcher: dict, labels: dict[str, str]) -> bool:
    # A label the alert does not carry is the empty string, matching
    # Alertmanager. That makes `{severity != "critical"}` match an alert with no
    # severity at all, which is the behaviour operators expect from it.
    actual = str(labels.get(matcher["label"], ""))
    op = matcher["op"]
    value = matcher["value"]

    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value

    try:
        # Anchored, like Alertmanager: `pod =~ "api-.*"` should not match
        # `legacy-api-7`. Unanchored regexes are a common source of silences
        # that quietly cover far more than intended.
        matched = re.fullmatch(value, actual) is not None
    except re.error:
        # Validated at create time, so this is a row that predates validation
        # or was written directly. Refuse to match rather than crash ingest.
        return False

    return matched if op == "=~" else not matched


def matches(silence: dict, labels: dict[str, str]) -> bool:
    """True when every matcher in the silence matches the alert's labels."""
    matchers = silence.get("matchers")
    # None means the stored JSON did not parse. Under AND semantics an empty
    # list matches everything, so this must fail closed.
    if not matchers:
        return False
    return all(_matches_one(m, labels) for m in matchers)


def find_matching(silences: list[dict], labels: dict[str, str]) -> list[dict]:
    return [s for s in silences if matches(s, labels)]
