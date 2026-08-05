"""Workflow files GitHub can actually load.

A duplicate top-level key makes a workflow unloadable. GitHub reports that as
a run that fails in 0 seconds with no jobs — which looks like infrastructure
flakiness, not a syntax error, and is easy to scroll past on a branch whose
checks you are not watching.

That is what happened to `ci.yml`. `main` and `feat/desktop` each added a
`permissions:` block, at different line offsets, so git merged both without a
conflict. `feat/desktop` then ran no CI for a day: pushes and pull requests
produced runs that failed instantly, while the CodeQL workflow — a separate,
still-valid file — kept reporting green and made the branch look checked.

PyYAML's default loader accepts duplicate keys silently (last one wins), so
these tests use a loader that refuses them, which is the behaviour GitHub has.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that treats a repeated mapping key as the error it is."""


def _no_duplicate_keys(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def test_there_are_workflows_to_check():
    """A glob that matches nothing would make every test below vacuous."""
    assert WORKFLOWS, f"no workflow files found under {WORKFLOW_DIR}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_has_no_duplicate_keys(path):
    try:
        yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        pytest.fail(f"{path.name} is not loadable by GitHub Actions:\n{exc}")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_declares_the_keys_a_workflow_needs(path):
    """Catches a file that parses but could never run.

    `on:` is YAML 1.1's boolean `True` once parsed — the quirk that makes
    hand-checking these files unreliable, and the reason this is asserted
    rather than eyeballed.
    """
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)

    assert isinstance(document, dict), f"{path.name} is not a mapping"
    assert document.get("jobs"), f"{path.name} declares no jobs"
    assert (
        "on" in document or True in document
    ), f"{path.name} declares no triggers"
