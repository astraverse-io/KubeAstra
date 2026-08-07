"""Merged configuration for KubeAstra MCP Server.

Combines settings from both:
- mcp-k8s-investigation-agent (kubectl, cluster access, recovery ops)
- k8s-ansible-mcp (Gemini AI, Weaviate vector DB, embeddings)
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CONFIG_DIR.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Kubernetes / kubectl settings ─────────────────────────────────────────
    kubeconfig_path: Optional[str] = None
    allowed_namespaces: str = "default"
    kubectl_timeout_seconds: int = 15
    max_log_tail_lines: int = 200
    max_output_bytes: int = 102400  # 100 KB — enough for logs/describe; run_json uses 10 MB
    enable_k8sgpt: bool = False
    enable_audit_log: bool = True
    # The backend chart always mounts /app/data as a writable volume for UID
    # 1000. Local development can override this with AUDIT_LOG_PATH.
    audit_log_path: str = "/app/data/audit.log"
    audit_log_max_bytes: int = 10 * 1024 * 1024
    audit_log_warning_interval_seconds: int = 60

    # Recovery operations (disabled by default for safety)
    enable_recovery_operations: bool = False
    max_scale_replicas: int = 100
    max_grace_period_seconds: int = 300

    # Destructive-op dry-run + confirmation token (Feature B).
    # When True, destructive tools require a two-step ritual:
    #   1. Call with dry_run=True → returns preview + short-lived token
    #   2. Call with confirm=True + the token → executes
    # When False, falls back to legacy confirm=True-only behavior (back-compat).
    require_destructive_confirmation: bool = True
    confirmation_token_ttl_seconds: int = 60

    # ── Deployment repository settings ────────────────────────────────────────
    # The internal Ansible deployment repo, indexed into the
    # ``deployment_repo`` Qdrant collection so the agent can ground its
    # answers in the actual playbooks/roles/inventory when a user pastes
    # an Ansible-flavored error. The reindex CronJob clones this on a
    # schedule; runtime chat never touches GitHub. See plan §10.
    deployment_repo_url: str = "https://github.com/kubeastra/deployment-provisioning.git"
    deployment_repo_branch: str = "main"
    # Walk only this directory inside the repo. Set to "" to walk the
    # whole repo; defaults to "ansible" so we skip Jenkinsfiles, Docker,
    # pipelines/, scripts/, powershell/ in v1.
    deployment_repo_subdir: str = "ansible"
    github_token: Optional[str] = None

    # ── LLM provider selection ────────────────────────────────────────────────
    # "gemini" (default), "ollama", "anthropic", or "openai". Add more
    # providers under services/llm.
    llm_provider: str = "gemini"

    # ── AI / Gemini settings ──────────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_timeout_seconds: int = 60
    # Kept for backwards-compatible /api/models responses; the chat UI no
    # longer exposes a model selector.
    gemini_available_models: str = "gemini-3.1-flash-lite"

    # ── Ollama settings (local / self-hosted LLM) ─────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    # Fallback list when /api/tags is unavailable.
    ollama_available_models: str = "llama3.1"
    ollama_auth_token: str = ""
    ollama_timeout_seconds: int = 120
    ollama_num_ctx: Optional[int] = None

    # ── Anthropic (Claude) settings ───────────────────────────────────────────
    # Required when LLM_PROVIDER=anthropic. Sampling params are not
    # forwarded — Claude Opus 4.7+ rejects them; prompting controls behavior.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    anthropic_timeout_seconds: int = 120

    # ── OpenAI settings ───────────────────────────────────────────────────────
    # Required when LLM_PROVIDER=openai. OPENAI_BASE_URL lets you point at
    # any OpenAI-compatible endpoint (Azure OpenAI, vLLM, LiteLLM, ...).
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: int = 120

    # ── Vector DB / RAG settings ──────────────────────────────────────────────
    # Qdrant replaces the prior Weaviate backend in Phase 1.1. The legacy
    # WEAVIATE_URL / WEAVIATE_COLLECTION env vars are intentionally NOT
    # honored — set QDRANT_URL / QDRANT_COLLECTION instead. Stale env vars
    # are silently ignored thanks to `extra="ignore"` in model_config.
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "k8s_errors"
    qdrant_timeout_seconds: float = 10.0

    # Where the vectors live.
    #   "server" — Qdrant over HTTP (Helm / docker-compose deployments).
    #   "local"  — qdrant-client embedded mode: an on-disk directory, no
    #              server process. Used by the desktop app.
    # Local mode takes an EXCLUSIVE lock on vector_db_path, so exactly one
    # process may hold it; the desktop launcher enforces single-instance for
    # this reason.
    vector_db_mode: str = "server"
    vector_db_path: str = ""  # required when vector_db_mode == "local"

    # Embedding model + its native vector dimension. The dimension MUST
    # match the model: bumping the model without bumping this number will
    # cause Qdrant collection creation to fail (or, worse, silent search
    # mismatches).
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # How embeddings are computed.
    #   "local"  — sentence-transformers in-process. Accurate and offline but
    #              drags in torch (~770MB with its transitive deps), so it is
    #              server-only; the desktop bundle excludes torch entirely.
    #   "api"    — the provider's embeddings HTTP API (desktop default).
    #   "ollama" — a local Ollama daemon; keeps airgapped desktops working.
    # NOTE: Anthropic has no embeddings API. When the chat provider is
    # Anthropic the user supplies a separate embeddings key (voyage / openai /
    # gemini); without one, memory degrades to keyword-only rather than
    # failing (see services/embeddings.py).
    embeddings_mode: str = "local"
    embeddings_provider: str = ""  # voyage | openai | gemini (mode == "api")
    embeddings_model: str = ""  # blank => provider default
    embeddings_timeout_seconds: float = 30.0
    ollama_url: str = "http://localhost:11434"

    # ── Alert ingestion ───────────────────────────────────────────────────────
    # `/api/v1/alerts/webhook` is exempt from interactive session auth, because
    # Alertmanager has no user session. That made it reachable by anyone who
    # could route to the backend, and a POST to it starts an LLM-backed
    # investigation against the cluster. The README documented this flag with a
    # default of `false` for as long as the feature existed; nothing read it, so
    # the endpoint was in fact always on. Now the documented default is the
    # actual one, and reaching the webhook is a deliberate act.
    alertmanager_webhook_enabled: bool = False

    # How close in time two alerts about the same workload have to be to count
    # as one incident. The window slides on last activity rather than on when
    # the incident opened, so a workload that keeps firing stays one incident
    # instead of fragmenting into a new one every window period.
    alert_correlation_window_minutes: int = 10

    # An incident normally closes when every investigation attached to it is
    # terminal. This is the backstop for when that never happens — an
    # Alertmanager configured with `send_resolved: false` never tells us the
    # condition ended, so without a lifetime cap the incident would keep
    # absorbing new alerts forever and they would stop being investigated.
    alert_incident_max_lifetime_hours: int = 24

    # ── RAG ingestion (Phase 1.2) ─────────────────────────────────────────────
    # Path to the YAML config consumed by scripts/reindex.py. Only the
    # CronJob honors this; the MCP/backend never reads it directly.
    rag_config: str = "/etc/rag/config.yaml"

    # ── RAG retrieval router (Phase 1.4) ──────────────────────────────────────
    # Master switch + thresholds for deciding cached / grounded / cold per
    # chat turn. Tuning these needs production data — start conservative
    # so we don't return wrong cached answers in early days.
    rag_router_enabled: bool = True
    rag_router_top_k: int = 5
    rag_router_cached_threshold: float = 0.92      # verified runbook only
    rag_router_grounded_threshold: float = 0.70    # any high-trust collection
    # Comma-separated list. Lower-trust collections (session_memory) are
    # intentionally not in the default — they go in once Phase 1.3 promotion
    # path is wired. ``deployment_repo`` is included by default (Phase 1.5);
    # the router additionally force-includes it when an Ansible-flavored
    # error is detected even if an operator removed it here.
    rag_router_collections: str = "runbook,devops_doc,deployment_repo"

    # ── Phase 1.3 — auto-capture from chats ──────────────────────────────────
    # Each chat that resolves a real problem is classified by a cheap LLM
    # call and (when worthy) written to the session_memory Qdrant collection.
    # Human thumbs-up later promotes the entry to the runbook collection.
    session_capture_enabled: bool = False              # opt-in until proven on staging
    session_capture_ttl_days: int = 90                 # unverified entries fade out after this
    # Soft cap on transcript size sent to the classifier (chars). Keeps
    # the classifier cheap and reduces the risk of leaked secrets in the
    # prompt.
    session_capture_transcript_chars: int = 4000
    # Redaction patterns are baked into services/rag/redaction.py; this
    # setting just lets ops disable redaction in a controlled environment
    # (almost always leave on).
    session_capture_redact_secrets: bool = True

    # ── Phase 2.3 — Semantic prompt cache (L2) ───────────────────────────────
    # Before invoking the retrieval router or the LLM, check whether a
    # similar question was already answered within the lookback window.
    # If yes (similarity above the strict threshold), return that prior
    # answer instantly — zero LLM call, zero tool calls. Reuses the
    # session_memory collection populated by Phase 1.3 capture, so this
    # only meaningfully kicks in after capture has been running for a
    # while AND `session_capture_enabled` is also true.
    prompt_cache_enabled: bool = False
    prompt_cache_threshold: float = 0.95         # stricter than router (0.70) — we skip the LLM
    prompt_cache_lookback_hours: int = 24        # only consider very recent captures
    prompt_cache_top_k: int = 5                  # how many candidates to inspect before giving up

    # ── Phase 3.0 — Proactive cluster triage ─────────────────────────────────
    # When enabled, the chat stream emits a "triage_greet" event on the
    # FIRST message of a session — a one-screen summary of CrashLooping
    # pods, pending pods, and recent Warning events on the user's current
    # cluster context. Reuses existing kubectl wrappers; no new pods or
    # background workers (see [Phase 3.1 cluster watcher] for that).
    # Off by default so existing deployments don't suddenly start
    # emitting summaries.
    enable_proactive_triage: bool = False
    # Comma-separated namespace list, or "*" for all namespaces.
    proactive_triage_namespaces: str = "*"
    # Window (minutes) for "recent" Warning events in the greeting.
    proactive_triage_event_lookback_min: int = 10

    # ── Tool result summarization (Phase 2.1) ─────────────────────────────────
    # When tool outputs (logs, events, describe) exceed the threshold, run them
    # through a summarizer before they reach the main LLM context. Saves tokens
    # and improves answer quality on noisy clusters. Raw output is always
    # preserved for the UI; only LLM consumers should prefer the summary.
    enable_log_summarization: bool = False
    log_summarization_threshold_bytes: int = 2048
    log_summarization_use_llm: bool = True   # False = heuristic-only (free, deterministic)
    log_summarization_max_tokens: int = 400

    # ── Database (optional, inherited from devops-ai-assistant) ───────────────
    database_url: str = "postgresql://devops_ai:devops_ai_password@localhost:5432/devops_ai_db"

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def allowed_namespaces_list(self) -> List[str]:
        return [ns.strip() for ns in self.allowed_namespaces.split(",") if ns.strip()]

    @property
    def kubeconfig_path_resolved(self) -> Optional[Path]:
        if not self.kubeconfig_path:
            return None
        return Path(self.kubeconfig_path).expanduser().resolve()

    @property
    def ai_enabled(self) -> bool:
        provider = (self.llm_provider or "").lower()
        if provider == "ollama":
            return bool(self.ollama_base_url and self.ollama_model)
        if provider == "anthropic":
            return bool(self.anthropic_api_key and self.anthropic_model)
        if provider == "openai":
            return bool(self.openai_api_key and self.openai_model)
        return bool(self.gemini_api_key)

    def validate_settings(self) -> None:
        if not self.allowed_namespaces_list:
            raise ValueError("ALLOWED_NAMESPACES must contain at least one namespace")
        if self.kubectl_timeout_seconds <= 0:
            raise ValueError("KUBECTL_TIMEOUT_SECONDS must be positive")
        if self.ollama_timeout_seconds <= 0:
            raise ValueError("OLLAMA_TIMEOUT_SECONDS must be positive")
        if self.gemini_timeout_seconds <= 0:
            raise ValueError("GEMINI_TIMEOUT_SECONDS must be positive")
        if self.anthropic_timeout_seconds <= 0:
            raise ValueError("ANTHROPIC_TIMEOUT_SECONDS must be positive")
        if self.openai_timeout_seconds <= 0:
            raise ValueError("OPENAI_TIMEOUT_SECONDS must be positive")
        if self.max_log_tail_lines <= 0 or self.max_log_tail_lines > 1000:
            raise ValueError("MAX_LOG_TAIL_LINES must be between 1 and 1000")
        if self.max_output_bytes <= 0:
            raise ValueError("MAX_OUTPUT_BYTES must be positive")
        if self.audit_log_max_bytes <= 0:
            raise ValueError("AUDIT_LOG_MAX_BYTES must be positive")
        if self.audit_log_warning_interval_seconds <= 0:
            raise ValueError("AUDIT_LOG_WARNING_INTERVAL_SECONDS must be positive")


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
