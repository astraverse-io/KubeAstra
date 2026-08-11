"""The cask update must hang off release *publication*, not the build.

`release-desktop.yml` creates a DRAFT release, and a draft's assets are not
served from

    https://github.com/.../releases/download/desktop-vX/...

which is exactly where the tap's `update-cask.yml` fetches the DMG to
checksum it. A dispatch fired from the tag build would therefore fail every
time, with "arm64 DMG is required" and nothing pointing at timing as the
cause. Publishing the draft is the moment the file becomes fetchable.

That is easy to "simplify" later by moving the trigger next to the build,
where it looks like it belongs. These tests are why that fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update-homebrew-cask.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-desktop.yml"


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers(doc: dict) -> dict:
    # PyYAML parses a bare `on:` key as the boolean True.
    return doc[True] if True in doc else doc["on"]


def _steps() -> list[dict]:
    return _wf()["jobs"]["dispatch"]["steps"]


def test_the_workflow_exists_and_parses():
    assert WORKFLOW.exists()
    assert _steps()


def test_it_triggers_on_release_publication():
    triggers = _triggers(_wf())

    assert "release" in triggers
    assert triggers["release"]["types"] == ["published"]


def test_it_does_not_trigger_on_the_tag_that_builds_the_release():
    """The tag build produces a draft. Triggering there means the tap fetches
    a URL that 404s, every time."""
    triggers = _triggers(_wf())

    assert "push" not in triggers, (
        "the cask dispatch now fires on a push/tag. The release it would "
        "point at is still a draft at that moment, so the tap cannot download "
        "the DMG and the update fails."
    )


def test_the_release_build_still_produces_a_draft():
    """The premise of the test above. If releases stop being drafts, the
    timing constraint disappears and this whole design can be revisited."""
    doc = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    steps = doc["jobs"]["build-desktop"]["steps"]
    drafts = [s["with"]["releaseDraft"] for s in steps if s.get("with", {}).get("releaseDraft") is not None]

    assert drafts and all(drafts), "releases are no longer drafts; revisit the cask dispatch trigger"


def test_the_workflow_declares_permissions():
    """Without a `permissions:` block a workflow inherits the repository
    default, which on many repos is read/write across the board. CodeQL
    flagged this one (alert: "Workflow does not contain permissions").

    Nothing here touches this repository — every API call goes to the tap
    through TAP_DISPATCH_TOKEN — so GITHUB_TOKEN needs nothing.
    """
    doc = _wf()

    assert "permissions" in doc, "no permissions block; the job inherits repo defaults"
    assert doc["permissions"] in ({}, None), (
        f"this workflow grants GITHUB_TOKEN {doc['permissions']} but never uses it"
    )


def test_a_missing_token_warns_instead_of_failing():
    """The cask can always be updated by hand. An optional secret that is not
    set should not make a successful release look broken."""
    note = next(s for s in _steps() if s["name"] == "Note the missing token")

    assert "HAS_TAP_TOKEN != 'true'" in note["if"]
    assert "::warning::" in note["run"]
    assert "gh workflow run update-cask.yml" in note["run"], (
        "the warning should tell the reader how to do it by hand"
    )


def test_the_token_is_only_used_to_trigger_never_to_push():
    """The token is scoped to Actions: write on the tap. Switching to
    repository_dispatch would need Contents: write, which would let it push
    code to the tap — a much larger grant for no benefit, since the tap's own
    workflow commits with its built-in GITHUB_TOKEN."""
    runs = " ".join(s.get("run", "") for s in _steps())
    # Comments explain *why* repository_dispatch is not used, so match on what
    # the steps actually execute rather than on the file's text.
    commands = "\n".join(
        line for line in runs.splitlines() if not line.strip().startswith("#")
    )

    assert "gh workflow run" in commands
    assert "repository_dispatch" not in commands, (
        "repository_dispatch needs Contents: write on the tap; use "
        "workflow_dispatch and Actions: write instead"
    )
    assert "gh api" not in commands or "dispatches" not in commands


def test_secret_presence_is_computed_where_secrets_are_readable():
    """`secrets` is not available in a step-level `if`. Computed in the job
    env, as release-desktop.yml does for the same reason."""
    job = _wf()["jobs"]["dispatch"]

    assert "secrets." in str(job["env"]["HAS_TAP_TOKEN"])


def test_a_non_desktop_release_is_ignored():
    """The repo publishes server releases (v0.2.0) from the same repository.
    Those must not retarget the desktop cask."""
    gate = _steps()[0]

    assert "desktop-v*" in gate["run"]


@pytest.mark.parametrize("name", ["Trigger the tap's cask update", "Confirm the cask actually updated"])
def test_every_token_using_step_is_gated_on_the_token(name: str):
    step = next(s for s in _steps() if s["name"] == name)

    assert "HAS_TAP_TOKEN == 'true'" in step["if"]


def test_the_dispatch_is_verified_not_assumed():
    """`gh workflow run` returns as soon as the request is accepted. Without a
    follow-up check, a failure inside the tap's job goes unnoticed until
    somebody installs a version the cask does not know about."""
    confirm = next(s for s in _steps() if s["name"] == "Confirm the cask actually updated")

    assert "gh run list" in confirm["run"]
    assert "completed:success" in confirm["run"]
