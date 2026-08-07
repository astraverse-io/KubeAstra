"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import ResultCard from "../../components/ResultCard";
import UserMessage from "../../components/astra/UserMessage";
import AstraMessage from "../../components/astra/AstraMessage";
import ReasoningBubble, { type ToolStep } from "../../components/astra/ReasoningBubble";
import RootCauseCard from "../../components/astra/RootCauseCard";
import IntentBar from "../../components/astra/IntentBar";
import ApprovalOverlay from "../../components/astra/ApprovalOverlay";
import ClusterConnect, { type ClusterInfo } from "../../components/ClusterConnect";
import {
  sendChat,
  sendChatStream,
  sendFeedback,
  checkHealth,
  getHistory,
  clearHistory,
  getSshTarget,
  saveSshTarget,
  deleteSshTarget,
  executeCommand,
  clusterStatus,
  clusterDisconnect,
  type ChatMessage,
  type SSHCredentials,
  type SSHTarget,
  type HistoryMessage,
  type StreamEvent,
} from "../../lib/api";

/* ── types ───────────────────────────────────────────────────── */

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  tool?: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
  loading?: boolean;
  viaSSH?: boolean;
  suggestedActions?: Array<{ label: string; command: string; confirm?: boolean }>;
  // Auto-capture id (blueprint §9). Set when the backend classified this
  // chat as worthy. Presence enables the 👍/👎 buttons below the bubble.
  captureId?: string | null;
  // Local UI state once the user has clicked one of the thumbs buttons.
  // "up" → "saved as runbook", "down" → "thanks, won't show again".
  feedbackSent?: "up" | "down" | null;
}

interface Health {
  ai_enabled: boolean;
  kubectl_available: boolean;
}

/* ── helpers ─────────────────────────────────────────────────── */

/**
 * Style for the 👍 / 👎 buttons (blueprint §9 thumbs UI).
 * Defined module-level so it isn't re-created on every render.
 */
const FEEDBACK_BTN: React.CSSProperties = {
  background: "transparent",
  border: "1px solid var(--rule)",
  borderRadius: 6,
  padding: "2px 8px",
  cursor: "pointer",
  fontSize: 13,
  lineHeight: 1,
  color: "var(--ink-2)",
};

function uid() {
  return Math.random().toString(36).slice(2);
}

function getOrCreateSessionId(): string {
  if (typeof window === "undefined") return uid();
  let sid = localStorage.getItem("k8s_session_id");
  if (!sid) {
    // Use crypto.randomUUID for strong entropy (shareable session URLs must not be guessable)
    sid = typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : uid() + uid() + uid() + uid();  // fallback: ~96 bits
    localStorage.setItem("k8s_session_id", sid);
  }
  return sid;
}

/** Unwrap ReAct-wrapped results.
 *  The DB stores {react_steps, tool_result} but ResultCard expects the raw tool result. */
function unwrapResult(result: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!result) return null;
  if ("tool_result" in result && result.tool_result && typeof result.tool_result === "object") {
    return result.tool_result as Record<string, unknown>;
  }
  return result;
}

function historyToMessages(history: HistoryMessage[]): Message[] {
  return history.map((h) => {
    // Restore capture_id (blueprint §9) so the 👍/👎 buttons reappear after
    // a page refresh on captured chats. The backend persists capture_id in
    // result.capture_id when maybe_capture succeeded.
    const rawResult = h.result ?? null;
    const capture =
      rawResult && typeof rawResult === "object" && "capture_id" in rawResult
        ? (rawResult as { capture_id?: string | null }).capture_id ?? null
        : null;
    return {
      id: uid(),
      role: h.role,
      text: h.content,
      tool: h.tool_used,
      result: unwrapResult(rawResult),
      error: h.error ?? null,
      captureId: capture,
    };
  });
}

function formatTime(): string {
  const d = new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Extract a clean summary from AI text, stripping markdown tables and raw data listings.
 *  The ResultCard already renders structured data beautifully — we only want the
 *  conversational insight (e.g. "There are 6 pods, 5 Pending and 1 Running.") */
function extractSummaryText(text: string): string {
  const lines = text.split("\n");
  const kept: string[] = [];
  let inTable = false;

  for (const line of lines) {
    const trimmed = line.trim();

    // Skip markdown table rows (pipes) and separator rows (|---|)
    if (/^\|.*\|$/.test(trimmed)) { inTable = true; continue; }
    if (/^[\s|:-]+$/.test(trimmed) && inTable) continue;
    inTable = false;

    // Skip lines that are just a list of resource names (e.g. "- namespace-name",
    // bare "my-namespace", "`pod-name`", "- **bold-name**") — keep actual sentences
    if (/^[-*]\s+[`*]*[a-z0-9][\w.-]*[`*]*$/i.test(trimmed)) continue;
    if (/^[`*]*[a-z0-9][\w.-]*[`*]*$/i.test(trimmed) && trimmed.length < 80) continue;
    if (/^\d+\.\s+[`*]*[a-z0-9][\w.-]*[`*]*$/i.test(trimmed)) continue;
    // Skip "- `name` — Pending" style lines (status after resource name)
    if (/^[-*]\s+[`*]*[\w.-]+[`*]*\s*[-–—:]\s*(Running|Pending|Error|CrashLoopBackOff|Succeeded|Terminating|Unknown)/i.test(trimmed)) continue;

    // Skip raw data lines like "name | status | ready | restarts"
    if ((trimmed.match(/\|/g) || []).length >= 2) continue;

    // Skip blank lines at the start
    if (kept.length === 0 && !trimmed) continue;

    kept.push(line);
  }

  // Trim trailing blank lines
  while (kept.length > 0 && !kept[kept.length - 1].trim()) kept.pop();

  const result = kept.join("\n").trim();

  // If what's left is just a header ("Here are the namespaces:") with nothing else
  // meaningful, that's fine — keep it as a short intro line above the card.
  // But if the entire text was just a table/list and nothing remains, return empty.
  return result;
}

/** Map tool_used to reasoning steps.
 *  When the result contains react_steps (from the ReAct loop),
 *  use the real investigation trace instead of simulated steps. */
function toolToSteps(tool: string, result: Record<string, unknown> | null): ToolStep[] {
  // Check for ReAct steps in the result metadata
  const reactSteps = result?.react_steps as Array<{
    thought?: string; action: string; params?: Record<string, unknown>; duration_ms?: number;
  }> | undefined;

  if (reactSteps && reactSteps.length > 0) {
    return reactSteps
      .filter(s => s.action !== "answer")
      .map(s => ({
        tool: s.action,
        status: "done" as const,
        result: s.thought || `${s.action} completed`,
      }));
  }

  // Fallback: static step mapping for single-shot / keyword path
  const steps: ToolStep[] = [];
  if (tool === "investigate_pod" || tool === "investigate_workload") {
    steps.push({ tool: "kubectl", status: "done", result: "pod status retrieved" });
    if (result?.logs || result?.current_logs) steps.push({ tool: "logs", status: "done", result: "log tail captured" });
    if (result?.events) steps.push({ tool: "events", status: "done", result: "events scanned" });
    if (result?.describe_highlights) steps.push({ tool: "describe", status: "done", result: "highlights extracted" });
    steps.push({ tool: "ai", status: "done", result: "analysis complete" });
  } else if (tool === "get_pods") {
    steps.push({ tool: "kubectl", status: "done", result: "pods listed" });
  } else if (tool === "get_events") {
    steps.push({ tool: "events", status: "done", result: "events fetched" });
  } else if (tool === "get_pod_logs") {
    steps.push({ tool: "logs", status: "done", result: "logs retrieved" });
  } else if (tool === "analyze_error") {
    steps.push({ tool: "ai", status: "done", result: "error analyzed" });
  } else if (tool) {
    steps.push({ tool: tool, status: "done" });
  }
  return steps;
}

/** Extract root-cause data from investigation results */
function extractRootCause(result: Record<string, unknown>) {
  const classificationObj = (result.classification || {}) as Record<string, unknown>;
  const classificationMode = String(classificationObj.mode || result.classification || "");
  const ai = (result.ai || {}) as Record<string, unknown>;
  const aiAnalysis = (ai.ai_analysis || {}) as Record<string, unknown>;
  const severityLabel = String(aiAnalysis.severity || classificationMode || "");
  const severity = severityLabel.toLowerCase().includes("critical") || classificationMode === "CrashLoopBackOff" || classificationMode === "ImagePullBackOff"
    ? "critical" as const
    : severityLabel.toLowerCase().includes("warn") || classificationMode === "Pending"
    ? "warning" as const
    : "info" as const;

  const describe = (result.describe || {}) as Record<string, unknown>;
  const highlights = (describe.highlights || result.describe_highlights || {}) as Record<string, unknown>;
  const metrics = [];
  if (highlights.restart_count !== undefined) {
    metrics.push({ label: "Restarts", value: String(highlights.restart_count), color: Number(highlights.restart_count) > 0 ? "#EF4444" : "#64748B" });
  }
  const status = String(classificationMode || result.effective_status || result.status || highlights.state || "");
  if (status) metrics.push({ label: "Status", value: status, color: status.includes("Crash") || status.includes("OOM") ? "#EF4444" : "#34D399" });
  const ready = String(highlights.ready || "");
  if (ready) metrics.push({ label: "Ready", value: ready, color: ready === "True" ? "#34D399" : "#EF4444" });

  // Build evidence string
  const evidenceLines: string[] = [];
  const currentLogs = (result.logs_current as Record<string, unknown> | undefined)?.logs;
  const previousLogs = (result.logs_previous as Record<string, unknown> | undefined)?.logs;
  const stderr = result.stderr;
  const events = (result.events as Record<string, unknown> | undefined)?.events as Array<Record<string, unknown>> | undefined;
  if (currentLogs) evidenceLines.push("# Current Logs", String(currentLogs).slice(0, 700));
  if (previousLogs) evidenceLines.push("", "# Previous Logs", String(previousLogs).slice(0, 500));
  if (!currentLogs && !previousLogs && stderr) evidenceLines.push("# Error", String(stderr).slice(0, 500));
  if (events && events.length > 0) {
    evidenceLines.push("", "# Events");
    for (const event of events.slice(0, 5)) {
      evidenceLines.push(`- [${String(event.type || "")}] ${String(event.reason || "")}: ${String(event.message || "")}`.slice(0, 220));
    }
  }

  const pod = String(result.pod_name || result.pod || result.name || "");
  const ns = String(result.namespace || "");
  const rootCause = String(aiAnalysis.root_cause || "");
  const solution = String(aiAnalysis.solution || "");
  const summary = rootCause
    ? solution ? `${rootCause}\n\nSuggested fix: ${solution}` : rootCause
    : String(result.error || result.analysis || classificationMode || "");
  const confidence = aiAnalysis.confidence !== undefined ? Number(aiAnalysis.confidence) : undefined;

  // Extract manual fix steps from AI analysis (shown when no automated commands)
  const steps = (aiAnalysis.steps || []) as string[];
  const prevention = String(aiAnalysis.prevention || "");

  return {
    severity,
    pod,
    namespace: ns,
    summary,
    metrics,
    evidence: evidenceLines.join("\n"),
    title: classificationMode || "Investigation Result",
    confidence,
    steps,
    prevention,
  };
}

/* ── SSH Panel (collapsible drawer) ──────────────────────────── */

function SSHDrawer({ onConnect, onDisconnect, connected }: {
  onConnect: (creds: SSHCredentials) => void;
  onDisconnect: () => void;
  connected: SSHCredentials | null;
}) {
  const [open, setOpen] = useState(false);
  const [host, setHost] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState("22");
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "ok" | "err">("idle");
  const [testError, setTestError] = useState("");

  const handleConnect = async () => {
    if (!host.trim() || !username.trim() || !password) return;
    const creds: SSHCredentials = { host: host.trim(), username: username.trim(), password, port: parseInt(port, 10) || 22 };
    setTestStatus("testing");
    setTestError("");
    try {
      const res = await sendChat("list clusters", [], creds);
      if (res.error && res.tool_used === "error") { setTestStatus("err"); setTestError(res.error); return; }
      setTestStatus("ok");
      onConnect(creds);
      setOpen(false);
    } catch (e) { setTestStatus("err"); setTestError(String(e)); }
  };

  if (connected) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span style={{
          fontSize: 10, fontFamily: "var(--mono)", fontWeight: 600, color: "var(--accent)",
          background: "var(--accent-bg)", border: "1px solid var(--accent-bd)",
          borderRadius: 4, padding: "3px 7px", display: "flex", alignItems: "center", gap: 4,
        }}>
          <span style={{ width: 5, height: 5, borderRadius: "50%", background: "var(--accent)", animation: "blink 2s infinite" }}/>
          SSH: {connected.username}@{connected.host}
        </span>
        <button onClick={() => { onDisconnect(); setTestStatus("idle"); setPassword(""); }}
          style={{ fontSize: 10, color: "var(--ink-4)", background: "none", border: "1px solid var(--rule)", borderRadius: 4, padding: "3px 7px", cursor: "pointer" }}>
          Disconnect
        </button>
      </div>
    );
  }

  return (
    <div style={{ position: "relative" }}>
      <button onClick={() => setOpen(v => !v)}
        style={{ fontSize: 10, color: "var(--ink-4)", background: "none", border: "1px solid var(--rule)", borderRadius: 4, padding: "3px 7px", cursor: "pointer", fontFamily: "var(--mono)" }}>
        SSH Cluster
      </button>
      {open && (
        <div style={{
          position: "absolute", right: 0, top: 32, zIndex: 50, width: 300, padding: 14,
          background: "#FFFFFF", border: "1px solid var(--accent-bd)", borderRadius: 10,
          boxShadow: "0 4px 20px rgba(0,0,0,0.08)", display: "flex", flexDirection: "column", gap: 8,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Connect to Remote Cluster</span>
            <button onClick={() => setOpen(false)} style={{ background: "none", border: "none", color: "var(--ink-4)", cursor: "pointer", fontSize: 16 }}>&times;</button>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <input value={host} onChange={e => setHost(e.target.value)} placeholder="10.0.1.5"
              style={{ flex: 1, background: "var(--paper)", border: "1px solid var(--rule)", borderRadius: 6, padding: "6px 10px", color: "var(--ink)", fontSize: 12, outline: "none" }}/>
            <input value={port} onChange={e => setPort(e.target.value)} style={{ width: 50, background: "var(--paper)", border: "1px solid var(--rule)", borderRadius: 6, padding: "6px 10px", color: "var(--ink)", fontSize: 12, outline: "none" }}/>
          </div>
          <input value={username} onChange={e => setUsername(e.target.value)} placeholder="ubuntu"
            style={{ background: "var(--paper)", border: "1px solid var(--rule)", borderRadius: 6, padding: "6px 10px", color: "var(--ink)", fontSize: 12, outline: "none" }}/>
          <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••"
            style={{ background: "var(--paper)", border: "1px solid var(--rule)", borderRadius: 6, padding: "6px 10px", color: "var(--ink)", fontSize: 12, outline: "none" }}/>
          {testStatus === "err" && <div style={{ fontSize: 10, color: "var(--red)", padding: "4px 8px", background: "var(--red-bg)", borderRadius: 4 }}>{testError}</div>}
          <button onClick={handleConnect} disabled={!host.trim() || !username.trim() || !password || testStatus === "testing"}
            style={{ padding: "8px", background: "var(--accent-bg)", border: "1px solid var(--accent-bd)", borderRadius: 6, color: "var(--accent)", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
            {testStatus === "testing" ? "Testing…" : "Connect & Test"}
          </button>
        </div>
      )}
    </div>
  );
}

/* ── main component ──────────────────────────────────────────── */

export default function ChatPage({ sharedSessionId }: { sharedSessionId?: string } = {}) {
  const [sessionId] = useState<string>(() => sharedSessionId || getOrCreateSessionId());
  const [shareToast, setShareToast] = useState(false);
  const isSharedView = !!sharedSessionId;
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthLoaded, setHealthLoaded] = useState(false);
  const [sshCreds, setSshCreds] = useState<SSHCredentials | null>(null);
  const [pendingReconnect, setPendingReconnect] = useState<SSHTarget | null>(null);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [showOverlay, setShowOverlay] = useState(false);
  const [overlayActions, setOverlayActions] = useState<Array<{ label: string; command: string }>>([]);
  const [overlayResource, setOverlayResource] = useState("");
  const [overlayNamespace, setOverlayNamespace] = useState("");
  const [overlaySourceMsgId, setOverlaySourceMsgId] = useState<string | null>(null);
  const [executedMsgIds, setExecutedMsgIds] = useState<Set<string>>(new Set());
  // cluster connection state
  const [clusterConn, setClusterConn] = useState<ClusterInfo | null>(null);
  const [showConnectScreen, setShowConnectScreen] = useState(false);
  const [clusterChecked, setClusterChecked] = useState(false);
  // reasoning animation
  const [reasoningSteps, setReasoningSteps] = useState<ToolStep[]>([]);
  const [isThinking, setIsThinking] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    checkHealth().then((h) => {
      if (h) setHealth(h as Health);
      setHealthLoaded(true);

      // Check cluster connection: DB first, then fallback to local kubectl
      clusterStatus(sessionId).then((cs) => {
        if (cs.connected) {
          setClusterConn({
            mode: cs.mode ?? "autodetect",
            context_name: cs.context_name ?? "",
            cluster_name: cs.cluster_name ?? "",
            server_url: cs.server_url ?? "",
            namespace: cs.namespace ?? "default",
          });
        } else if (!h || !(h as Health).kubectl_available) {
          // No saved connection and no local kubectl → show connect screen
          setShowConnectScreen(true);
        }
        // If h.kubectl_available is true and no saved connection, proceed with local kubectl (backward compatible)
        setClusterChecked(true);
      });
    });
    getHistory(sessionId).then((history) => { if (history.length > 0) setMessages(historyToMessages(history)); setHistoryLoaded(true); });
    getSshTarget(sessionId).then((target) => { if (target) setPendingReconnect(target); });
  }, [sessionId]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, reasoningSteps]);

  const handleConnect = useCallback((creds: SSHCredentials) => {
    setSshCreds(creds);
    setPendingReconnect(null);
    saveSshTarget(sessionId, { host: creds.host, username: creds.username, port: creds.port ?? 22 });
    setMessages(prev => [...prev, { id: uid(), role: "assistant", text: `Connected to **${creds.username}@${creds.host}** via SSH.` }]);
  }, [sessionId]);

  const handleDisconnect = useCallback(() => {
    setSshCreds(null);
    deleteSshTarget(sessionId);
    setMessages(prev => [...prev, { id: uid(), role: "assistant", text: "SSH session closed. Reverting to local cluster." }]);
  }, [sessionId]);

  const handleReconnect = useCallback(async (password: string) => {
    if (!pendingReconnect) return;
    const creds: SSHCredentials = { ...pendingReconnect, password };
    try {
      const res = await sendChat("list clusters", [], creds, sessionId);
      if (res.error && res.tool_used === "error") { deleteSshTarget(sessionId); setPendingReconnect(null); return; }
      setSshCreds(creds); setPendingReconnect(null);
      setMessages(prev => [...prev, { id: uid(), role: "assistant", text: `Reconnected to **${creds.username}@${creds.host}** via SSH.` }]);
    } catch { deleteSshTarget(sessionId); setPendingReconnect(null); }
  }, [pendingReconnect, sessionId]);

  const submit = useCallback(async (text: string) => {
    if (!text.trim() || loading) return;
    const userMsg: Message = { id: uid(), role: "user", text: text.trim(), viaSSH: !!sshCreds };
    const thinkingMsg: Message = { id: uid(), role: "assistant", text: "", loading: true };
    setMessages(prev => [...prev, userMsg, thinkingMsg]);
    setLoading(true);
    setIsThinking(true);

    // Real reasoning steps are driven by SSE events from the backend.
    // Each iteration_planned adds a `running` step; step_complete marks it done.
    setReasoningSteps([]);

    const history: ChatMessage[] = messages.filter(m => !m.loading).slice(-10).map(m => ({ role: m.role, content: m.text }));

    // Token accumulator — incremented on every `token` event so the message
    // text grows incrementally as the backend streams the answer.
    let streamedText = "";

    // Handler invoked for every event the server emits.
    const handleEvent = (event: StreamEvent) => {
      if (event.type === "iteration_planned") {
        // LLM picked a tool (or chose to answer). Show it as a running step.
        // "answer" actions aren't real tool calls — don't pollute the trace.
        if (event.action === "answer") return;
        setReasoningSteps(prev => [...prev, { tool: event.action, status: "running" }]);
      } else if (event.type === "step_complete") {
        // Mark the most recent running step (matching this action) as done.
        setReasoningSteps(prev => {
          const next = [...prev];
          for (let i = next.length - 1; i >= 0; i--) {
            if (next[i].tool === event.action && next[i].status === "running") {
              next[i] = { ...next[i], status: "done" };
              break;
            }
          }
          return next;
        });
      } else if (event.type === "answer_start") {
        // Token streaming begins — flip the message out of "loading" state.
        // Show "…" as a placeholder so the bubble isn't blank during the
        // 100-300ms window between answer_start and the first `token` event.
        // The first `token` replaces "…" by overwriting `text` with snapshot
        // of streamedText (which starts as "").
        setIsThinking(false);
        setMessages(prev => prev.map(m =>
          m.id === thinkingMsg.id ? { ...m, loading: false, text: "…" } : m
        ));
      } else if (event.type === "token") {
        // Append the new token chunk. React batches setState — the markdown
        // renderer re-renders on each token which is acceptable for the
        // typical 500-1500 token response size.
        // The first token implicitly replaces the "…" placeholder set by
        // answer_start, because we overwrite `text` with `snapshot`.
        streamedText += event.text;
        const snapshot = streamedText;
        setMessages(prev => prev.map(m =>
          m.id === thinkingMsg.id ? { ...m, text: snapshot } : m
        ));
      } else if (event.type === "answer_end") {
        // Finalize streaming complete. If the stream was truncated mid-way
        // (B2 fix in backend), append a subtle hint so the user knows the
        // answer they're seeing may be incomplete.
        if (event.truncated && streamedText) {
          const hint = "\n\n_(connection interrupted — answer may be incomplete)_";
          streamedText += hint;
          const snapshot = streamedText;
          setMessages(prev => prev.map(m =>
            m.id === thinkingMsg.id ? { ...m, text: snapshot } : m
          ));
        }
      }
      // start / done / error are handled outside this callback.
    };

    try {
      const res = await sendChatStream(text.trim(), history, sshCreds, sessionId, handleEvent);

      // Mark any straggler "running" steps done — the response arrived.
      setReasoningSteps(prev => prev.map(s =>
        s.status === "running" ? { ...s, status: "done" as const } : s
      ));
      setIsThinking(false);
      // Sync the final canonical state. If tokens streamed, `res.reply` should
      // match the accumulated text. If tokens were skipped (e.g. Phase B
      // disabled), `res.reply` is the ReAct draft and replaces our empty text.
      setMessages(prev => prev.map(m =>
        m.id === thinkingMsg.id
          ? { ...m, loading: false, text: res.reply, tool: res.tool_used,
              result: res.result, error: res.error,
              suggestedActions: res.suggested_actions,
              // Auto-capture id (blueprint §9). Present when the backend
              // classified this chat as worthy → enables the 👍/👎 buttons.
              captureId: (res as { capture_id?: string | null }).capture_id ?? null }
          : m
      ));
    } catch (err) {
      // Preserve any partial reasoning state + streamed text so the user
      // sees what ran rather than getting a blank error message.
      setIsThinking(false);
      const preservedText = streamedText
        ? `${streamedText}\n\n_(connection lost mid-response)_`
        : "Failed to reach the backend. Is it running?";
      setMessages(prev => prev.map(m =>
        m.id === thinkingMsg.id
          ? { ...m, loading: false, text: preservedText, error: String(err) }
          : m
      ));
    } finally { setLoading(false); }
  }, [loading, messages, sshCreds, sessionId]);

  const handleNewChat = useCallback(() => {
    setMessages([]);
    setReasoningSteps([]);
    clearHistory(sessionId);
  }, [sessionId]);

  /**
   * Handle 👍/👎 click on a captured assistant message (blueprint §9).
   *
   * Optimistic update: mark the message's feedbackSent immediately so the
   * thumbs UI updates instantly. Revert on error. The backend handles
   * idempotency — clicking twice is harmless.
   */
  const handleFeedback = useCallback(
    async (captureId: string, rating: "up" | "down", messageId: string) => {
      setMessages(prev => prev.map(m =>
        m.id === messageId ? { ...m, feedbackSent: rating } : m
      ));
      try {
        await sendFeedback(captureId, rating, { sessionId });
      } catch (err) {
        // Revert on failure so the user can retry.
        console.warn("feedback submission failed:", err);
        setMessages(prev => prev.map(m =>
          m.id === messageId ? { ...m, feedbackSent: null } : m
        ));
      }
    },
    [sessionId],
  );

  const handleShare = useCallback(() => {
    const url = `${window.location.origin}/chat/${sessionId}`;
    navigator.clipboard.writeText(url).then(() => {
      setShareToast(true);
      setTimeout(() => setShareToast(false), 2000);
    });
  }, [sessionId]);

  const handleReviewExecute = useCallback((actions: Array<{ label: string; command: string }>, resource: string, ns: string, msgId: string) => {
    setOverlayActions(actions);
    setOverlayResource(resource);
    setOverlayNamespace(ns);
    setOverlaySourceMsgId(msgId);
    setShowOverlay(true);
  }, []);

  const handleExecuteConfirm = useCallback(async () => {
    let anySuccess = false;
    for (const action of overlayActions) {
      try {
        const res = await executeCommand(action.command, sshCreds, sessionId);
        if (res.success) anySuccess = true;
        setMessages(prev => [...prev, {
          id: uid(), role: "assistant",
          text: res.success ? `✓ Executed: \`${action.command}\`\n\n${res.output}` : `✗ Failed: ${res.error}`,
        }]);
      } catch (e) {
        setMessages(prev => [...prev, { id: uid(), role: "assistant", text: `✗ Execution error: ${e}` }]);
      }
    }
    // Hide the "Review & Execute" button on the source message after execution
    if (anySuccess && overlaySourceMsgId) {
      setExecutedMsgIds(prev => new Set(prev).add(overlaySourceMsgId));
    }
  }, [overlayActions, sshCreds, sessionId, overlaySourceMsgId]);

  const handleClusterConnect = useCallback((info: ClusterInfo) => {
    setClusterConn(info);
    setShowConnectScreen(false);
    setMessages(prev => [...prev, { id: uid(), role: "assistant", text: `Connected to **${info.cluster_name || info.context_name}** via ${info.mode}.` }]);
  }, []);

  const handleClusterDisconnect = useCallback(() => {
    clusterDisconnect(sessionId);
    setClusterConn(null);
    // Also disconnect SSH if active
    if (sshCreds) {
      setSshCreds(null);
      deleteSshTarget(sessionId);
    }
    setShowConnectScreen(true);
  }, [sessionId, sshCreds]);

  const handleSSHFromConnect = useCallback((creds: { host: string; username: string; password: string; port: number }) => {
    const sshCred: SSHCredentials = { host: creds.host, username: creds.username, password: creds.password, port: creds.port };
    handleConnect(sshCred);
    setShowConnectScreen(false);
  }, [handleConnect]);

  const clusterDisplayName = clusterConn
    ? clusterConn.cluster_name || clusterConn.context_name
    : sshCreds
    ? sshCreds.host
    : health?.kubectl_available
    ? "local-cluster"
    : "no cluster";

  const clusterMode = clusterConn?.mode || (sshCreds ? "ssh" : "local");
  const isConnected = !!clusterConn || !!sshCreds || !!health?.kubectl_available;

  const isEmpty = historyLoaded && messages.length === 0;
  const isInvestigation = (tool?: string) => tool === "investigate_pod" || tool === "investigate_workload";

  // Show connection screen when needed
  if (showConnectScreen && clusterChecked && !isSharedView) {
    return (
      <div style={{ width: "100vw", height: "100vh", background: "var(--paper)", display: "flex", flexDirection: "column" }}>
        <ClusterConnect
          sessionId={sessionId}
          onConnect={handleClusterConnect}
          onSSHConnect={handleSSHFromConnect}
        />
      </div>
    );
  }

  return (
    <div style={{ width: "100vw", height: "100vh", background: "var(--paper)", display: "flex", flexDirection: "column", overflow: "hidden", position: "relative" }}>

      {/* ── Top bar ── */}
      <div style={{
        height: 50, flexShrink: 0, background: "#FFFFFF", borderBottom: "1px solid var(--rule)",
        boxShadow: "0 1px 0 var(--rule)",
        display: "flex", alignItems: "center", padding: "0 20px", gap: 14,
      }}>
        <svg viewBox="0 0 20 20" width="18" height="18">
          <polygon points="10,0.5 18.7,5.25 18.7,14.75 10,19.5 1.3,14.75 1.3,5.25"
            fill="none" stroke="var(--accent)" strokeWidth="0.9" opacity="0.5"/>
          <path d="M10,3 L10.7,8.2 L15.4,5.9 L11.5,10 L15.4,14.1 L10.7,11.8 L10,17 L9.3,11.8 L4.6,14.1 L8.5,10 L4.6,5.9 L9.3,8.2 Z"
            fill="var(--accent)"/>
        </svg>
        <span style={{ fontSize: 14, fontWeight: 700, color: "var(--ink)", letterSpacing: "-0.02em" }}>
          Kube<span style={{ color: "var(--accent)" }}>Astra</span>
        </span>
        <div style={{ width: 1, height: 18, background: "var(--rule)" }}/>
        <span style={{ fontSize: 10, color: "var(--ink-4)", fontFamily: "var(--mono)", fontWeight: 500 }}>Astra Intent</span>
        <div style={{ flex: 1 }}/>

        <SSHDrawer connected={sshCreds} onConnect={handleConnect} onDisconnect={handleDisconnect}/>

        {healthLoaded && (
          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            background: "var(--paper-2)", border: "1px solid var(--rule)",
            borderRadius: 6, padding: "4px 10px",
          }}>
            <div style={{ width: 6, height: 6, borderRadius: "50%", background: isConnected ? "var(--green)" : "var(--amber)", boxShadow: isConnected ? "0 0 0 2px #BBF7D0" : "none" }}/>
            <span style={{ fontSize: 10, color: "var(--ink-2)", fontFamily: "var(--mono)", fontWeight: 500 }}>
              {clusterDisplayName}
            </span>
            {clusterConn && (
              <span style={{ fontSize: 9, color: "var(--ink-4)", fontFamily: "var(--mono)" }}>
                via {clusterMode}
              </span>
            )}
          </div>
        )}

        {!isSharedView && (
          <button onClick={clusterConn || sshCreds ? handleClusterDisconnect : () => setShowConnectScreen(true)}
            style={{ fontSize: 10, color: "var(--ink-4)", background: "none", border: "1px solid var(--rule)", borderRadius: 4, padding: "4px 8px", cursor: "pointer", fontWeight: 500 }}>
            Switch
          </button>
        )}

        {messages.length > 0 && (
          <div style={{ display: "flex", gap: 6 }}>
            <button onClick={handleShare} style={{ fontSize: 10, color: "var(--accent)", background: "var(--accent-bg)", border: "1px solid var(--accent-bd)", borderRadius: 4, padding: "4px 10px", cursor: "pointer", fontWeight: 500, position: "relative" }}>
              {shareToast ? "Copied!" : "Share"}
            </button>
            <button onClick={handleNewChat} style={{ fontSize: 10, color: "var(--ink-4)", background: "none", border: "1px solid var(--rule)", borderRadius: 4, padding: "4px 10px", cursor: "pointer", fontWeight: 500 }}>
              New chat
            </button>
          </div>
        )}
      </div>

      {/* ── Reconnect banner ── */}
      {pendingReconnect && !sshCreds && (
        <div style={{ padding: "8px 18px", background: "var(--accent-bg)", borderBottom: "1px solid var(--accent-bd)", display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
          <span style={{ color: "var(--accent)" }}>Previous SSH: {pendingReconnect.username}@{pendingReconnect.host}</span>
          <input type="password" placeholder="Password" id="reconnect-pw"
            style={{ background: "#FFFFFF", border: "1px solid var(--rule)", borderRadius: 4, padding: "4px 8px", color: "var(--ink)", fontSize: 11, outline: "none", width: 140 }}
            onKeyDown={e => { if (e.key === "Enter") { handleReconnect((e.target as HTMLInputElement).value); } }}/>
          <button onClick={() => { const el = document.getElementById("reconnect-pw") as HTMLInputElement; if (el) handleReconnect(el.value); }}
            style={{ fontSize: 10, color: "var(--accent)", background: "#FFFFFF", border: "1px solid var(--accent-bd)", borderRadius: 4, padding: "3px 8px", cursor: "pointer" }}>
            Reconnect
          </button>
          <button onClick={() => { setPendingReconnect(null); deleteSshTarget(sessionId); }}
            style={{ fontSize: 10, color: "var(--ink-4)", background: "none", border: "none", cursor: "pointer" }}>
            Dismiss
          </button>
        </div>
      )}

      {/* ── Thread ── */}
      <div style={{ flex: 1, overflowY: "auto", padding: "28px 22px", display: "flex", flexDirection: "column", gap: 22, background: "var(--paper)" }}>
        <div style={{ maxWidth: 720, width: "100%", margin: "0 auto", display: "flex", flexDirection: "column", gap: 22 }}>

          {!historyLoaded && (
            <div style={{ textAlign: "center", padding: 32, fontSize: 12, color: "var(--ink-4)" }}>Loading history…</div>
          )}

          {/* Shared session not found */}
          {isEmpty && isSharedView && (
            <div style={{ textAlign: "center", paddingTop: 80, animation: "springIn 0.5s ease both" }}>
              <div style={{ fontSize: 40, marginBottom: 12 }}>&#128269;</div>
              <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)", marginBottom: 6 }}>Session not found</h3>
              <p style={{ fontSize: 13, color: "var(--ink-3)", lineHeight: 1.6, maxWidth: 360, margin: "0 auto" }}>
                This shared investigation session doesn&apos;t exist or has expired.
                Ask the person who shared it to send a new link.
              </p>
            </div>
          )}

          {/* Welcome */}
          {isEmpty && !isSharedView && (
            <div style={{ textAlign: "center", paddingTop: 80, animation: "springIn 0.5s ease both" }}>
              <svg viewBox="0 0 48 48" width="52" height="52" style={{ margin: "0 auto 16px", display: "block" }}>
                <polygon points="24,2 43.2,13 43.2,35 24,46 4.8,35 4.8,13"
                  fill="none" stroke="var(--accent)" strokeWidth="1.2" opacity="0.35"/>
                <path d="M24,7 L25.7,19.5 L37,14.2 L27.5,24 L37,33.8 L25.7,28.5 L24,41 L22.3,28.5 L11,33.8 L20.5,24 L11,14.2 L22.3,19.5 Z"
                  fill="var(--accent)"/>
              </svg>
              <div style={{ fontSize: 17, fontWeight: 600, color: "var(--ink-2)", marginBottom: 6 }}>Astra is ready</div>
              <div style={{ fontSize: 13, color: "var(--ink-4)" }}>Ask anything about your cluster</div>
            </div>
          )}

          {/* Messages */}
          {messages.map((m) => {
            // Thumbs UI helper — rendered below assistant bubbles when the
            // backend returned a capture_id (i.e. the session was classified
            // worthy per blueprint §9). After the user has voted, the
            // buttons are replaced by a one-line success hint.
            const renderFeedback = () => {
              if (m.role !== "assistant" || !m.captureId) return null;
              if (m.feedbackSent === "up") {
                return (
                  <div style={{ fontSize: 11, color: "var(--ink-3)", marginTop: 8 }}>
                    ✓ saved as runbook
                  </div>
                );
              }
              if (m.feedbackSent === "down") {
                return (
                  <div style={{ fontSize: 11, color: "var(--ink-3)", marginTop: 8 }}>
                    ✓ thanks — won&apos;t show this answer again
                  </div>
                );
              }
              return (
                <div style={{ display: "flex", alignItems: "center", gap: 8,
                              marginTop: 10, fontSize: 11, color: "var(--ink-3)" }}>
                  <span>Was this helpful?</span>
                  <button
                    onClick={() => handleFeedback(m.captureId!, "up", m.id)}
                    title="Save this answer as a verified runbook"
                    style={FEEDBACK_BTN}
                  >👍</button>
                  <button
                    onClick={() => handleFeedback(m.captureId!, "down", m.id)}
                    title="Don't surface this answer in future searches"
                    style={FEEDBACK_BTN}
                  >👎</button>
                </div>
              );
            };

            if (m.role === "user") {
              return <UserMessage key={m.id} text={m.text} time={formatTime()}/>;
            }

            // Assistant message
            if (m.loading) {
              return (
                <AstraMessage key={m.id} time={formatTime()} text="Investigating across your cluster…">
                  {reasoningSteps.length > 0 && <ReasoningBubble steps={reasoningSteps} thinking={isThinking}/>}
                </AstraMessage>
              );
            }

            // Investigation result → RootCauseCard
            if (isInvestigation(m.tool) && m.result) {
              const rc = extractRootCause(m.result);
              const actions = (m.suggestedActions || []).map(a => ({ label: a.label, command: a.command }));
              const alreadyExecuted = executedMsgIds.has(m.id);
              return (
                <AstraMessage key={m.id} time={formatTime()} text={m.text ? undefined : "Investigation complete."}>
                  {reasoningSteps.length > 0 && <ReasoningBubble steps={reasoningSteps}/>}
                  <div style={{ marginTop: 10 }}>
                    <RootCauseCard
                      severity={rc.severity}
                      title={rc.title}
                      pod={rc.pod}
                      namespace={rc.namespace}
                      summary={rc.summary || m.text}
                      metrics={rc.metrics}
                      evidence={rc.evidence}
                      onReviewExecute={actions.length > 0 && !alreadyExecuted ? () => handleReviewExecute(actions, rc.pod, rc.namespace, m.id) : undefined}
                      manualSteps={actions.length === 0 ? rc.steps : []}
                      prevention={actions.length === 0 ? rc.prevention : ""}
                    />
                  </div>
                  {renderFeedback()}
                </AstraMessage>
              );
            }

            // Regular result card
            if (m.result && m.tool && m.tool !== "none") {
              // Extract a clean summary from the AI text — strip markdown tables,
              // pipe-delimited data, and raw listings that the ResultCard renders better.
              const cleanSummary = m.text ? extractSummaryText(m.text) : "";

              return (
                <AstraMessage key={m.id} time={formatTime()}>
                  {/* Short AI insight line — only if it adds value beyond the card */}
                  {cleanSummary && (
                    <div style={{
                      fontSize: 13,
                      color: "var(--ink-2)",
                      lineHeight: 1.6,
                      marginBottom: 10,
                    }}>
                      <ReactMarkdown>{cleanSummary}</ReactMarkdown>
                    </div>
                  )}
                  {/* ResultCard is always the primary display */}
                  <ResultCard tool={m.tool} result={m.result}/>
                  {m.error && (
                    <div style={{ fontSize: 11, color: "var(--red)", marginTop: 8, padding: "8px 12px", background: "rgba(239,68,68,0.06)", border: "1px solid rgba(239,68,68,0.2)", borderRadius: 6, display: "flex", alignItems: "center", gap: 6 }}>
                      <span>⚠</span> {m.error}
                    </div>
                  )}
                  {renderFeedback()}
                </AstraMessage>
              );
            }

            // Plain text
            return (
              <AstraMessage key={m.id} time={formatTime()}>
                <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.6 }}>
                  <ReactMarkdown>{m.text}</ReactMarkdown>
                </div>
                {m.error && <div style={{ fontSize: 11, color: "var(--red)", marginTop: 4 }}>{m.error}</div>}
                {renderFeedback()}
              </AstraMessage>
            );
          })}

          <div ref={bottomRef}/>
        </div>
      </div>

      {/* ── Intent bar ── */}
      <IntentBar onSend={submit} listening={loading} clusterName={clusterDisplayName}/>

      {/* ── Approval overlay ── */}
      {showOverlay && (
        <ApprovalOverlay
          onClose={() => setShowOverlay(false)}
          onConfirm={handleExecuteConfirm}
          actions={overlayActions}
          targetResource={overlayResource}
          namespace={overlayNamespace}
        />
      )}
    </div>
  );
}
