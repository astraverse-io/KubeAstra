"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import { SlideToConfirm } from "../../components/SlideToConfirm";
import AccountSettings from "../../components/AccountSettings";
import ResultCard from "../../components/ResultCard";
import ClusterConnect from "../../components/ClusterConnect";
import SetupNotice from "../../components/SetupNotice";
import TargetBar from "../../components/TargetBar";
import HeaderOverflow, { type OverflowItem } from "../../components/HeaderOverflow";
import { SessionSidebar } from "../../components/SessionSidebar";
import { IntentBar } from "../../components/IntentBar";
import { copyToClipboard } from "../../lib/clipboard";
import { InvestigationTrail, ReactStep } from "../../components/InvestigationTrail";
import { RootCauseCard } from "../../components/RootCauseCard";
import { ApprovalOverlay } from "../../components/ApprovalOverlay";
import { CostBreakdownOverlay } from "../../components/CostBreakdownOverlay";
import { YamlProposer } from "../../components/YamlProposer";
import { SuggestedActions, firstExecutableAction, type SuggestedAction } from "../../components/SuggestedActions";
import { AstraGlyph } from "../../components/AstraGlyph";
import { MissionControlHeader } from "../../components/MissionControlHeader";
import HeaderLiveCounters from "../../components/HeaderLiveCounters";
import { MissionControlLeftRail } from "../../components/MissionControlLeftRail";
import { MissionControlDiagnosis } from "../../components/MissionControlDiagnosis";
import { MissionControlApprovalOverlay } from "../../components/MissionControlApprovalOverlay";
import { MissionControlToolTrail } from "../../components/MissionControlToolTrail";
import { CommandBar } from "../../components/CommandBar";
import { resultToMissionControlDiagnosis } from "../../lib/missionControlAdapters";
import {
  sendChat,
  sendChatStream,
  sendFeedback,
  getAuthStatus,
  login,
  signup,
  logout,
  listChatSessions,
  createChatSession,
  deleteChatSession,
  type ChatStreamEvent,
  executeCommand,
  checkHealth,
  getHistory,
  getHistoryDetail,
  clearHistory,
  exportPostMortem,
  getSshTarget,
  saveSshTarget,
  deleteSshTarget,
  clusterStatus,
  ApiError,
  type ChatMessage,
  type ClusterStatus,
  type SSHCredentials,
  type SSHTarget,
  type HistoryMessage,
  type AuthStatus,
  type ChatSession,
  type SessionAccessMode,
  triggerManualInvestigation,
  appendSessionMessages,
  fetchDesktopSetup,
  type DesktopSetupState,
} from "../../lib/api";
import FirstRunWizard from "../../components/FirstRunWizard";

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
  suggestedActions?: SuggestedAction[];
  executionResult?: { success: boolean; output?: string; error?: string };
  reactSteps?: ReactStep[];
  // Phase 1.3 — capture_id from the response payload. Drives the
  // thumbs-up/down feedback UI.
  captureId?: string;
  feedbackSent?: "up" | "down" | null;
  runId?: string | null;
  costSummary?: {
    total_cost_usd: number;
    total_tokens_in: number;
    total_tokens_out: number;
    total_cached_tokens_in: number;
    model: string;
  } | null;
}

type SessionViewMode = SessionAccessMode | "denied";

const PENDING_SHARED_SESSION_KEY = "k8s_pending_shared_session_id";



interface HealthCheck {
  status: "ok" | "degraded" | "failed";
  duration_ms?: number;
  detail?: string | null;
}

interface Health {
  ai_enabled: boolean;
  llm_provider?: string;
  kubectl_available: boolean;
  kubectl_context?: string | null;
  kubectl_mode?: "in_cluster" | "kubeconfig" | "unavailable";
  status?: "ok" | "degraded";
  checks?: Record<string, HealthCheck>;
}

/* ── example prompts ─────────────────────────────────────────── */

const EXAMPLES = [
  "My pod is stuck in CrashLoopBackOff — paste the error here",
  "List all pods in the production namespace",
  "Investigate pod api-service-7d4f9b in namespace default",
  "Show me recent events in the staging namespace",
  "What clusters do I have configured?",
  "Generate a runbook for OOMKilled errors",
];

/* ── helpers ─────────────────────────────────────────────────── */

// React keys only. Never use this for anything a URL exposes or an access
// check consults — see randomSessionId below for why. Deliberately kept
// distinct rather than pointed at the CSPRNG, so the next reader has to make
// the same choice rather than inherit it by accident.
function uid() {
  return Math.random().toString(36).slice(2);
}

// A session id is not a display detail: /chat/:sessionId is a shareable URL,
// so anyone who can guess an id can read that investigation — pod names, log
// excerpts, the cluster's shape. Math.random() is seeded from the clock and
// yields roughly 52 bits of predictable state, which is a guess away, not a
// search away. Only a CSPRNG belongs here.
function randomSessionId(): string {
  // Bound once and probed with optional calls: `"x" in crypto` narrows the
  // type to `never` after the first branch returns, because Crypto always
  // declares randomUUID even where the runtime does not provide it.
  const source = typeof crypto !== "undefined" ? crypto : undefined;
  if (source?.randomUUID) {
    return source.randomUUID();
  }
  if (source?.getRandomValues) {
    const bytes = new Uint8Array(16);
    source.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  // No CSPRNG at all: refuse rather than quietly issue a guessable id. Every
  // browser this app supports has one, so reaching here means something is
  // wrong that a weak fallback would only hide.
  throw new Error("This browser has no secure random source; cannot start a session.");
}

function getOrCreateSessionId(): string {
  if (typeof window === "undefined") return randomSessionId();
  let sid = localStorage.getItem("k8s_session_id");
  if (!sid) {
    // Was `crypto.randomUUID() ?? uid() + uid()`. The fallback is the whole
    // problem: it fires exactly where the CSPRNG is missing, so the weakest
    // ids were issued in the situations least able to tolerate them.
    sid = randomSessionId();
    localStorage.setItem("k8s_session_id", sid);
  }
  return sid;
}

function unwrapHistoryResult(result?: Record<string, unknown>) {
  if (!result) return { result: null, reactSteps: undefined };
  const toolResult = result.tool_result;
  const steps = result.react_steps;
  const captureId = typeof result.capture_id === "string"
    ? result.capture_id
    : isRecord(toolResult) && typeof toolResult.capture_id === "string"
      ? toolResult.capture_id
      : undefined;
  return {
    result: isRecord(toolResult) ? toolResult : result,
    reactSteps: Array.isArray(steps) ? steps as ReactStep[] : undefined,
    captureId,
  };
}

function formatCost(usd: number): string {
  if (usd === 0) return "$0.00";
  if (usd < 0.0001) return `$${usd.toFixed(6)}`;
  if (usd < 0.001) return `$${usd.toFixed(5)}`;
  return `$${usd.toFixed(4)}`;
}

function formatTokens(count: number): string {
  if (count < 1000) return `${count}`;
  return `${(count / 1000).toFixed(1)}k`;
}

function historyToMessages(history: HistoryMessage[]): Message[] {
  return history.map((h) => ({
    ...(() => {
      const unwrapped = unwrapHistoryResult(h.result);
      const costSummary = h.result && typeof h.result === "object" ? (h.result.cost_summary as any) : undefined;
      const runId = h.result && typeof h.result === "object" ? (h.result.run_id as string) : undefined;
      return {
        id: uid(),
        role: h.role,
        text: h.content,
        tool: h.tool_used,
        result: unwrapped.result,
        reactSteps: unwrapped.reactSteps,
        captureId: unwrapped.captureId,
        error: h.error ?? null,
        runId,
        costSummary,
      };
    })(),
  }));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractRootCause(result?: Record<string, unknown> | null) {
  if (!result) return null;
  const ai = result.ai;
  if (!isRecord(ai)) return null;
  const analysis = ai.ai_analysis;
  if (!isRecord(analysis)) return null;
  return {
    rootCause: String(analysis.root_cause ?? ""),
    solution: String(analysis.solution ?? ""),
    severity: String(analysis.severity ?? ""),
    confidence: analysis.confidence,
  };
}



function MarkdownMessage({ text, onApplyYaml }: { text: string; onApplyYaml?: (yaml: string) => void }) {
  // Let's rely on standard markdown rendering but ensure no margin on p tags in our global CSS.
  // We'll leave the generic prose class or rely on custom CSS for `.markdown-body`
  return (
    <div className="markdown-body">
      <ReactMarkdown
        components={{
          code(props) {
            const {children, className, node, ...rest} = props
            const match = /language-(\w+)/.exec(className || '')
            if (match && match[1] === 'yaml' && typeof children === 'string' && children.startsWith('# patch:apply\n')) {
              if (onApplyYaml) {
                 const yamlContent = children.replace('# patch:apply\n', '');
                 return <YamlProposer yamlText={yamlContent} onApply={onApplyYaml} />
              }
            }
            return <code className={className} {...rest}>{children}</code>
          }
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/* ── KubeAstra logo components ────────────────────────────────── */

/** Circular star emblem — KubeAstra icon mark (kube = cluster, astra = star) */
function KubeAstraEmblem({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="22" cy="22" r="22" fill="var(--brand)" />
      {/* Four-point star — clean, geometric, astra */}
      <path
        d="M22 8 L24.2 19.8 L36 22 L24.2 24.2 L22 36 L19.8 24.2 L8 22 L19.8 19.8 Z"
        fill="#0a0a0a"
      />
    </svg>
  );
}

/** Full KubeAstra wordmark: emblem + "Kube" ink + "Astra" brand-cyan */

/* ── SSH reconnect banner ────────────────────────────────────── */

interface ReconnectBannerProps {
  target: SSHTarget;
  onReconnect: (password: string) => void;
  onDismiss: () => void;
}
// Removed ReconnectBanner from page.tsx to use imported version if needed, wait, is it exported from somewhere?
// Actually let's just remove the import and keep the local function, that's easier.
function ReconnectBanner({ target, onReconnect, onDismiss }: ReconnectBannerProps) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const handleReconnect = async () => {
    if (!password) return;
    setBusy(true);
    onReconnect(password);
  };

  return (
    <div
      style={{ flexShrink: 0, borderBottom: "1px solid var(--brand-bd)", padding: "0.75rem 1rem", background: "var(--brand-bg)" }}
    >
      <div style={{ maxWidth: "48rem", margin: "0 auto", display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.875rem", flex: 1, minWidth: 0, color: "var(--brand)" }}>
          <span style={{ width: "0.5rem", height: "0.5rem", borderRadius: "50%", flexShrink: 0, background: "var(--brand)" }} />
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            Previous SSH session:{" "}
            <span style={{ fontFamily: "var(--mono)", fontWeight: 500 }}>{target.username}@{target.host}</span>
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <input
            name="ssh-reconnect-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleReconnect()}
            placeholder="Password to reconnect"
            autoFocus
            className="app-input"
            style={{ borderRadius: "0.5rem", padding: "0.375rem 0.75rem", fontSize: "0.875rem", width: "11rem" }}
          />
          <button
            onClick={handleReconnect}
            disabled={!password || busy}
            className="app-btn-primary"
            style={{ padding: "0.375rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.875rem", fontWeight: 500 }}
          >
            {busy ? "Connecting…" : "Reconnect"}
          </button>
          <button
            onClick={onDismiss}
            className="app-btn-ghost"
            style={{ padding: "0.375rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.875rem" }}
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

function AuthPanel({ status, onAuthenticated }: { status: AuthStatus; onAuthenticated: (status: AuthStatus) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const canSignup = status.allow_signup;

  const submitAuth = async () => {
    if (!username.trim() || !password) return;
    setBusy(true);
    setError("");
    try {
      const next = mode === "signup"
        ? await signup(username.trim(), password, displayName.trim() || undefined, email.trim() || undefined)
        : await login(username.trim(), password);
      onAuthenticated(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--paper)", color: "var(--ink)", padding: "1rem" }}>
      <div style={{ width: "100%", maxWidth: "26rem", borderRadius: "1rem", padding: "1.5rem", background: "var(--paper-2)", border: "1px solid var(--rule)", boxShadow: "0 20px 60px rgba(0,0,0,0.25)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "1.25rem" }}>
          <KubeAstraEmblem size={36} />
          <div>
            <h1 style={{ margin: 0, fontSize: "1rem", fontWeight: 700 }}>KubeAstra Assistant</h1>
            <p style={{ margin: "0.125rem 0 0 0", color: "var(--ink-3)", fontSize: "0.75rem" }}>Sign in to load your chat history</p>
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
            Username
            <input name="auth-username" className="app-input" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
          </label>
          {mode === "signup" && (
            <>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
                Display name
                <input name="auth-display-name" className="app-input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
                Email <span style={{ color: "var(--ink-4)" }}>(optional, for password reset)</span>
                <input className="app-input" type="email" value={email} placeholder="you@example.com" onChange={(e) => setEmail(e.target.value)} />
              </label>
            </>
          )}
          <label style={{ display: "flex", flexDirection: "column", gap: "0.25rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
            Password
            <input
              name="auth-password"
              className="app-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitAuth();
              }}
            />
          </label>
          {error && <p style={{ color: "var(--danger)", fontSize: "0.75rem", margin: 0 }}>{error}</p>}
          <button
            className="app-btn-primary"
            disabled={busy || !username.trim() || !password}
            onClick={submitAuth}
            style={{ padding: "0.625rem 0.75rem", borderRadius: "0.75rem", fontWeight: 600, opacity: busy ? 0.6 : 1 }}
          >
            {busy ? "Please wait..." : mode === "signup" ? "Create account" : "Sign in"}
          </button>
          {canSignup && (
            <button
              className="app-btn-ghost"
              onClick={() => {
                setError("");
                setMode(mode === "signup" ? "login" : "signup");
              }}
              style={{ fontSize: "0.75rem" }}
            >
              {mode === "signup" ? "Already have an account? Sign in" : "Need an account? Sign up"}
            </button>
          )}
          {mode === "login" && (
            <Link
              href="/forgot-password"
              style={{ fontSize: "0.75rem", color: "var(--ink-3)", textAlign: "center", textDecoration: "none" }}
            >
              Forgot password?
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── SSH panel ───────────────────────────────────────────────── */

interface SSHPanelProps {
  sessionId: string;
  onConnect: (creds: SSHCredentials) => void;
  onDisconnect: () => void;
  connected: SSHCredentials | null;
  isOpen?: boolean;
  onToggle?: (open: boolean) => void;
}

function SSHPanel({ sessionId, onConnect, onDisconnect, connected, isOpen, onToggle }: SSHPanelProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = isOpen !== undefined ? isOpen : internalOpen;
  const setOpen = onToggle !== undefined ? onToggle : setInternalOpen;

  const [host, setHost] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [port, setPort] = useState("22");
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "ok" | "err">("idle");
  const [testError, setTestError] = useState("");

  const handleConnect = async () => {
    if (!host.trim() || !username.trim() || !password) return;
    const creds: SSHCredentials = {
      host: host.trim(),
      username: username.trim(),
      password,
      port: parseInt(port, 10) || 22,
    };

    setTestStatus("testing");
    setTestError("");
    try {
      const res = await sendChat("list clusters", [], creds, sessionId);
      if (res.error && res.tool_used === "error") {
        setTestStatus("err");
        setTestError(res.error);
        return;
      }
      setTestStatus("ok");
      onConnect(creds);
      setOpen(false);
    } catch (e) {
      setTestStatus("err");
      setTestError(String(e));
    }
  };

  const handleDisconnect = () => {
    onDisconnect();
    setTestStatus("idle");
    setTestError("");
    setPassword("");
  };

  if (connected) {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.75rem" }}>
        <span
          style={{ display: "flex", alignItems: "center", gap: "0.375rem", padding: "0.25rem 0.625rem", borderRadius: "0.5rem", border: "1px solid var(--brand-bd)", fontSize: "0.75rem", fontWeight: 500, background: "var(--brand-bg)", color: "var(--brand)" }}
        >
          <span style={{ width: "0.375rem", height: "0.375rem", borderRadius: "50%", background: "var(--brand)", animation: "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite" }} />
          SSH: {connected.username}@{connected.host}
        </span>
        <button
          onClick={handleDisconnect}
          className="app-btn-ghost"
          style={{ padding: "0.25rem 0.625rem", borderRadius: "0.5rem", fontSize: "0.75rem" }}
        >
          Disconnect
        </button>
      </div>
    );
  }

  return (
    <div style={{ position: "relative" }}>
      {/* No trigger of its own. SSH is a *way to reach* a cluster, not a peer
          of "choose a cluster", so it is opened from inside the target
          popover. Rendering a second top-level button here is what produced
          two controls for one decision. */}
      {open && (
        <div
          style={{ position: "absolute", left: 0, top: "calc(100% + 0.5rem)", zIndex: 50, width: "20rem", borderRadius: "1rem", boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)", padding: "1rem", display: "flex", flexDirection: "column", gap: "0.75rem", background: "var(--paper-2)", border: "1px solid var(--rule)" }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h3 style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--ink)", margin: 0 }}>
              Aim at a cluster over SSH
            </h3>
            <button
              onClick={() => setOpen(false)}
              style={{ fontSize: "1.125rem", lineHeight: 1, transition: "color 0.15s", color: "var(--ink-3)", background: "none", border: "none", cursor: "pointer", padding: 0 }}
            >
              &times;
            </button>
          </div>

          <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", margin: 0 }}>
            SSH into a kubeadm master node. All kubectl commands will run remotely for this session.
          </p>

          <div style={{ display: "flex", gap: "0.5rem" }}>
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: "0.25rem" }}>
              <label style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>Hostname / IP</label>
              <input name="ssh-host" type="text" value={host} onChange={(e) => setHost(e.target.value)}
                placeholder="10.0.1.5" className="app-input" style={{ width: "100%", boxSizing: "border-box" }} />
            </div>
            <div style={{ width: "4rem", display: "flex", flexDirection: "column", gap: "0.25rem" }}>
              <label style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>Port</label>
              <input name="ssh-port" type="number" value={port} onChange={(e) => setPort(e.target.value)}
                className="app-input" style={{ width: "100%", boxSizing: "border-box" }} />
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <label style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>Username</label>
            <input name="ssh-username" type="text" value={username} onChange={(e) => setUsername(e.target.value)}
              placeholder="ansible" className="app-input" style={{ width: "100%", boxSizing: "border-box" }} />
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            <label style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>Password</label>
            <input name="ssh-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••" className="app-input" style={{ width: "100%", boxSizing: "border-box" }} />
          </div>

          {testStatus === "err" && (
            <p style={{ fontSize: "0.75rem", borderRadius: "0.5rem", padding: "0.5rem 0.75rem", margin: 0, color: "var(--danger)", background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)" }}>
              {testError || "Connection failed"}
            </p>
          )}

          <button
            onClick={handleConnect}
            disabled={!host.trim() || !username.trim() || !password || testStatus === "testing"}
            className="app-btn-primary"
            style={{ marginTop: "0.25rem", width: "100%", padding: "0.5rem 0", borderRadius: "0.75rem", fontSize: "0.875rem", fontWeight: 500, display: "flex", alignItems: "center", justifyContent: "center", gap: "0.5rem" }}
          >
            {testStatus === "testing" ? (
              <>
                <span style={{ width: "0.875rem", height: "0.875rem", border: "2px solid rgba(255,255,255,0.3)", borderTopColor: "white", borderRadius: "50%", animation: "spin 1s linear infinite" }} />
                Testing connection…
              </>
            ) : "Connect & Test"}
          </button>
        </div>
      )}
    </div>
  );
}

/* ── main component ──────────────────────────────────────────── */

export default function ChatPage() {
  const [sessionId, setSessionId] = useState<string>(() => getOrCreateSessionId());
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [authLoaded, setAuthLoaded] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthLoaded, setHealthLoaded] = useState(false);
  const [sshCreds, setSshCreds] = useState<SSHCredentials | null>(null);
  const [clusterConn, setClusterConn] = useState<ClusterStatus | null>(null);
  const [pendingReconnect, setPendingReconnect] = useState<SSHTarget | null>(null);
  const [activePopover, setActivePopover] = useState<"none" | "cluster" | "ssh">("none");
  const [overflowOpen, setOverflowOpen] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [sessionAccessMode, setSessionAccessMode] = useState<SessionViewMode>("owned");
  const [sharedSessionMeta, setSharedSessionMeta] = useState<{ owner?: string | null; title?: string | null }>({});
  const [sharedAccessError, setSharedAccessError] = useState<string | null>(null);
  // edit state
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const [exportingPM, setExportingPM] = useState(false);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);
  const [shareCopied, setShareCopied] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<{ messageId: string; action: SuggestedAction } | null>(null);
  // Read the theme synchronously from the DOM on first render. The inline
  // script in layout.tsx sets data-theme from localStorage before hydration,
  // so this returns the correct value on the client. On the server document
  // is undefined and we fall back to "dark". suppressHydrationWarning on
  // <html> silences the resulting mismatch — it's intentional.
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    if (typeof document === "undefined") return "dark";
    const attr = document.documentElement.getAttribute("data-theme");
    // Legacy "mission-control" migrates to dark — the aesthetic is baked
    // into dark now.
    if (attr === "mission-control") return "dark";
    if (attr === "light" || attr === "dark") return attr;
    return "dark";
  });
  const [costBreakdownRunId, setCostBreakdownRunId] = useState<string | null>(null);
  // `mounted` starts true on the client (theme is already correct via the
  // lazy initializer + layout script), so isMissionControl is right from
  // paint 1. On the server it stays false so we don't render mission-control
  // markup that would mismatch on hydration.
  const [mounted, setMounted] = useState(() => typeof document !== "undefined");
  // null while unknown or in server mode; a state object in desktop mode.
  const [desktopSetup, setDesktopSetup] = useState<DesktopSetupState | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const authIsEnabled = Boolean(authStatus?.auth_enabled);
  const currentUser = authStatus?.user ?? null;
  const isOwnedSession = sessionAccessMode === "owned";
  const isReadonlySharedSession = sessionAccessMode === "admin_readonly";
  const isDeniedSharedSession = sessionAccessMode === "denied";

  const resetSharedViewState = useCallback(() => {
    if (typeof window !== "undefined") {
      sessionStorage.removeItem(PENDING_SHARED_SESSION_KEY);
    }
    setSessionAccessMode("owned");
    setSharedSessionMeta({});
    setSharedAccessError(null);
  }, []);

  const loadAccountSessions = useCallback(async () => {
    resetSharedViewState();
    const remoteSessions = await listChatSessions();
    if (remoteSessions.length > 0) {
      setSessions(remoteSessions);
      setSessionId(remoteSessions[0].id);
      setMessages([]);
      setHistoryLoaded(false);
      return;
    }
    const created = await createChatSession();
    setSessions([created]);
    setSessionId(created.id);
    setMessages([]);
    setHistoryLoaded(true);
  }, [resetSharedViewState]);

  const loadPendingSharedSession = useCallback(async (status: AuthStatus): Promise<boolean> => {
    if (typeof window === "undefined") return false;
    const pendingSessionId = sessionStorage.getItem(PENDING_SHARED_SESSION_KEY);
    if (!pendingSessionId) return false;

    setHistoryLoaded(false);
    setSharedAccessError(null);
    setSshCreds(null);
    setClusterConn(null);
    setPendingReconnect(null);
    setActivePopover("none");

    try {
      const detail = await getHistoryDetail(pendingSessionId);
      sessionStorage.removeItem(PENDING_SHARED_SESSION_KEY);
      setSessionId(detail.session_id);
      setMessages(historyToMessages(detail.messages));
      setSessionAccessMode(detail.access_mode);
      setSharedSessionMeta({
        owner: detail.owner_display_name || detail.owner_username || null,
        title: detail.title,
      });
      setHistoryLoaded(true);
      if (status.auth_enabled && status.user) {
        try {
          setSessions(await listChatSessions());
        } catch {
          setSessions([]);
        }
      }
      return true;
    } catch (err) {
      const statusCode = err instanceof ApiError ? err.status : 0;
      const message = statusCode === 401
        ? "Please log in to view this shared chat."
        : "You do not have access to this shared chat. Ask an admin or the owner for access.";
      sessionStorage.removeItem(PENDING_SHARED_SESSION_KEY);
      setSessionId(pendingSessionId);
      setMessages([]);
      setSessionAccessMode("denied");
      setSharedSessionMeta({});
      setSharedAccessError(message);
      setHistoryLoaded(true);
      if (status.auth_enabled && status.user) {
        try {
          setSessions(await listChatSessions());
        } catch {
          setSessions([]);
        }
      }
      return true;
    }
  }, []);

  const handleAuthenticated = useCallback(async (status: AuthStatus) => {
    setAuthStatus(status);
    if (status.auth_enabled && status.user) {
      const handledShared = await loadPendingSharedSession(status);
      if (!handledShared) {
        await loadAccountSessions();
      }
    }
  }, [loadAccountSessions, loadPendingSharedSession]);

  const handleStop = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  useEffect(() => {
    // Belt-and-suspenders: if for some reason (JS disabled, script blocked)
    // the layout init script didn't run, mounted may still be false — force
    // it here. Theme was already read from data-theme in the state initializer.
    setMounted(true);
  }, []);

  useEffect(() => {
    // Shared-session links arrive as /chat?session=<id>. The older
    // /chat/<id> route still exists in server builds and redirects here, but
    // it cannot exist in the desktop static export (a dynamic route needs
    // generateStaticParams, and session ids are not knowable at build time).
    // Carrying the id in the query string means one static page serves both.
    //
    // Runs before the auth effect below, so the id is in sessionStorage by
    // the time loadPendingSharedSession looks for it. Reading
    // window.location directly rather than useSearchParams keeps this out of
    // a Suspense boundary, which static export would otherwise require.
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const shared = params.get("session");
    if (!shared) return;

    sessionStorage.setItem(PENDING_SHARED_SESSION_KEY, shared);
    // Drop the param so a refresh doesn't re-enter the shared-view flow after
    // the user has navigated on. Matches what the old redirect route did.
    params.delete("session");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}`,
    );
  }, []);

  useEffect(() => {
    // Desktop mode only: /api/desktop/* is absent in server deployments, and
    // fetchDesktopSetup returns null on 404 rather than throwing, so this is
    // a no-op there.
    let cancelled = false;
    fetchDesktopSetup()
      .then((state) => {
        if (!cancelled) setDesktopSetup(state);
      })
      .catch(() => {
        // Never block the app on setup state; the wizard just won't appear.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAuthStatus()
      .then(async (status) => {
        if (cancelled) return;
        setAuthStatus(status);
        setAuthLoaded(true);
        if (status.auth_enabled && status.user) {
          const handledShared = await loadPendingSharedSession(status);
          if (!handledShared) {
            await loadAccountSessions();
          }
        } else if (!status.auth_enabled && typeof window !== "undefined") {
          try {
            const stored = JSON.parse(localStorage.getItem("k8s_sessions") || "[]");
            setSessions(stored);
          } catch {
            setSessions([]);
          }
        } else {
          setHistoryLoaded(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAuthStatus({ auth_enabled: false, allow_signup: false, user: null });
          setAuthLoaded(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [loadAccountSessions, loadPendingSharedSession]);

  useEffect(() => {
    if (mounted) {
      document.documentElement.setAttribute("data-theme", theme);
      localStorage.setItem("theme", theme);
    }
  }, [theme, mounted]);

  useEffect(() => {
    if (!authLoaded) return;
    if (authIsEnabled && !currentUser) return;
    if (typeof window !== "undefined" && sessionStorage.getItem(PENDING_SHARED_SESSION_KEY)) return;
    checkHealth().then((h) => {
      if (h) setHealth(h as Health);
      setHealthLoaded(true);
    });
    if (!isOwnedSession) {
      setHistoryLoaded(true);
      return;
    }
    getHistory(sessionId).then((history) => {
      setMessages(historyToMessages(history));
      setHistoryLoaded(true);
    });
    getSshTarget(sessionId).then((target) => {
      if (target) setPendingReconnect(target);
    });
    clusterStatus(sessionId).then((status) => {
      if (status.connected) setClusterConn(status);
    }).catch(() => {
      // Cluster connection is optional; ignore status fetch failures on load.
    });
  }, [sessionId, authLoaded, authIsEnabled, currentUser, isOwnedSession]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const autoResize = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  };

  const handleConnect = useCallback((creds: SSHCredentials) => {
    if (!isOwnedSession) return;
    setSshCreds(creds);
    setPendingReconnect(null);
    saveSshTarget(sessionId, { host: creds.host, username: creds.username, port: creds.port ?? 22 });
    setMessages((prev) => [
      ...prev,
      { id: uid(), role: "assistant", text: `Connected to **${creds.username}@${creds.host}** via SSH. All kubectl commands will now run on that cluster.` },
    ]);
  }, [isOwnedSession, sessionId]);

  const handleDisconnect = useCallback(() => {
    if (!isOwnedSession) return;
    setSshCreds(null);
    deleteSshTarget(sessionId);
    setMessages((prev) => [
      ...prev,
      { id: uid(), role: "assistant", text: "SSH session closed. Reverting to local cluster." },
    ]);
  }, [isOwnedSession, sessionId]);

  const handleReconnectFromBanner = useCallback(async (password: string) => {
    if (!isOwnedSession) return;
    if (!pendingReconnect) return;
    const creds: SSHCredentials = { ...pendingReconnect, password };
    try {
      const res = await sendChat("list clusters", [], creds, sessionId);
      if (res.error && res.tool_used === "error") {
        deleteSshTarget(sessionId);
        setPendingReconnect(null);
        return;
      }
      setSshCreds(creds);
      setPendingReconnect(null);
      setMessages((prev) => [
        ...prev,
        { id: uid(), role: "assistant", text: `Reconnected to **${creds.username}@${creds.host}** via SSH.` },
      ]);
    } catch {
      deleteSshTarget(sessionId);
      setPendingReconnect(null);
    }
  }, [isOwnedSession, pendingReconnect, sessionId]);

  const submit = useCallback(async (text: string, historySource?: Message[]) => {
    if (!text.trim() || loading || !isOwnedSession) return;

    if (messages.length === 0 && !historySource) {
      const title = text.trim().slice(0, 60) + (text.trim().length > 60 ? "..." : "");
      setSessions((prev) => {
        if (authIsEnabled) {
          return prev.map((s) => s.id === sessionId ? { ...s, title, timestamp: Date.now() } : s);
        }
        if (prev.find(s => s.id === sessionId)) return prev;
        const next = [{ id: sessionId, title, timestamp: Date.now() }, ...prev];
        if (typeof window !== "undefined") localStorage.setItem("k8s_sessions", JSON.stringify(next));
        return next;
      });
    }

    const userMsg: Message = { id: uid(), role: "user", text: text.trim(), viaSSH: !!sshCreds };
    const thinkingMsg: Message = { id: uid(), role: "assistant", text: "", loading: true };
    const priorMessages = historySource ?? messages;

    const controller = new AbortController();
    abortControllerRef.current = controller;

    setMessages((prev) => [...prev, userMsg, thinkingMsg]);
    setInput("");
    setLoading(true);
    if (textareaRef.current) textareaRef.current.style.height = "auto";

    const history: ChatMessage[] = priorMessages
      .filter((m) => !m.loading)
      .slice(-10)
      .map((m) => ({ role: m.role, content: m.text }));

    const trimmedText = text.trim();
    if (trimmedText === "/rca" || trimmedText.startsWith("/rca ")) {
      const target = trimmedText.substring(4).trim();
      if (!target) {
        setMessages((prev) => [
          ...prev.slice(0, -1),
          { ...thinkingMsg, loading: false, text: "Please provide a resource target (e.g. `/rca pod/frontend`).", error: "Missing target" },
        ]);
        setLoading(false);
        return;
      }
      try {
        const result = await triggerManualInvestigation(target);
        const assistantText = `Triggered formal RCA playbook for \`${target}\`. Investigation ID: **${result.investigation_id}**.\n\nYou can view the live progress on the [/alerts](/alerts) dashboard.`;
        setMessages((prev) => [
          ...prev.slice(0, -1),
          { ...thinkingMsg, loading: false, text: assistantText },
        ]);
        // Persist the exchange to the session so it becomes first-class chat
        // history — appears in chronological order alongside regular turns and
        // survives any navigation. Best-effort: a failure here does not roll
        // back the user-facing message (the investigation itself already ran).
        try {
          await appendSessionMessages(sessionId, [
            { role: "user", content: text.trim() },
            { role: "assistant", content: assistantText },
          ]);
        } catch {
          // History persistence is best-effort; UI message still rendered.
        }
      } catch (err: unknown) {
        setMessages((prev) => [
          ...prev.slice(0, -1),
          {
            ...thinkingMsg,
            loading: false,
            text: `Failed to trigger RCA for \`${target}\`.`,
            error: err instanceof Error ? err.message : String(err),
          },
        ]);
      }
      setLoading(false);
      return;
    }

    // Track live ReAct steps per-iteration so step_complete can patch the
    // matching iteration_planned entry (duration, observation preview).
    const liveSteps: ReactStep[] = [];
    let streamedText = "";

    const pushStepsSnapshot = () => {
      // A fresh array reference is enough to trigger React reconciliation;
      // step objects are treated as immutable after push so deep-copying
      // each one was wasted work.
      const snapshot = liveSteps.slice();
      setMessages((prev) =>
        prev.map((m) =>
          m.id === thinkingMsg.id ? { ...m, reactSteps: snapshot } : m,
        ),
      );
    };

    const handleEvent = (evt: ChatStreamEvent) => {
      if (evt.type === "thought_stream") {
        if (liveSteps.length === 0 || liveSteps[liveSteps.length - 1].action !== "thinking...") {
          liveSteps.push({
            thought: evt.text || "",
            action: "thinking...",
            params: {},
          });
        } else {
          liveSteps[liveSteps.length - 1].thought += (evt.text || "");
        }
        pushStepsSnapshot();
      } else if (evt.type === "iteration_planned") {
        if (liveSteps.length > 0 && liveSteps[liveSteps.length - 1].action === "thinking...") {
          liveSteps[liveSteps.length - 1].thought = evt.thought;
          liveSteps[liveSteps.length - 1].action = evt.action ?? "";
          liveSteps[liveSteps.length - 1].params = evt.params;
        } else {
          liveSteps.push({
            thought: evt.thought,
            action: evt.action ?? "",
            params: evt.params,
          });
        }
        pushStepsSnapshot();
      } else if (evt.type === "step_complete") {
        // Patch the last step matching this iteration with duration_ms.
        for (let i = liveSteps.length - 1; i >= 0; i--) {
          if (liveSteps[i].action === evt.action) {
            liveSteps[i] = { ...liveSteps[i], duration_ms: evt.duration_ms };
            break;
          }
        }
        pushStepsSnapshot();
      } else if (evt.type === "answer_start") {
        // Flip out of "loading" so the message renders as a text bubble
        // instead of the loading indicator. reactSteps stays on the
        // message so the historical pills remain visible above the
        // streaming text. A "…" placeholder fills the brief gap before
        // the first token arrives so the bubble isn't visibly empty.
        setMessages((prev) =>
          prev.map((m) =>
            m.id === thinkingMsg.id ? { ...m, loading: false, text: "…" } : m,
          ),
        );
      } else if (evt.type === "token" && evt.text) {
        streamedText += evt.text;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === thinkingMsg.id ? { ...m, loading: false, text: streamedText } : m,
          ),
        );
      }
      // start / answer_end / done / error are handled below or ignored.
    };

    try {
      const res = await sendChatStream(
        text.trim(),
        history,
        sshCreds,
        sessionId,
        null,
        handleEvent,
        controller.signal,
      );
      if (res.session_id && res.session_id !== sessionId) {
        const nextSessionId = res.session_id;
        setSessionId(nextSessionId);
        if (!authIsEnabled && typeof window !== "undefined") {
          localStorage.setItem("k8s_session_id", nextSessionId);
        }
        setSessions((prev) => (
          prev.some((s) => s.id === nextSessionId)
            ? prev
            : [{ id: nextSessionId, title: text.trim().slice(0, 60) || "New investigation", timestamp: Date.now() }, ...prev]
        ));
      }
      const unwrapped = unwrapHistoryResult(res.result ?? undefined);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === thinkingMsg.id
            ? {
                ...m,
                loading: false,
                text: res.reply,
                tool: res.tool_used,
                result: unwrapped.result,
                // Prefer the server-authoritative steps from the final
                // response when present (includes the "answer" step + any
                // detail the live events omitted), otherwise keep what we
                // accumulated from streaming.
                reactSteps: unwrapped.reactSteps ?? liveSteps,
                captureId: typeof (res.result as Record<string, unknown> | null | undefined)?.capture_id === "string"
                  ? ((res.result as Record<string, unknown>).capture_id as string)
                  : undefined,
                error: res.error,
                suggestedActions: res.suggested_actions ?? [],
                runId: res.run_id,
                costSummary: res.cost_summary,
              }
            : m
        )
      );
    } catch (err) {
      const isAborted = err instanceof DOMException && err.name === "AbortError";
      const partial = streamedText;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === thinkingMsg.id
            ? {
                ...m,
                loading: false,
                text: isAborted
                  ? (partial ? `${partial}\n\n*[Stopped by user]*` : "*[Request stopped by user]*")
                  : (partial || "Failed to reach the backend. Is it running?"),
                error: isAborted ? undefined : String(err),
              }
            : m
        )
      );
    } finally {
      setLoading(false);
      abortControllerRef.current = null;
    }
  }, [authIsEnabled, isOwnedSession, loading, messages, sshCreds, sessionId]);

  const handleCopy = useCallback((text: string, idx: number) => {
    copyToClipboard(text).then(() => {
      setCopiedIdx(idx);
      setTimeout(() => setCopiedIdx(null), 1500);
    });
  }, []);

  const handleEditStart = useCallback((idx: number, text: string) => {
    setEditingIdx(idx);
    setEditText(text);
  }, []);

  const handleEditCancel = useCallback(() => {
    setEditingIdx(null);
    setEditText("");
  }, []);

  // Feedback: use captured RAG IDs when available, otherwise persist an
  // audit-only event keyed to the assistant message.
  const handleFeedback = useCallback(async (msgId: string, captureId: string | undefined, rating: "up" | "down") => {
    if (!isOwnedSession) return;
    const messageIndex = messages.findIndex((m) => m.id === msgId);
    const assistantMessage = messageIndex >= 0 ? messages[messageIndex] : undefined;
    const promptMessage = messageIndex >= 0
      ? [...messages.slice(0, messageIndex)].reverse().find((m) => m.role === "user")
      : undefined;

    setMessages((prev) =>
      prev.map((m) => (m.id === msgId ? { ...m, feedbackSent: rating } : m))
    );
    try {
      const feedbackId = captureId || `message:${sessionId}:${msgId}`;
      await sendFeedback(feedbackId, rating, {
        sessionId,
        reason: captureId ? undefined : "audit_only_no_capture",
        prompt: promptMessage?.text,
        response: assistantMessage?.text,
        toolUsed: assistantMessage?.tool,
      });
    } catch (err) {
      setMessages((prev) =>
        prev.map((m) => (m.id === msgId ? { ...m, feedbackSent: null } : m))
      );
      console.warn("feedback failed:", err);
    }
  }, [isOwnedSession, messages, sessionId]);

  const renderFeedbackControls = useCallback((m: Message, placement: "footer" | "inline" | "standalone" = "standalone") => {
    if (!isOwnedSession) return null;
    if (m.role !== "assistant" || m.loading || m.tool === "error" || !(m.text || m.result)) return null;
    const inline = placement === "inline";
    const footer = placement === "footer";

    return (
      <div style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        padding: inline ? "0.625rem 0 0 0" : footer ? 0 : "0 0.25rem",
        marginTop: inline ? "0.625rem" : footer ? 0 : "0.25rem",
        borderTop: inline ? "1px solid var(--rule)" : "none",
        flexWrap: "wrap",
      }}>
        {m.feedbackSent === "up" ? (
          <span style={{ fontSize: "0.75rem", color: "var(--success)" }}>
            {m.captureId ? "👍 Saved as a runbook — thanks" : "👍 Feedback saved — thanks"}
          </span>
        ) : m.feedbackSent === "down" ? (
          <span style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>
            {m.captureId ? "👎 Removed from KB" : "👎 Feedback saved"}
          </span>
        ) : (
          <>
            <span style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>
              Was this useful?
            </span>
            <button
              aria-label="Promote to runbook"
              title={m.captureId ? "Save this resolution as a runbook" : "Mark this answer as useful"}
              onClick={() => handleFeedback(m.id, m.captureId, "up")}
              style={{ width: "1.5rem", height: "1.5rem", borderRadius: "0.25rem", display: "flex", alignItems: "center", justifyContent: "center", transition: "color 0.15s", background: "var(--paper-3)", border: "1px solid var(--rule)", cursor: "pointer" }}
            >
              👍
            </button>
            <button
              aria-label="Quarantine this entry"
              title={m.captureId ? "The answer was wrong — remove from KB" : "Mark this answer as not useful"}
              onClick={() => handleFeedback(m.id, m.captureId, "down")}
              style={{ width: "1.5rem", height: "1.5rem", borderRadius: "0.25rem", display: "flex", alignItems: "center", justifyContent: "center", transition: "color 0.15s", background: "var(--paper-3)", border: "1px solid var(--rule)", cursor: "pointer" }}
            >
              👎
            </button>
          </>
        )}
      </div>
    );
  }, [handleFeedback, isOwnedSession]);

  const handleEditSubmit = useCallback(async (idx: number) => {
    const text = editText.trim();
    if (!text || loading || !isOwnedSession) return;
    // Remove the original message and everything after it, then re-run
    const trimmedMessages = messages.slice(0, idx);
    setMessages(trimmedMessages);
    setEditingIdx(null);
    setEditText("");
    await submit(text, trimmedMessages);
  }, [editText, isOwnedSession, loading, messages, submit]);

  const runApprovedAction = useCallback(async (messageId: string, action: SuggestedAction, confirmed: boolean) => {
    if (!isOwnedSession) return;
    setMessages((prev) =>
      prev.map((m) =>
        m.id === messageId
          ? { ...m, executionResult: { success: true, output: "Running command..." } }
          : m
      )
    );

    try {
      const res = await executeCommand(action.command ?? "", confirmed, sshCreds, sessionId, action.stdin);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? { ...m, executionResult: { success: res.success, output: res.output, error: res.error } }
            : m
        )
      );
    } catch (err) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId
            ? { ...m, executionResult: { success: false, error: String(err) } }
            : m
        )
      );
    }
  }, [isOwnedSession, sessionId, sshCreds]);

  const handleExecuteAction = useCallback(async (messageId: string, action: SuggestedAction) => {
    if (!isOwnedSession) return;
    if (action.confirm) {
      setPendingApproval({ messageId, action });
      return;
    }
    await runApprovedAction(messageId, action, false);
  }, [isOwnedSession, runApprovedAction]);

  const approvePendingAction = useCallback(async () => {
    if (!pendingApproval) return;
    const { messageId, action } = pendingApproval;
    setPendingApproval(null);
    await runApprovedAction(messageId, action, true);
  }, [pendingApproval, runApprovedAction]);

  const handleNewChat = useCallback(async () => {
    resetSharedViewState();
    if (authIsEnabled && currentUser) {
      const created = await createChatSession();
      setSessions((prev) => [created, ...prev.filter((s) => s.id !== created.id)]);
      setSessionId(created.id);
      setMessages([]);
      setHistoryLoaded(true);
      return;
    }
    // Was `uid() + uid()`. Two Math.random() draws look like more entropy than
    // one and are not: both come from the same clock-seeded state, so the pair
    // is no harder to predict than the first. This is the id that ends up in
    // /chat/:sessionId, so it gets the CSPRNG like every other session id.
    const newId = randomSessionId();
    setSessionId(newId);
    if (typeof window !== "undefined") localStorage.setItem("k8s_session_id", newId);
    setMessages([]);
    setHistoryLoaded(true);
  }, [authIsEnabled, currentUser, resetSharedViewState]);

  const handleDeleteSession = useCallback(async (id: string) => {
    if (!isOwnedSession) return;
    if (authIsEnabled && currentUser) {
      await deleteChatSession(id);
      const updated = sessions.filter(s => s.id !== id);
      setSessions(updated);
      if (id === sessionId) {
        if (updated.length > 0) {
          setSessionId(updated[0].id);
          setMessages([]);
          setHistoryLoaded(false);
        } else {
          await handleNewChat();
        }
      }
      return;
    }

    const updated = sessions.filter(s => s.id !== id);
    setSessions(updated);
    if (typeof window !== "undefined") localStorage.setItem("k8s_sessions", JSON.stringify(updated));
    if (id === sessionId) handleNewChat();
  }, [authIsEnabled, currentUser, handleNewChat, isOwnedSession, sessionId, sessions]);

  const handleShare = useCallback(() => {
    if (typeof window === "undefined") return;
    // Query form, not /chat/<id>: the path form needs a dynamic route, which
    // a static export cannot produce. Already-shared /chat/<id> links keep
    // working in server builds via the redirect page.
    const url = `${window.location.origin}/chat?session=${encodeURIComponent(sessionId)}`;
    copyToClipboard(url).then(() => {
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 1500);
    });
  }, [sessionId]);

  const handleExportPM = useCallback(async () => {
    if (!sessionId || !isOwnedSession) return;
    setExportingPM(true);
    try {
      const markdown = await exportPostMortem(sessionId);
      // Trigger download
      const blob = new Blob([markdown], { type: "text/markdown" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `post-mortem-${sessionId.slice(0,8)}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Failed to export post-mortem:", err);
      alert("Failed to export post-mortem. Please try again.");
    } finally {
      setExportingPM(false);
    }
  }, [isOwnedSession, sessionId]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!isOwnedSession) return;
      submit(input);
    }
  };

  const isEmpty = historyLoaded && messages.length === 0 && !isDeniedSharedSession;
  const sharedOwnerLabel = sharedSessionMeta.owner || "another user";

  if (!authLoaded) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--paper)", color: "var(--ink-3)" }}>
        Loading...
      </div>
    );
  }

  if (authStatus?.auth_enabled && !authStatus.user) {
    return <AuthPanel status={authStatus} onAuthenticated={handleAuthenticated} />;
  }

  // The new mission-control UI is now the only UI — light/dark just swap
  // colors within the same components. We keep this variable as `true`
  // to preserve the branch shape for a future re-fork if we ever want a
  // classic-UI fallback; today it collapses to a single code path.
  const isMissionControl = true;

  const exportPMButton = (
    <button
      onClick={handleExportPM}
      disabled={!isOwnedSession || exportingPM || messages.length === 0}
      className="app-btn-ghost"
      style={{ display: "flex", alignItems: "center", gap: "0.375rem", padding: "0.375rem 0.625rem", borderRadius: "0.375rem", fontSize: "0.75rem", fontWeight: 500, color: "var(--ink)", opacity: (!isOwnedSession || exportingPM || messages.length === 0) ? 0.5 : 1, transition: "background 0.15s, opacity 0.15s" }}
      title={isOwnedSession ? "Write up this investigation as a post-mortem report" : "Saving a report is disabled for read-only shared sessions"}
    >
      {exportingPM ? (
        <svg className="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      )}
      {/* Was "Export PM". "PM" reads as post-mortem to whoever wrote it and
          project manager to everyone else. */}
      <span>Save report</span>
    </button>
  );

  const alertsButton = !isDeniedSharedSession && (
    <button
      onClick={() => {
        if (typeof window !== "undefined") {
          sessionStorage.setItem("k8s_chat_return_session", sessionId);
        }
        window.location.href = "/alerts";
      }}
      className="app-btn-ghost"
      style={{ padding: "0.25rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.75rem", marginLeft: "0.5rem" }}
      title="View triggered investigations and RCAs"
    >
      Alerts
    </button>
  );

  const newInvestigationButton = (
    <button
      onClick={handleNewChat}
      className="app-btn-ghost"
      style={{ padding: "0.25rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.75rem" }}
      title="Start a new investigation"
    >
      New investigation
    </button>
  );

  // Set-once items only. Anything you might reach for while something is
  // broken — the target, alerts, saving a report — stays in the bar.
  const overflowItems: OverflowItem[] = [
    ...(messages.length > 0 && !isDeniedSharedSession
      ? [{
          group: "This investigation",
          label: shareCopied ? "Link copied" : "Copy a read-only link",
          onSelect: handleShare,
          icon: (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M4 12v8h16v-8"/><path d="M16 6l-4-4-4 4"/><path d="M12 2v14"/></svg>
          ),
        }]
      : []),
    {
      group: "Appearance",
      label: !mounted ? "Switch theme" : theme === "light" ? "Dark theme" : "Light theme",
      onSelect: () => setTheme(t => (t === "light" ? "dark" : "light")),
      icon: !mounted || theme === "light" ? (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
      ) : (
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
      ),
    },
    ...(authIsEnabled && currentUser
      ? [
          {
            group: currentUser.display_name || currentUser.username,
            label: "Account",
            onSelect: () => setAccountOpen(true),
            icon: (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"><circle cx="12" cy="8" r="3.6"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/></svg>
            ),
          },
          {
            label: "Sign out",
            onSelect: async () => {
              await logout();
              setAuthStatus({ auth_enabled: true, allow_signup: authStatus?.allow_signup ?? false, user: null });
              setMessages([]);
              setSessions([]);
              setClusterConn(null);
              setSshCreds(null);
              setHistoryLoaded(true);
            },
            icon: (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>
            ),
          },
        ]
      : []),
  ];

  // The header's subject: what this session is aimed at. Sits on the left,
  // holds the popover, and replaces six controls that all described it.
  const targetControl = isOwnedSession ? (
    <div style={{ position: "relative", display: "flex", alignItems: "center" }}>
      <ClusterConnect
        sessionId={sessionId}
        status={clusterConn}
        onStatusChange={(status) => setClusterConn(status?.connected ? status : null)}
        isOpen={activePopover === "cluster"}
        onToggle={(open) => setActivePopover(open ? "cluster" : "none")}
        onUseSsh={() => setActivePopover("ssh")}
        trigger={({ open, toggle }) => (
          <TargetBar
            contextName={clusterConn?.context_name || clusterConn?.cluster_name}
            namespace={clusterConn?.namespace}
            sshHost={sshCreds ? `${sshCreds.username}@${sshCreds.host}` : null}
            mode={health?.kubectl_mode}
            loaded={healthLoaded}
            reachable={Boolean(clusterConn?.connected || sshCreds || health?.kubectl_available)}
            onClick={toggle}
            expanded={open}
          />
        )}
      />
      <SSHPanel
        sessionId={sessionId}
        connected={sshCreds}
        onConnect={handleConnect}
        onDisconnect={handleDisconnect}
        isOpen={activePopover === "ssh"}
        onToggle={(open) => setActivePopover(open ? "ssh" : "none")}
      />
    </div>
  ) : null;

  // Server mode with no provider configured. Desktop has the first-run
  // wizard instead, so `desktopSetup` being present suppresses this.
  const noModel = Boolean(healthLoaded && health && !health.ai_enabled && !desktopSetup);

  const headerRightControls = (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.75rem" }}>
      <HeaderLiveCounters sessionId={sessionId} />
      {alertsButton}
      {exportPMButton}
      {newInvestigationButton}
      <HeaderOverflow
        open={overflowOpen}
        onOpenChange={setOverflowOpen}
        items={overflowItems}
      />
    </div>
  );

  return (
    <div style={{ display: "flex", width: "100vw", height: "100vh", overflow: "hidden", background: "var(--paper)", color: "var(--ink)" }}>
      {accountOpen && currentUser && (
        <AccountSettings
          user={currentUser}
          onClose={() => setAccountOpen(false)}
          onAuthChanged={(status) => setAuthStatus(status)}
        />
      )}
      {isMissionControl ? (
        <MissionControlLeftRail
          sessions={sessions.map((s) => ({ id: s.id, title: s.title, timestamp: s.timestamp }))}
          currentSessionId={sessionId}
          onSelectSession={(id) => {
            resetSharedViewState();
            setSessionId(id);
            if (!authIsEnabled && typeof window !== "undefined") localStorage.setItem("k8s_session_id", id);
            setMessages([]);
            setHistoryLoaded(false);
          }}
          onNewSession={handleNewChat}
          onDeleteSession={handleDeleteSession}
        />
      ) : (
        <SessionSidebar
          isOpen={sidebarOpen}
          onClose={() => setSidebarOpen(false)}
          sessions={sessions}
          currentSessionId={sessionId}
          onSelectSession={(id) => {
            resetSharedViewState();
            setSessionId(id);
            if (!authIsEnabled && typeof window !== "undefined") localStorage.setItem("k8s_session_id", id);
            setMessages([]);
            setHistoryLoaded(false);
          }}
          onNewSession={handleNewChat}
          onDeleteSession={handleDeleteSession}
        />
      )}
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {isOwnedSession && pendingApproval && (
          isMissionControl ? (
            <MissionControlApprovalOverlay
              onClose={() => setPendingApproval(null)}
              onConfirm={approvePendingAction}
              title={pendingApproval.action.label || "Review and execute fix"}
              coordinates={[
                { label: "cluster", value: sshCreds?.host || clusterConn?.cluster_name || clusterConn?.context_name || "Local" },
                ...(pendingApproval.action.risk ? [{ label: "risk", value: pendingApproval.action.risk }] : []),
              ]}
              preflightChecks={[
                "AI DevOps Assistant requires your approval before running this recovery action.",
              ]}
              diffFileHeader={pendingApproval.action.command ?? ""}
              diffLines={[
                { kind: "context", text: pendingApproval.action.command ?? "" },
                ...(pendingApproval.action.stdin
                  ? [{ kind: "context" as const, text: "── stdin ──" }].concat(
                      pendingApproval.action.stdin.split("\n").map((line) => ({ kind: "context" as const, text: line })),
                    )
                  : []),
              ]}
              executionCommand={pendingApproval.action.command}
            />
          ) : (
            <ApprovalOverlay
              onClose={() => setPendingApproval(null)}
              onConfirm={approvePendingAction}
              commandInfo={{
                command: pendingApproval.action.command ?? "",
                explanation: `${pendingApproval.action.label || "Review and execute fix"}${pendingApproval.action.risk ? ` (${pendingApproval.action.risk} risk)` : ""}. AI DevOps Assistant requires your approval before running this recovery action.`,
                stdin: pendingApproval.action.stdin
              }}
              contextName={sshCreds?.host || clusterConn?.context_name || "Local"}
            />
          )
        )}

        {/* Desktop first run: no AI provider configured yet. Absent in
            server mode, where desktopSetup stays null. */}
        {desktopSetup && !desktopSetup.configured && (
          <FirstRunWizard
            state={desktopSetup}
            onComplete={() => {
              fetchDesktopSetup()
                .then(setDesktopSetup)
                .catch(() => setDesktopSetup(null));
            }}
          />
        )}

        {isMissionControl ? (
          <div className="mc-header-controls">
            <MissionControlHeader
              clusterStatus={clusterConn}
              busy={loading}
              targetSlot={targetControl}
              rightSlot={headerRightControls}
            />
          </div>
        ) : (
        <header
          style={{ flexShrink: 0, padding: "0.5rem 1rem 0.5rem 0.5rem", display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", borderBottom: "1px solid var(--rule)", background: "var(--paper-2)" }}
        >
          {/* The target is the header. The wordmark and the "paste an error or
              ask a question" strapline left with it: the mark is already in
              the target glyph, and the strapline restated the placeholder text
              sitting in the input box below. */}
          <div style={{ display: "flex", alignItems: "center", gap: "0.25rem", minWidth: 0 }}>
            <button
              onClick={() => setSidebarOpen(true)}
              className="app-btn-ghost"
              style={{ padding: "0.5rem", borderRadius: "0.5rem", border: "none", background: "none", display: sidebarOpen ? "none" : "flex", alignItems: "center", color: "var(--ink-3)" }}
              title="Investigations"
              aria-label="Show investigations"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
            </button>
            {targetControl}
          </div>

          {headerRightControls}
        </header>
        )}

      {(isReadonlySharedSession || isDeniedSharedSession) && (
        <div style={{
          flexShrink: 0,
          borderBottom: "1px solid var(--rule)",
          padding: "0.75rem 1rem",
          background: isDeniedSharedSession ? "rgba(239,68,68,0.08)" : "var(--brand-bg)",
          color: isDeniedSharedSession ? "var(--danger)" : "var(--brand)",
        }}>
          <div style={{ maxWidth: "48rem", margin: "0 auto", fontSize: "0.875rem", lineHeight: 1.5 }}>
            {isDeniedSharedSession ? (
              <strong>{sharedAccessError || "You do not have access to this shared chat."}</strong>
            ) : (
              <>
                <strong>Read-only shared chat.</strong>{" "}
                You are viewing {sharedSessionMeta.title ? `"${sharedSessionMeta.title}"` : "this chat"} from {sharedOwnerLabel} as an admin.
              </>
            )}
          </div>
        </div>
      )}

      {/* ── SSH reconnect banner ── */}
      {isOwnedSession && pendingReconnect && !sshCreds && (
        <ReconnectBanner
          target={pendingReconnect}
          onReconnect={handleReconnectFromBanner}
          onDismiss={() => {
            setPendingReconnect(null);
            deleteSshTarget(sessionId);
          }}
        />
      )}

      {/* ── messages ── */}
      <div style={{ flex: 1, overflowY: "auto", padding: "1.5rem 1rem", minWidth: 0 }}>
        <div style={{ maxWidth: "48rem", margin: "0 auto", display: "flex", flexDirection: "column", gap: "1.5rem", width: "100%", minWidth: 0 }}>

          {!historyLoaded && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "8rem", fontSize: "0.875rem", color: "var(--ink-3)" }}>
              Loading history…
            </div>
          )}

          {isEmpty && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", minHeight: "60vh", textAlign: "center", gap: "1.5rem" }}>
              <div>
                {isMissionControl ? (
                  <>
                    <div style={{ margin: "0 auto 1rem auto", width: "max-content" }}>
                      <AstraGlyph size={56} animate />
                    </div>
                    <div style={{ fontFamily: "var(--sans)", fontSize: 20, fontWeight: 600, color: "var(--ink, var(--fg-0))" }}>
                      {isReadonlySharedSession
                        ? "Empty shared chat"
                        : noModel
                          ? "Astra needs a model"
                          : "Astra is online"}
                    </div>
                    <div style={{ marginTop: 6, fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-3, var(--fg-3))", letterSpacing: "0.05em" }}>
                      {/* "Ready to investigate" above "no language model
                          connected" is the contradiction this whole change
                          exists to remove. */}
                      {isReadonlySharedSession
                        ? "read-only · no messages"
                        : noModel
                          ? "cluster tools ready · reasoning offline"
                          : "Ready to investigate · standing by"}
                    </div>
                    {isOwnedSession && !sshCreds && !clusterConn?.connected && (
                      <div style={{ marginTop: 12, fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3, var(--fg-3))" }}>
                        No target — choose a cluster from the header to begin.
                      </div>
                    )}
                    {/* Only once health has answered, so a slow first request
                        does not flash "no model" at someone who has one, and
                        only in server mode — desktop has the first-run wizard
                        and `desktopSetup` is null here otherwise. */}
                    {noModel && isOwnedSession && (
                      <SetupNotice provider={health?.llm_provider} />
                    )}
                  </>
                ) : (
                  <>
                    {/* Large KubeAstra emblem */}
                    <div style={{ margin: "0 auto 1.25rem auto", width: "max-content" }}>
                      <KubeAstraEmblem size={60} />
                    </div>
                    <h2 style={{ fontSize: "1.5rem", fontWeight: 600, color: "var(--ink)", margin: 0 }}>
                      {isReadonlySharedSession ? "This shared chat has no messages yet" : "How can I help you today?"}
                    </h2>
                    <p style={{ marginTop: "0.5rem", fontSize: "0.875rem", maxWidth: "28rem", margin: "0.5rem auto 0 auto", color: "var(--ink-2)" }}>
                      {isReadonlySharedSession
                        ? "You can view this chat as an admin, but cannot add messages to it."
                        : "Ask about Kubernetes errors, pod status, logs, or events. I'll route to the right tool automatically."}
                    </p>
                    {isOwnedSession && !sshCreds && (
                      <p style={{ marginTop: "0.5rem", fontSize: "0.75rem", color: "var(--ink-3)" }}>
                        To reach a cluster on another host, choose{" "}
                        <span style={{ color: "var(--brand)" }}>Over SSH</span> from the target in the header.
                      </p>
                    )}
                  </>
                )}
              </div>

              {isOwnedSession && (
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "0.5rem", width: "100%", maxWidth: "36rem" }}>
                  {EXAMPLES.map((ex) => (
                    <button
                      key={ex}
                      onClick={() => submit(ex)}
                      style={{
                        textAlign: "left", fontSize: "0.75rem", borderRadius: "0.75rem", padding: "0.75rem 1rem", transition: "all 0.15s",
                        color: "var(--ink-2)",
                        background: "var(--paper-2)",
                        border: "1px solid var(--rule)",
                        cursor: "pointer",
                      }}
                      onMouseEnter={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--brand-bd)";
                        (e.currentTarget as HTMLButtonElement).style.color = "var(--ink)";
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--rule)";
                        (e.currentTarget as HTMLButtonElement).style.color = "var(--ink-2)";
                      }}
                    >
                      {ex}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {isDeniedSharedSession && historyLoaded && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: "60vh", textAlign: "center", gap: "1rem", color: "var(--danger)" }}>
              <h2 style={{ fontSize: "1.25rem", margin: 0 }}>Shared chat unavailable</h2>
              <p style={{ maxWidth: "32rem", margin: 0, color: "var(--ink-2)" }}>
                {sharedAccessError || "You do not have access to this shared chat."}
              </p>
              <button onClick={handleNewChat} className="app-btn-primary" style={{ borderRadius: "0.5rem", padding: "0.5rem 1rem", fontSize: "0.875rem" }}>
                Start a new chat
              </button>
            </div>
          )}

          {/* message list */}
          {messages.map((m, idx) => (
            <div
              key={m.id}
              style={{ display: "flex", gap: "0.75rem", flexDirection: m.role === "user" ? "row-reverse" : "row" }}
            >

              {/* avatar — hidden in mission-control (terminal-style prefixes replace it) */}
              {!isMissionControl && (
                <div
                  style={{
                    flexShrink: 0, width: "2rem", height: "2rem", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "0.75rem", fontWeight: "bold",
                    ...(m.role === "user"
                      ? { background: "var(--brand)", color: "#000" }
                      : { background: "var(--paper-3)", color: "var(--ink-2)", border: "1px solid var(--rule)" }
                    )
                  }}
                >
                  {m.role === "user" ? "U" : "⎈"}
                </div>
              )}

              <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", maxWidth: "85%", minWidth: 0, alignItems: m.role === "user" ? "flex-end" : "flex-start" }}>

                {m.role === "user" && m.viaSSH && sshCreds && (
                  <span style={{ fontSize: "10px", padding: "0 0.25rem", color: "var(--brand)" }}>
                    via SSH · {sshCreds.host}
                  </span>
                )}

                {m.role === "assistant" && !m.loading && m.reactSteps && m.reactSteps.length > 0 && (
                  isMissionControl
                    ? <MissionControlToolTrail steps={m.reactSteps} thinking={false} />
                    : <InvestigationTrail steps={m.reactSteps} thinking={false} />
                )}

                {m.role === "assistant" && !m.loading && m.result && m.tool && m.tool !== "none" && m.text && (
                  <div
                    style={
                      isMissionControl
                        ? {
                            borderRadius: 6,
                            padding: "10px 14px",
                            fontFamily: "var(--sans)",
                            fontSize: 12.5,
                            lineHeight: 1.55,
                            background: "var(--bg-1, var(--paper-2))",
                            color: "var(--ink, var(--fg-1))",
                            border: "1px solid var(--line, var(--rule))",
                            borderLeft: "2px solid var(--cyan, var(--brand))",
                            wordBreak: "break-word",
                            overflowWrap: "break-word",
                            maxWidth: "100%",
                          }
                        : {
                            borderRadius: "1rem",
                            borderTopLeftRadius: "0.125rem",
                            padding: "0.75rem 1rem",
                            fontSize: "0.875rem",
                            lineHeight: 1.6,
                            background: "var(--paper-2)",
                            color: "var(--ink)",
                            border: "1px solid var(--rule)",
                            wordBreak: "break-word",
                            overflowWrap: "break-word",
                            maxWidth: "100%",
                          }
                    }
                  >
                    {isMissionControl && (
                      <div aria-hidden="true" style={{
                        fontFamily: "var(--mono)",
                        fontSize: 9,
                        textTransform: "uppercase",
                        letterSpacing: "0.10em",
                        color: "var(--cyan, var(--brand))",
                        marginBottom: 6,
                      }}>
                        astra›
                      </div>
                    )}
                    <MarkdownMessage text={m.text} onApplyYaml={isOwnedSession ? (yaml) => {
                      handleExecuteAction(m.id, {
                        label: "Apply YAML Patch",
                        command: "kubectl apply -f -",
                        confirm: true,
                        stdin: yaml,
                      });
                    } : undefined} />
                    {m.tool === "proactive_triage" && renderFeedbackControls(m, "inline")}
                  </div>
                )}

                {m.role === "assistant" && !m.loading && m.result && m.tool && m.tool !== "none" && m.tool !== "proactive_triage" && (() => {
                  const isInvestigation = m.tool === "investigate_pod" || m.tool === "investigate_workload" || m.tool === "analyze_namespace";
                  const executable = firstExecutableAction(m.suggestedActions);
                  const onExec = isOwnedSession && executable ? () => handleExecuteAction(m.id, executable) : undefined;
                  const missionControlDiagnosis = isMissionControl && isInvestigation ? resultToMissionControlDiagnosis(m.result) : null;
                  return (
                    <div style={{ width: "100%", display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                      {missionControlDiagnosis ? (
                        // Mission Control mode: the diagnosis card is the whole
                        // presentation. Skip ResultCard — it duplicates evidence,
                        // metrics, and next-actions that Diagnosis already covers.
                        <MissionControlDiagnosis
                          severity={missionControlDiagnosis.severity}
                          title={missionControlDiagnosis.title}
                          summary={missionControlDiagnosis.summary}
                          confidence={missionControlDiagnosis.confidence}
                          metrics={missionControlDiagnosis.metrics}
                          diff={missionControlDiagnosis.diff}
                          diffMeta={missionControlDiagnosis.diffMeta}
                          onAuthorize={onExec}
                        />
                      ) : (
                        <>
                          {isInvestigation && !isMissionControl && (
                            <RootCauseCard
                              result={m.result}
                              onReviewExecute={onExec}
                            />
                          )}
                          <ResultCard tool={m.tool} result={m.result} footerSlot={renderFeedbackControls(m, "footer")} />
                        </>
                      )}
                    </div>
                  );
                })()}

                {/* ── User message ── */}
                {m.role === "user" && (
                  editingIdx === idx ? (
                    /* Inline edit textarea */
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", width: "100%", maxWidth: "100%" }}>
                      <textarea
                        name="message-edit"
                        ref={(el) => {
                          if (el) {
                            el.style.height = "auto";
                            el.style.height = `${Math.min(el.scrollHeight, 400)}px`;
                          }
                        }}
                        style={{
                          borderRadius: "0.75rem", padding: "0.75rem 1rem", fontSize: "0.875rem", lineHeight: 1.6, resize: "vertical",
                          width: "100%",
                          background: "var(--paper-2)",
                          color: "var(--ink)",
                          border: "1px solid var(--brand)",
                          outline: "none",
                          minHeight: "80px",
                          maxHeight: "400px",
                        }}
                        value={editText}
                        autoFocus
                        onChange={(e) => {
                          setEditText(e.target.value);
                          e.target.style.height = "auto";
                          e.target.style.height = `${Math.min(e.target.scrollHeight, 400)}px`;
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleEditSubmit(idx); }
                          if (e.key === "Escape") handleEditCancel();
                        }}
                      />
                      <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
                        <button
                          onClick={handleEditCancel}
                          style={{ padding: "0.25rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.75rem", color: "var(--ink-3)", border: "1px solid var(--rule)", background: "transparent", cursor: "pointer" }}
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => handleEditSubmit(idx)}
                          disabled={!editText.trim() || loading}
                          style={{ padding: "0.25rem 0.75rem", borderRadius: "0.5rem", fontSize: "0.75rem", fontWeight: 500, background: "var(--brand)", color: "#000", border: "none", cursor: "pointer", opacity: (!editText.trim() || loading) ? 0.5 : 1 }}
                        >
                          Send
                        </button>
                      </div>
                    </div>
                  ) : (
                    /* Icon-only actions left of bubble, bubble to the right */
                    <div style={{ display: "flex", alignItems: "center", gap: "0.375rem", maxWidth: "100%", minWidth: 0 }}>
                      <div
                        style={{ display: "flex", gap: "0.25rem", opacity: 1, transition: "opacity 0.15s", flexShrink: 0 }}
                      >
                        <button
                          title={copiedIdx === idx ? "Copied!" : "Copy"}
                          onClick={() => handleCopy(m.text, idx)}
                          style={{
                            width: "1.75rem", height: "1.75rem", borderRadius: "0.5rem", display: "flex", alignItems: "center", justifyContent: "center", transition: "color 0.15s", cursor: "pointer",
                            background: "var(--paper-3)",
                            border: "1px solid var(--rule)",
                            color: copiedIdx === idx ? "var(--success)" : "var(--ink-3)",
                          }}
                        >
                          {copiedIdx === idx ? (
                            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M2 8l4 4 8-8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
                          ) : (
                            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><rect x="5" y="5" width="9" height="9" rx="1.5" stroke="currentColor" strokeWidth="1.5"/><path d="M11 5V3.5A1.5 1.5 0 009.5 2h-6A1.5 1.5 0 002 3.5v6A1.5 1.5 0 003.5 11H5" stroke="currentColor" strokeWidth="1.5"/></svg>
                          )}
                        </button>
                        {isOwnedSession && (
                          <button
                            title="Edit and resend"
                            onClick={() => handleEditStart(idx, m.text)}
                            style={{
                              width: "1.75rem", height: "1.75rem", borderRadius: "0.5rem", display: "flex", alignItems: "center", justifyContent: "center", transition: "color 0.15s", cursor: "pointer",
                              background: "var(--paper-3)",
                              border: "1px solid var(--rule)",
                              color: "var(--ink-3)",
                            }}
                          >
                            <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5a1.5 1.5 0 012.12 2.12L5 13.24l-3 .76.76-3L11.5 2.5z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                          </button>
                        )}
                      </div>
                      <div
                        style={
                          isMissionControl
                            ? {
                                borderRadius: 6,
                                padding: "10px 14px",
                                fontFamily: "var(--mono)",
                                fontSize: 12,
                                lineHeight: 1.55,
                                background: "var(--cyan-bg, var(--brand-bg))",
                                color: "var(--ink, var(--fg-0))",
                                border: "1px solid var(--cyan-bd, var(--brand-bd))",
                                wordBreak: "break-word",
                                overflowWrap: "break-word",
                                maxWidth: "100%",
                              }
                            : {
                                borderRadius: "1rem",
                                borderTopRightRadius: "0.125rem",
                                padding: "0.75rem 1rem",
                                fontSize: "0.875rem",
                                lineHeight: 1.6,
                                background: "var(--brand)",
                                color: "#000",
                                wordBreak: "break-word",
                                overflowWrap: "break-word",
                                maxWidth: "100%",
                              }
                        }
                      >
                        {isMissionControl && (
                          <span aria-hidden="true" style={{ color: "var(--cyan, var(--brand))", marginRight: 6 }}>you›</span>
                        )}
                        <span style={{ whiteSpace: "pre-wrap" }}>{m.text}</span>
                      </div>
                    </div>
                  )
                )}

                {/* ── Assistant bubble ── */}
                {m.role === "assistant" && (m.loading || !m.result || !m.tool || m.tool === "none") && (
                  m.loading ? (
                    isMissionControl
                      ? <MissionControlToolTrail steps={m.reactSteps || []} thinking={true} />
                      : <InvestigationTrail steps={m.reactSteps || []} thinking={true} />
                  ) : (
                    <div
                      style={
                        isMissionControl
                          ? {
                              borderRadius: 6,
                              padding: "10px 14px",
                              fontFamily: "var(--sans)",
                              fontSize: 12.5,
                              lineHeight: 1.55,
                              background: "var(--bg-1, var(--paper-2))",
                              color: "var(--ink, var(--fg-1))",
                              border: "1px solid var(--line, var(--rule))",
                              borderLeft: "2px solid var(--cyan, var(--brand))",
                              wordBreak: "break-word",
                              overflowWrap: "break-word",
                              maxWidth: "100%",
                            }
                          : {
                              borderRadius: "1rem",
                              borderTopLeftRadius: "0.125rem",
                              padding: "0.75rem 1rem",
                              fontSize: "0.875rem",
                              lineHeight: 1.6,
                              background: "var(--paper-2)",
                              color: "var(--ink)",
                              border: "1px solid var(--rule)",
                              wordBreak: "break-word",
                              overflowWrap: "break-word",
                              maxWidth: "100%",
                            }
                      }
                    >
                      {isMissionControl && (
                        <div aria-hidden="true" style={{
                          fontFamily: "var(--mono)",
                          fontSize: 9,
                          textTransform: "uppercase",
                          letterSpacing: "0.10em",
                          color: "var(--cyan, var(--brand))",
                          marginBottom: 6,
                        }}>
                          astra›
                        </div>
                      )}
                      <MarkdownMessage text={m.text} onApplyYaml={isOwnedSession ? (yaml) => {
                        handleExecuteAction(m.id, {
                          label: "Apply YAML Patch",
                          command: "kubectl apply -f -",
                          confirm: true,
                          stdin: yaml,
                        });
                      } : undefined} />
                    </div>
                  )
                )}

                {m.error && (
                  <p style={{ fontSize: "0.75rem", padding: "0 0.25rem", color: "var(--danger)", margin: 0 }}>{m.error}</p>
                )}

                {m.role === "assistant" && !m.loading && m.costSummary && (m.costSummary.total_cost_usd > 0 || (m.costSummary.total_tokens_in + m.costSummary.total_tokens_out) > 0) && (
                  <div
                    role={m.runId ? "button" : undefined}
                    tabIndex={m.runId ? 0 : undefined}
                    aria-label={m.runId ? "View run spend metrics" : undefined}
                    onClick={() => {
                      if (m.runId) {
                        setCostBreakdownRunId(m.runId);
                      }
                    }}
                    onKeyDown={(e) => {
                      if (m.runId && (e.key === "Enter" || e.key === " ")) {
                        e.preventDefault();
                        setCostBreakdownRunId(m.runId);
                      }
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.375rem",
                      padding: "0.25rem 0.625rem",
                      borderRadius: "1rem",
                      background: "var(--paper-3)",
                      border: "1px solid var(--rule)",
                      fontSize: "0.75rem",
                      color: "var(--ink-3)",
                      cursor: m.runId ? "pointer" : "default",
                      userSelect: "none",
                      transition: "all 0.15s ease",
                      marginTop: "0.25rem",
                      outline: "none",
                    }}
                    onMouseEnter={(e) => {
                      if (m.runId) {
                        e.currentTarget.style.borderColor = "var(--brand-bd)";
                        e.currentTarget.style.color = "var(--brand)";
                        e.currentTarget.style.background = "var(--brand-bg)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (m.runId) {
                        e.currentTarget.style.borderColor = "var(--rule)";
                        e.currentTarget.style.color = "var(--ink-3)";
                        e.currentTarget.style.background = "var(--paper-3)";
                      }
                    }}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                      <line x1="12" y1="1" x2="12" y2="23"></line>
                      <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path>
                    </svg>
                    <span>
                      {formatCost(m.costSummary.total_cost_usd)}
                    </span>
                    <span style={{ color: "var(--ink-4)" }}>·</span>
                    <span>
                      {formatTokens(m.costSummary.total_tokens_in + m.costSummary.total_tokens_out)} tokens
                    </span>
                  </div>
                )}

                {/* Feedback buttons. Captured answers can be promoted/quarantined;
                    uncaptured answers are stored as audit-only feedback. */}
                {!(m.role === "assistant" && !m.loading && m.result && m.tool && m.tool !== "none" && (m.tool !== "proactive_triage" || Boolean(m.text))) && renderFeedbackControls(m)}

                {isOwnedSession && m.role === "assistant" && !m.loading && m.suggestedActions && m.suggestedActions.length > 0 && (
                  <SuggestedActions
                    actions={m.suggestedActions}
                    onExecute={(action) => handleExecuteAction(m.id, action)}
                    onFollowUp={(prompt) => submit(prompt)}
                  />
                )}

                {m.executionResult && (
                  <div
                    style={{
                      width: "100%", borderRadius: "0.75rem", padding: "0.5rem 0.75rem", fontSize: "0.75rem", whiteSpace: "pre-wrap",
                      background: "var(--paper-3)",
                      border: "1px solid var(--rule)",
                      color: m.executionResult.success ? "var(--ink-2)" : "var(--danger)",
                    }}
                  >
                    {m.executionResult.output || m.executionResult.error || "Command completed."}
                  </div>
                )}
              </div>
            </div>
          ))}

          <div ref={bottomRef} />
        </div>
      </div>

      {/* ── input bar ── */}
      {isOwnedSession ? (
        isMissionControl ? (
          <CommandBar
            onSend={(text) => submit(text)}
            busy={loading}
            clusterLabel={sshCreds?.host || clusterConn?.cluster_name || clusterConn?.context_name || "Local"}
            clusterConnected={!!(clusterConn?.connected || sshCreds)}
          />
        ) : (
          <IntentBar
            onSend={(text) => submit(text)}
            listening={loading}
            onStop={handleStop}
            contextName={sshCreds?.host || clusterConn?.context_name || "Local"}
          />
        )
      ) : (
        <div style={{ flexShrink: 0, borderTop: "1px solid var(--rule)", padding: "0.875rem 1rem", textAlign: "center", fontSize: "0.875rem", color: "var(--ink-3)", background: "var(--paper-2)" }}>
          {isDeniedSharedSession
            ? "Shared chat access is unavailable for this account."
            : "Read-only shared chat: messages and actions are disabled."}
        </div>
      )}
    </div>

    {costBreakdownRunId && (
      <CostBreakdownOverlay
        runId={costBreakdownRunId}
        onClose={() => setCostBreakdownRunId(null)}
      />
    )}
  </div>
  );
}
