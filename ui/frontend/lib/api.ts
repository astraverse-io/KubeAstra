const BASE = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

function apiUrl(path: string) {
  return BASE ? `${BASE}${path}` : path;
}

// Every id interpolated into a path below goes through encodeURIComponent, and
// none of them are ours: a session id comes from localStorage or the /chat/:id
// URL, a run id from a previous response. Unencoded, an id is not a segment but
// a fragment of URL — `../admin` climbs to a different endpoint, `?` or `#`
// truncates the path and pushes the rest into the query string, and with BASE
// set that resolution happens against another origin. It also just fixes
// ordinary ids containing a slash, which today 404 for no visible reason.

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type JsonRequestInit = Omit<RequestInit, "body"> & {
  body?: unknown;
};

async function fetchJson(path: string, init: JsonRequestInit = {}) {
  const headers = new Headers(init.headers);
  let requestBody: BodyInit | undefined;

  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
    requestBody = JSON.stringify(init.body);
  }

  const res = await fetch(apiUrl(path), {
    ...init,
    headers,
    body: requestBody,
    credentials: "include",
  });

  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const error = await res.json();
      message = error.detail || error.error || message;
    } catch {
      // Fall back to HTTP status when the backend error isn't JSON.
    }
    throw new ApiError(message, res.status);
  }

  return res.json();
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface SSHCredentials {
  host: string;
  username: string;
  password: string;
  port?: number;
}

export interface SSHTarget {
  host: string;
  username: string;
  port: number;
}

export interface KubeContext {
  name: string;
  cluster: string;
  server: string;
  user: string;
  namespace: string;
}

export interface ClusterStatus {
  connected: boolean;
  mode?: "autodetect" | "kubeconfig-upload" | "ssh" | string;
  context_name?: string;
  cluster_name?: string;
  server_url?: string;
  namespace?: string;
}

export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  tool_used?: string;
  result?: Record<string, unknown>;
  error?: string;
  created_at: string;
}

export type SessionAccessMode = "owned" | "admin_readonly";

export interface HistoryDetail {
  session_id: string;
  access_mode: SessionAccessMode;
  readonly: boolean;
  owner_username?: string | null;
  owner_display_name?: string | null;
  title?: string | null;
  messages: HistoryMessage[];
}

export interface AuthUser {
  id: string;
  username: string;
  display_name?: string | null;
  email?: string | null;
  role: string;
}

export interface AuthStatus {
  auth_enabled: boolean;
  allow_signup: boolean;
  user: AuthUser | null;
}

export interface ChatSession {
  id: string;
  title: string;
  timestamp: number;
}

export interface ChatResponse {
  reply: string;
  tool_used: string;
  result: Record<string, unknown> | null;
  error?: string | null;
  timestamp?: number;
  suggested_actions?: Array<{
    type?: string;
    action_kind?: "read_only" | "write_command" | "apply_yaml" | "manual";
    risk?: "low" | "medium" | "high";
    requires_approval?: boolean;
    label: string;
    command?: string;
    follow_up_prompt?: string;
    confirm?: boolean;
    stdin?: string;
    evidence_reference?: Record<string, unknown>;
  }>;
  synthesis_breakdown?: Record<string, unknown> | null;
  eval_retrieval_context?: string[];
  session_id?: string | null;
  run_id?: string | null;
  cost_summary?: {
    total_cost_usd: number;
    total_tokens_in: number;
    total_tokens_out: number;
    total_cached_tokens_in: number;
    model: string;
  } | null;
}

export interface ExecuteResponse {
  success: boolean;
  output: string;
  error: string;
}

// ── Alerts ──────────────────────────────────────────────────────────────────────

export async function triggerManualInvestigation(target: string): Promise<{ investigation_id: string }> {
  return fetchJson("/api/v1/alerts/manual", {
    method: "POST",
    body: { target },
  });
}

// ── Chat ──────────────────────────────────────────────────────────────────────

export async function sendChat(
  message: string,
  history: ChatMessage[],
  ssh?: SSHCredentials | null,
  sessionId?: string | null,
  model?: string | null
): Promise<ChatResponse> {
  const body: Record<string, unknown> = { message, history };
  if (ssh) body.ssh = ssh;
  if (sessionId) body.session_id = sessionId;
  if (model) body.model = model;

  const res = await fetch(apiUrl("/api/chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Feedback (Phase 1.3 — promote / quarantine captured sessions) ───────────

export interface FeedbackResponse {
  ok: boolean;
  detail: Record<string, unknown>;
}

export async function sendFeedback(
  captureId: string,
  rating: "up" | "down",
  options?: {
    reason?: string;
    sessionId?: string | null;
    prompt?: string;
    response?: string;
    toolUsed?: string | null;
  },
): Promise<FeedbackResponse> {
  const body: Record<string, unknown> = {
    capture_id: captureId,
    rating,
  };
  if (options?.reason) body.reason = options.reason;
  if (options?.sessionId) body.session_id = options.sessionId;
  if (options?.prompt) body.prompt = options.prompt;
  if (options?.response) body.response = options.response;
  if (options?.toolUsed) body.tool_used = options.toolUsed;

  const res = await fetch(apiUrl("/api/feedback"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}


// ── Streaming chat (Phase A: real-time ReAct step events) ────────────────────

export interface ChatStreamEvent {
  type:
    | "start"
    | "iteration_planned"
    | "thought_stream"
    | "step_complete"
    | "answer_start"
    | "token"
    | "answer_end"
    | "done"
    | "error"
    | string;
  iteration?: number;
  thought?: string;
  action?: string;
  params?: Record<string, unknown>;
  duration_ms?: number;
  preview?: string;
  text?: string;            // present on "token" events
  fallback_used?: boolean;  // present on "answer_end"
  result?: ChatResponse;
  message?: string;
  session?: string;
  timestamp?: number;
}

/**
 * Stream a chat turn. Calls `onEvent` for every server-sent event in order:
 *   start → (iteration_planned + step_complete)* → done | error
 *
 * Resolves with the final ChatResponse from the `done` event, or rejects on
 * `error` event / network failure.
 */
export async function sendChatStream(
  message: string,
  history: ChatMessage[],
  ssh: SSHCredentials | null | undefined,
  sessionId: string | null | undefined,
  model: string | null | undefined,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<ChatResponse> {
  const body: Record<string, unknown> = { message, history };
  if (ssh) body.ssh = ssh;
  if (sessionId) body.session_id = sessionId;
  if (model) body.model = model;

  const res = await fetch(apiUrl("/api/chat/stream"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(body),
    signal,
    credentials: "include",
  });

  if (!res.ok || !res.body) {
    throw new Error(`HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: ChatResponse | null = null;
  let errorMessage: string | null = null;

  // SSE messages are separated by a blank line ("\n\n"). Each message has
  // one or more lines like "data: <json>". Accumulate bytes, split on the
  // blank line, concat all data: lines per frame, then JSON.parse.
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      sep = buffer.indexOf("\n\n");

      const dataLines: string[] = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).replace(/^ /, ""));
        }
      }
      if (dataLines.length === 0) continue;

      let evt: ChatStreamEvent;
      try {
        evt = JSON.parse(dataLines.join("\n")) as ChatStreamEvent;
      } catch {
        continue; // ignore malformed frames; keep stream alive
      }

      try {
        onEvent(evt);
      } catch {
        // Consumer errors must not break the stream loop.
      }

      if (evt.type === "done" && evt.result) {
        finalResult = evt.result;
      } else if (evt.type === "error") {
        errorMessage = evt.message ?? "stream error";
      }
    }
  }

  if (errorMessage) throw new Error(errorMessage);
  if (!finalResult) {
    throw new Error("stream ended without a 'done' event");
  }
  return finalResult;
}

// ── Session history ───────────────────────────────────────────────────────────

// ── Local auth ────────────────────────────────────────────────────────────────

export async function getAuthStatus(): Promise<AuthStatus> {
  const res = await fetch(apiUrl("/api/auth/me"), { credentials: "include" });
  if (!res.ok) {
    return { auth_enabled: true, allow_signup: false, user: null };
  }
  return res.json();
}

export async function login(username: string, password: string): Promise<AuthStatus> {
  return fetchJson("/api/auth/login", {
    method: "POST",
    body: { username, password },
  });
}

export async function signup(
  username: string,
  password: string,
  displayName?: string,
  email?: string,
): Promise<AuthStatus> {
  return fetchJson("/api/auth/signup", {
    method: "POST",
    body: {
      username,
      password,
      display_name: displayName || undefined,
      email: email || undefined,
    },
  });
}

export async function logout(): Promise<void> {
  await fetchJson("/api/auth/logout", { method: "POST" });
}

// ── Account self-service (password change, email, reset) ────────────────────

export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  return fetchJson("/api/auth/change-password", {
    method: "POST",
    body: { current_password: currentPassword, new_password: newPassword },
  });
}

export async function updateEmail(
  email: string | null,
  currentPassword: string,
): Promise<AuthStatus> {
  return fetchJson("/api/auth/update-email", {
    method: "POST",
    body: { email: email || null, current_password: currentPassword },
  });
}

export async function forgotPassword(email: string): Promise<{ ok: boolean; message?: string }> {
  return fetchJson("/api/auth/forgot-password", {
    method: "POST",
    body: { email },
  });
}

export async function resetPassword(
  token: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  return fetchJson("/api/auth/reset-password", {
    method: "POST",
    body: { token, new_password: newPassword },
  });
}

export async function listChatSessions(): Promise<ChatSession[]> {
  const data = await fetchJson("/api/sessions");
  return Array.isArray(data.sessions) ? data.sessions : [];
}

export async function createChatSession(title?: string): Promise<ChatSession> {
  const data = await fetchJson("/api/sessions", {
    method: "POST",
    body: { title },
  });
  return data.session as ChatSession;
}

export async function deleteChatSession(sessionId: string): Promise<void> {
  await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export async function getHistory(sessionId: string): Promise<HistoryMessage[]> {
  try {
    const detail = await getHistoryDetail(sessionId);
    return detail.messages;
  } catch {
    return [];
  }
}

export async function getHistoryDetail(sessionId: string): Promise<HistoryDetail> {
  const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/history`), { credentials: "include" });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const error = await res.json();
      message = error.detail || error.error || message;
    } catch {
      // Preserve HTTP status for callers that need auth/access branching.
    }
    throw new ApiError(message, res.status);
  }
  const data = await res.json();
  return {
    session_id: data.session_id ?? sessionId,
    access_mode: data.access_mode === "admin_readonly" ? "admin_readonly" : "owned",
    readonly: Boolean(data.readonly),
    owner_username: data.owner_username ?? null,
    owner_display_name: data.owner_display_name ?? null,
    title: data.title ?? null,
    messages: Array.isArray(data.messages) ? data.messages : [],
  };
}

export async function clearHistory(sessionId: string): Promise<void> {
  try {
    await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/history`), { method: "DELETE", credentials: "include" });
  } catch {
    // best-effort
  }
}

/**
 * Append a series of user/assistant messages to the session history. Used by
 * client-side flows that produce an exchange without going through the chat
 * stream (e.g. the /rca slash command), so the turns become first-class
 * history rather than client-only state.
 */
export async function appendSessionMessages(
  sessionId: string,
  messages: Array<{ role: "user" | "assistant"; content: string; tool_used?: string }>,
): Promise<void> {
  const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/messages`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ messages }),
  });
  if (!res.ok) {
    throw new ApiError(`HTTP ${res.status}`, res.status);
  }
}

export async function exportPostMortem(sessionId: string): Promise<string> {
  const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/export`), {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`Failed to export post-mortem: HTTP ${res.status}`);
  }
  const data = await res.json();
  return data.markdown;
}

// ── SSH target ────────────────────────────────────────────────────────────────

export async function getSshTarget(sessionId: string): Promise<SSHTarget | null> {
  try {
    const res = await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/ssh-target`), { credentials: "include" });
    if (!res.ok) return null;
    const data = await res.json();
    return data.ssh_target ?? null;
  } catch {
    return null;
  }
}

export async function saveSshTarget(
  sessionId: string,
  target: SSHTarget
): Promise<void> {
  try {
    await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/ssh-target`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(target),
      credentials: "include",
    });
  } catch {
    // best-effort
  }
}

export async function deleteSshTarget(sessionId: string): Promise<void> {
  try {
    await fetch(apiUrl(`/api/sessions/${encodeURIComponent(sessionId)}/ssh-target`), { method: "DELETE", credentials: "include" });
  } catch {
    // best-effort
  }
}

// ── Health ────────────────────────────────────────────────────────────────────

export async function checkHealth() {
  try {
    const res = await fetch(apiUrl("/api/health"), { credentials: "include" });
    return res.ok ? res.json() : null;
  } catch {
    return null;
  }
}

// ── Execute (approval gate) ──────────────────────────────────────────────────

export async function executeCommand(
  command: string,
  confirm = false,
  ssh?: SSHCredentials | null,
  sessionId?: string | null,
  stdin?: string | null
): Promise<ExecuteResponse> {
  const body: Record<string, unknown> = { command, confirm };
  if (ssh) body.ssh = ssh;
  if (sessionId) body.session_id = sessionId;
  if (stdin) body.stdin = stdin;
  const res = await fetch(apiUrl("/api/execute"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ── Cluster connection ───────────────────────────────────────────────────────

export async function clusterAutodetect(): Promise<{
  in_cluster: boolean;
  contexts: KubeContext[];
  kubeconfig_path?: string | null;
  current_context?: string | null;
  message?: string;
  error?: string;
}> {
  return fetchJson("/api/cluster/autodetect");
}

export async function clusterUploadKubeconfig(
  sessionId: string,
  content: string
): Promise<{
  contexts: KubeContext[];
  kubeconfig_path?: string;
  current_context?: string | null;
  message?: string;
  error?: string;
}> {
  return fetchJson("/api/cluster/connect/kubeconfig", {
    method: "POST",
    body: { session_id: sessionId, content },
  });
}

export async function clusterConnectContext(body: {
  session_id: string;
  context_name: string;
  mode: "autodetect" | "kubeconfig-upload";
  kubeconfig_path?: string | null;
}): Promise<ClusterStatus & { error?: string }> {
  return fetchJson("/api/cluster/connect/context", {
    method: "POST",
    body,
  });
}

export async function clusterDisconnect(sessionId: string): Promise<void> {
  await fetchJson("/api/cluster/disconnect", {
    method: "POST",
    body: { session_id: sessionId },
  });
}

export async function clusterStatus(sessionId: string): Promise<ClusterStatus> {
  return fetchJson(`/api/cluster/status/${encodeURIComponent(sessionId)}`);
}

// ── Legacy form dashboard client ─────────────────────────────────────────────

export const api = {
  analyze: (body: unknown) => fetchJson("/api/analyze", { method: "POST", body }),
  fix: (body: unknown) => fetchJson("/api/fix", { method: "POST", body }),
  categories: () => fetchJson("/api/categories"),
  runbook: (body: unknown) => fetchJson("/api/runbook", { method: "POST", body }),
  report: (body: unknown) => fetchJson("/api/report", { method: "POST", body }),
  summary: (body: unknown) => fetchJson("/api/summary", { method: "POST", body }),
  investigate: (body: unknown) => fetchJson("/api/investigate", { method: "POST", body }),
  pods: (body: unknown) => fetchJson("/api/pods", { method: "POST", body }),
  describe: (body: unknown) => fetchJson("/api/describe", { method: "POST", body }),
  logs: (body: unknown) => fetchJson("/api/logs", { method: "POST", body }),
  events: (body: unknown) => fetchJson("/api/events", { method: "POST", body }),
  find: (body: unknown) => fetchJson("/api/find", { method: "POST", body }),
  deployment: (body: unknown) => fetchJson("/api/deployment", { method: "POST", body }),
  service: (body: unknown) => fetchJson("/api/service", { method: "POST", body }),
  endpoints: (body: unknown) => fetchJson("/api/endpoints", { method: "POST", body }),
  rolloutStatus: (body: unknown) => fetchJson("/api/rollout-status", { method: "POST", body }),
  contexts: () => fetchJson("/api/contexts"),
  currentContext: () => fetchJson("/api/contexts/current"),
  switchContext: (contextName: string) =>
    fetchJson("/api/contexts/switch", { method: "POST", body: { context_name: contextName } }),
  addContext: (body: unknown) => fetchJson("/api/contexts/add", { method: "POST", body }),
  restart: (body: unknown) => fetchJson("/api/restart", { method: "POST", body }),
  scale: (body: unknown) => fetchJson("/api/scale", { method: "POST", body }),
  deletePod: (body: unknown) => fetchJson("/api/delete-pod", { method: "POST", body }),
  exec: (body: unknown) => fetchJson("/api/exec", { method: "POST", body }),
  patch: (body: unknown) => fetchJson("/api/patch", { method: "POST", body }),
};

export interface AgentRunDetails {
  run: {
    id: string;
    session_id?: string;
    user_id?: string;
    status: string;
    model?: string;
    total_tokens_in: number;
    total_tokens_out: number;
    total_cached_tokens_in: number;
    total_cost_usd: number;
  };
  steps: Array<{
    id: number;
    iteration: number;
    step_kind: string;
    thought?: string;
    action: string;
    status: string;
    tokens_in: number;
    tokens_out: number;
    cached_tokens_in: number;
    cost_usd: number;
    step_model?: string;
    duration_ms?: number;
  }>;
  access_mode: string;
}

export async function fetchAgentRunDetails(runId: string): Promise<AgentRunDetails> {
  const res = await fetch(apiUrl(`/api/agent-runs/${encodeURIComponent(runId)}`), {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}


// ── Desktop mode ────────────────────────────────────────────────────────────
// These endpoints exist only when the backend runs with KUBEASTRA_MODE=desktop.
// A 404 from `fetchDesktopSetup` is the signal that this is a server
// deployment, so callers should treat it as "not desktop" rather than an error.

export interface DesktopSetupState {
  configured: boolean;
  llm_provider: string | null;
  needs_embeddings_key: boolean;
  memory_available: boolean;
  memory_mode: "vector" | "keyword";
  keychain_secure: boolean;
  keychain_backend: string;
}

export interface DesktopSettingsState {
  memory_enabled: boolean;
  remote_diagnostics_enabled: boolean;
  memory_mode: "vector" | "keyword";
  memory_available: boolean;
  keychain_secure: boolean;
  keychain_backend: string;
}

/** Returns null in server mode (endpoint absent), the state otherwise. */
export async function fetchDesktopSetup(): Promise<DesktopSetupState | null> {
  try {
    return (await fetchJson("/api/desktop/setup")) as DesktopSetupState;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export async function setupDesktopLlm(payload: {
  provider: string;
  api_key?: string;
}): Promise<{ ok: boolean; provider: string; needs_embeddings_key: boolean }> {
  return fetchJson("/api/desktop/setup/llm", { method: "POST", body: payload });
}

export async function setupDesktopEmbeddings(payload: {
  provider: string;
  api_key: string;
}): Promise<{ ok: boolean; provider: string; dim: number }> {
  return fetchJson("/api/desktop/setup/embeddings", { method: "POST", body: payload });
}

export async function fetchDesktopSettings(): Promise<DesktopSettingsState> {
  return fetchJson("/api/desktop/settings");
}

export async function updateDesktopSettings(payload: {
  memory_enabled?: boolean;
  remote_diagnostics_enabled?: boolean;
}): Promise<DesktopSettingsState> {
  return fetchJson("/api/desktop/settings", { method: "PUT", body: payload });
}

export async function forgetDesktopSecret(name: string): Promise<{ ok: boolean }> {
  return fetchJson(`/api/desktop/secrets/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

// ── Live cluster ──────────────────────────────────────────────────────────
// Both endpoints are cached server-side for 30s and poll on the same cadence,
// so a slow cluster costs the header staleness rather than a spinner.

export interface ClusterCounters {
  pods_ready: number;
  pods_total: number;
  workloads_degraded: number;
  alerts_active: number;
  alerts_sev1: number;
}

export interface ClusterSummary {
  cluster: string | null;
  context: string | null;
  namespace: string | null;
  counters: ClusterCounters | null;
  generated_at: string;
  cache_age_seconds: number;
  // Why there are no counters, when there are none. The header words these
  // differently: one asks you to connect a cluster, the other to ask for
  // access.
  reason: "no_cluster" | "insufficient_rbac" | null;
}

export interface TopologyNode {
  id: string;
  kind: string;
  namespace: string;
  name: string;
  health: "green" | "amber" | "red" | "idle";
  replicas: { ready: number; desired: number };
}

export interface TopologyEdge {
  source: string;
  target: string;
  kind: string;
  rate_rps: number;
}

export interface ClusterTopology {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  generated_at: string;
}

export async function fetchClusterSummary(
  sessionId: string,
): Promise<ClusterSummary> {
  return fetchJson(
    `/api/v1/cluster/summary/${encodeURIComponent(sessionId)}`,
  );
}

export async function fetchClusterTopology(
  sessionId: string,
  scope: "all" | "alerting" = "alerting",
): Promise<ClusterTopology> {
  return fetchJson(
    `/api/v1/cluster/topology/${encodeURIComponent(sessionId)}?scope=${scope}`,
  );
}
