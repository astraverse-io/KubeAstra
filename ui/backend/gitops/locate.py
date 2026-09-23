"""Locate the exact character span of a scalar field in a YAML document.

yaml.compose_all() yields a node tree whose ScalarNodes carry start/end marks
with character indices. We walk that tree along a field path and return the
terminal scalar's span, so a caller can replace exactly those characters in the
raw text without re-serialising (which would drop comments and reorder keys).

Container and env lists are addressed by their `name` child, never by list
index — a reordered manifest must still resolve.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class FieldChange:
    kind: str
    name: str
    namespace: str | None
    field_path: tuple  # str keys; a str element indexes a named list item
    new_value: str | int
    reason: str


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    old_value: str


def _child_by_key(node: yaml.MappingNode, key: str):
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _named_seq_item(node: yaml.SequenceNode, name: str):
    """A list element whose `name:` child equals `name`. Returns the
    MappingNode item or None."""
    for item in node.value:
        if not isinstance(item, yaml.MappingNode):
            continue
        name_node = _child_by_key(item, "name")
        if isinstance(name_node, yaml.ScalarNode) and name_node.value == name:
            return item
    return None


def _shared_nodes(root: yaml.Node) -> set[int]:
    """ids of nodes referenced more than once in `root`'s tree.

    yaml.compose_all() collapses a YAML alias (`*a`) into the *same* node object
    as its anchor (`&a`), so an anchored-and-aliased node — or an aliased
    mapping pulled in by a merge key (`<<: *a`) — is reachable from >1 parent.
    A single character span cannot faithfully change a value shared across
    locations (editing `&a 3` also changes every `*a`), so the locator must
    refuse on any such node rather than silently edit one site.
    """
    counts: Counter[int] = Counter()
    seen: set[int] = set()

    def walk(node: yaml.Node | None) -> None:
        if node is None:
            return
        counts[id(node)] += 1
        if id(node) in seen:            # count the reference, don't re-expand
            return
        seen.add(id(node))              # guards against cyclic anchors
        if isinstance(node, yaml.MappingNode):
            for _k, v in node.value:
                walk(v)
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item)

    walk(root)
    return {nid for nid, c in counts.items() if c >= 2}


def _is_anchored(raw_text: str, node: yaml.Node) -> bool:
    """True if the node's span begins with a YAML anchor property (`&name`).

    yaml.compose_all() does not keep the anchor name on the node, but the
    ScalarNode's start mark points at the `&`, so the span would *include* the
    anchor property — editing it would silently drop `&name`. An alias composes
    to its anchor node, so this also fires on alias targets; together with
    shared-node detection it makes anchored/aliased scalars non-span-editable.
    """
    i = node.start_mark.index
    return 0 <= i < len(raw_text) and raw_text[i] == "&"


def find_span(raw_text: str, doc_index: int, field_path: tuple) -> Span | None:
    docs = list(yaml.compose_all(raw_text))
    if doc_index < 0 or doc_index >= len(docs):
        return None
    root = docs[doc_index]
    shared = _shared_nodes(root)
    node: yaml.Node | None = root
    visited: list[yaml.Node] = [root]
    for key in field_path:
        if isinstance(node, yaml.MappingNode):
            node = _child_by_key(node, key)
        elif isinstance(node, yaml.SequenceNode):
            node = _named_seq_item(node, key)
        else:
            return None
        if node is None:
            return None
        visited.append(node)
    if not isinstance(node, yaml.ScalarNode):
        return None
    # Fail-closed on anchors/aliases: refuse if the target scalar carries an
    # anchor / is an alias to one, or if any node on the path to it is shared
    # (the edit would drop an anchor or change the value at every alias site).
    if _is_anchored(raw_text, node) or any(id(n) in shared for n in visited):
        return None
    return Span(
        start=node.start_mark.index,
        end=node.end_mark.index,
        old_value=node.value,
    )
