"""Collection definitions for the RAG knowledge base.

Each collection is a logical bucket in Qdrant. Phase 1.2 only auto-creates
``DevOpsDoc`` (ingested team documentation); the other collections are
declared here so the design is visible and so 1.3 / 4.1 can pick them up
without touching this file again.

Naming: lowercase + underscores (Qdrant convention). The ``k8s_errors``
collection is the historical one previously seeded by ``data/seed.py``;
it's still here for backward compat with ``ai_tools/analyze.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CollectionSpec:
    name: str
    # Keyword-indexed payload fields. Indexes accelerate equality filters at
    # search time (e.g. ``filter(source="runbooks-repo", verified=True)``).
    indexed_fields: tuple[str, ...] = ()
    # Documentation only — not enforced at the storage layer (Qdrant is
    # schemaless for payloads). Helps reviewers see the expected shape.
    payload_fields: tuple[str, ...] = ()
    description: str = ""


# ── Common metadata fields ───────────────────────────────────────────────────
# Every collection includes these in its payload for filtering/citation.
_COMMON_FIELDS = (
    "source",            # which source produced it ("local_path:/knowledge", "git_repo:url@branch")
    "title",             # human-readable name for citations
    "url",               # canonical link (file:// or https://)
    "cluster",           # cluster scope, "*" for any
    "namespace",         # namespace scope, "*" for any
    "kind",              # k8s resource kind, "*" for any
    "error_signature",   # opaque signature of the error/issue, when known
    "timestamp",         # ISO 8601 of last write
    "user",              # who created/promoted the entry, "system" for ingestion
    "verified",          # bool — did a human confirm this is correct?
    "upvotes",           # int — promotion signal
)

_COMMON_INDEXED = ("source", "cluster", "namespace", "kind", "verified")


# ── DevOpsDoc ────────────────────────────────────────────────────────────────
# Ingested team documentation: markdown runbooks, postmortems, how-tos.
DEVOPS_DOC = CollectionSpec(
    name="devops_doc",
    indexed_fields=_COMMON_INDEXED,
    payload_fields=_COMMON_FIELDS + (
        "content",       # the chunk text itself
        "section",       # which markdown header this chunk lives under
        "chunk_index",   # position within the source doc
        "chunk_hash",    # SHA256 of content — used for idempotent re-ingest
    ),
    description="Ingested team docs: markdown runbooks, postmortems, how-tos.",
)

# ── Runbook ──────────────────────────────────────────────────────────────────
# Promoted-from-chat or hand-curated problem→fix recipes. Higher trust than
# DevOpsDoc because each entry has been explicitly marked verified.
RUNBOOK = CollectionSpec(
    name="runbook",
    indexed_fields=_COMMON_INDEXED,
    payload_fields=_COMMON_FIELDS + (
        "problem",
        "resolution",
        "commands",      # JSON-encoded list of kubectl commands
    ),
    description="Verified problem→fix recipes (promoted chats + manual entries).",
)

# ── SessionMemory ────────────────────────────────────────────────────────────
# Auto-captured chat resolutions awaiting human thumbs-up. Lower trust;
# 90-day TTL applied at render time so stale guesses fade out.
SESSION_MEMORY = CollectionSpec(
    name="session_memory",
    indexed_fields=_COMMON_INDEXED,
    payload_fields=_COMMON_FIELDS + (
        "question",
        "context",
        "resolution",
        "commands_run",
        "expires_at",
    ),
    description="Auto-captured chat resolutions awaiting human verification.",
)

# ── DeploymentRepo ───────────────────────────────────────────────────────────
# Indexed content of the internal Ansible deployment repo: playbooks,
# roles, inventory, templates, custom modules, plus per-role aggregate
# summaries. Trust level matches DEVOPS_DOC (curated source-of-truth)
# but kept separate so the router can scope retrieval to it on Ansible-
# flavored questions and the schema can carry Ansible-specific fields.
DEPLOYMENT_REPO = CollectionSpec(
    name="deployment_repo",
    indexed_fields=_COMMON_INDEXED + (
        "category", "role", "environment", "play_group", "module",
        "file_type", "kind",
    ),
    payload_fields=_COMMON_FIELDS + (
        "content",            # the chunk text
        "section",            # breadcrumb path
        "chunk_index",
        "chunk_hash",
        # Provenance (set by GitRepoSource for git-cloned content)
        "repo_url",
        "branch",
        "path",               # path within the repo
        # File classification (set by LocalPathSource)
        "file_type",          # markdown | yaml | jinja | ansible_module | role_aggregate
        # Path-derived (LocalPathSource)
        "category",           # role category, e.g. kubernetes / linux / awx
        "role",               # role name
        "role_subdir",        # tasks / defaults / handlers / templates / vars / meta
        "play_group",         # playbooks/<group>/...
        "environment",        # inventory/<env>/...
        "inventory_kind",     # group_vars / host_vars / (file)
        # Chunker-extracted (chunking_ansible.py)
        "task_name",
        "module",             # e.g. ansible.builtin.shell, kubernetes.core.k8s_info
        "has_rescue",
        "is_block",
        "play_name",
        "hosts",
        "roles_list",
        "template_path",
        "module_name",        # custom module name (library/<name>.py)
        "kind",               # task | handler | play | template | custom_module | role_aggregate | defaults | vars | meta
        "doc_extracted",      # bool — was Ansible DOCUMENTATION block found?
        "parse_error",        # set when YAML parse failed; chunk fell back to fixed-window
    ),
    description="Indexed Ansible deployment repo (playbooks, roles, inventory, templates, custom modules, role aggregates).",
)

# ── K8sError (legacy, kept for backward compat with seed.py + analyze.py) ────
K8S_ERROR = CollectionSpec(
    name="k8s_errors",
    indexed_fields=("tool", "category", "severity"),
    payload_fields=(
        "error_text", "tool", "category", "solution_text",
        "commands", "success_rate", "severity",
    ),
    description="Seeded error→solution pairs (pre-RAG).",
)

# ── IncidentMemory ───────────────────────────────────────────────────────────
INCIDENT_MEMORY = CollectionSpec(
    name="incident_memory",
    indexed_fields=_COMMON_INDEXED + ("alert_name", "category", "severity"),
    payload_fields=_COMMON_FIELDS + (
        "investigation_id",
        "alert_name",
        "category",
        "severity",
        "workload",
        "rca_summary",
        "root_causes",
        "recommendations",
        "evidence_summaries",
        "tags",
    ),
    description="Persistent semantic memory of past alert investigations and root cause analyses.",
)


# Convenience export. Order matters for "search all KB collections" use cases:
# higher-trust collections first so they bubble to the top on tied scores.
ALL_COLLECTIONS: tuple[CollectionSpec, ...] = (
    RUNBOOK,
    DEVOPS_DOC,
    DEPLOYMENT_REPO,
    K8S_ERROR,
    SESSION_MEMORY,
    INCIDENT_MEMORY,
)



def get_collection(name: str) -> CollectionSpec | None:
    """Look up a CollectionSpec by name. Returns None if unknown."""
    for spec in ALL_COLLECTIONS:
        if spec.name == name:
            return spec
    return None
