"""Locate the exact character span of a scalar field in a YAML document.

yaml.compose_all() yields a node tree whose ScalarNodes carry start/end marks
with character indices. We walk that tree along a field path and return the
terminal scalar's span, so a caller can replace exactly those characters in the
raw text without re-serialising (which would drop comments and reorder keys).

Container and env lists are addressed by their `name` child, never by list
index — a reordered manifest must still resolve.
"""
from __future__ import annotations

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


def find_span(raw_text: str, doc_index: int, field_path: tuple) -> Span | None:
    docs = list(yaml.compose_all(raw_text))
    if doc_index < 0 or doc_index >= len(docs):
        return None
    node = docs[doc_index]
    for key in field_path:
        if isinstance(node, yaml.MappingNode):
            node = _child_by_key(node, key)
        elif isinstance(node, yaml.SequenceNode):
            node = _named_seq_item(node, key)
        else:
            return None
        if node is None:
            return None
    if not isinstance(node, yaml.ScalarNode):
        return None
    return Span(
        start=node.start_mark.index,
        end=node.end_mark.index,
        old_value=node.value,
    )
