"""The release workflow must pick exactly one signing/notarization path.

A missing GitHub secret is an EMPTY STRING, not an unset variable. Tauri reads
its credentials with the Rust equivalent of `env::var`, which returns `Ok("")`
for a variable that was set to nothing — so naming a secret in `env:` is enough
to make Tauri believe it has that credential and take the wrong branch.

release-desktop.yml already carries a long comment about this: passing
APPLE_CERTIFICATE when no such secret exists produced

    failed codesign application: failed to run command security import:
    failed to import keychain certificate

There is no way to conditionally omit a single `env` key, so the only fix is
one step per credential combination, each guarded by an `if`. That makes the
step conditions load-bearing, and nothing else checks them — a tagged release
is the first thing that would, and by then the tag exists.

Notarization has the same shape as signing. Apple takes either an app-specific
password (APPLE_ID + APPLE_PASSWORD) or an App Store Connect API key
(APPLE_API_ISSUER + APPLE_API_KEY + APPLE_API_KEY_PATH). Tauri prefers the API
key when it sees one, so a blank APPLE_API_KEY alongside a working password
silently breaks a release that would otherwise have worked.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-desktop.yml"

# The two credential families. Naming one variable from either family commits
# Tauri to that family, so a step must draw from at most one of them.
PASSWORD_VARS = {"APPLE_ID", "APPLE_PASSWORD"}
API_KEY_VARS = {"APPLE_API_ISSUER", "APPLE_API_KEY", "APPLE_API_KEY_PATH"}


def _build_steps() -> list[dict]:
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    return [s for s in job["steps"] if str(s.get("name", "")).startswith("Build Tauri App")]


def _job_env() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]["env"]


def test_the_workflow_still_parses():
    """A guard on the guard — every assertion below reads this file."""
    assert WORKFLOW.exists()
    assert _build_steps(), "parsed no build steps; the naming convention changed"


def test_there_is_a_step_for_every_credential_combination():
    """unsigned, signed+password, signed+api-key. Dropping one does not fail a
    build — it silently produces an unsigned release, or none at all."""
    assert len(_build_steps()) == 3


@pytest.mark.parametrize("step", _build_steps(), ids=lambda s: s["name"])
def test_every_build_step_is_guarded(step: dict):
    """An unguarded step runs alongside the one that was supposed to replace
    it, and the release ends up with two builds racing for the same tag."""
    assert step.get("if"), f"{step['name']} has no `if`"


@pytest.mark.parametrize("step", _build_steps(), ids=lambda s: s["name"])
def test_no_step_mixes_the_two_notarization_credential_families(step: dict):
    """The empty-string trap. A step that names both hands Tauri the API-key
    branch whether or not the API key exists."""
    env = set(step.get("env", {}))
    password = env & PASSWORD_VARS
    api_key = env & API_KEY_VARS

    assert not (password and api_key), (
        f"{step['name']} names both {sorted(password)} and {sorted(api_key)}. "
        f"Tauri prefers the API key, so whichever is unset becomes an empty "
        f"string and notarization fails with no useful message. Split this "
        f"into two steps guarded on HAS_APPLE_API_KEY."
    )


@pytest.mark.parametrize(
    "flag", ["HAS_APPLE_CERT", "HAS_UPDATER_KEY", "HAS_APPLE_API_KEY"]
)
def test_secret_presence_is_computed_at_job_level(flag: str):
    """`secrets` is not available in a step-level `if`. Computing these in the
    job `env` is the only place the check works — moved into a step, the
    expression evaluates to an empty string and the step silently never runs.
    """
    assert flag in _job_env()
    assert "secrets." in str(_job_env()[flag])


def test_the_signed_steps_disagree_on_exactly_the_api_key_flag():
    """The two signed paths must be mutually exclusive. If both conditions can
    be true at once, a tagged build runs two full Tauri builds and uploads two
    sets of artifacts to the same draft release."""
    signed = [s for s in _build_steps() if "HAS_APPLE_CERT == 'true'" in s["if"]]

    assert len(signed) == 2
    conditions = {s["if"] for s in signed}
    assert any("HAS_APPLE_API_KEY == 'true'" in c for c in conditions)
    assert any("HAS_APPLE_API_KEY != 'true'" in c for c in conditions)


def test_no_two_build_steps_share_a_condition():
    """Distinct `if` strings are not proof of exclusivity, but two identical
    ones are proof of the opposite."""
    conditions = [s["if"] for s in _build_steps()]

    for a, b in itertools.combinations(conditions, 2):
        assert a != b, f"two build steps share the condition {a!r}"


def test_the_api_key_file_is_written_before_the_build_that_reads_it():
    """APPLE_API_KEY_PATH points at a file. Ordering these wrong gives a
    notarization failure that reads like a credentials problem."""
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["build-desktop"]
    names = [str(s.get("name", "")) for s in job["steps"]]

    materialize = names.index("Materialize App Store Connect API key")
    build = names.index("Build Tauri App (signed, notarized with an API key)")

    assert materialize < build


def test_the_p8_secret_is_never_named_by_a_build_step():
    """The key material is decoded to a file by one step. A build step that
    also named it would put the raw key into the environment of a third-party
    action for no reason."""
    for step in _build_steps():
        assert "APPLE_API_KEY_P8" not in set(step.get("env", {})), (
            f"{step['name']} exposes the raw .p8 to tauri-action"
        )
