import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type { ChatMessage, ChatStreamEvent } from "@lib/api";
import { onHostMessage, ready, sendChatStream, isAbortError } from "./webviewApi";

interface AuthState {
  signedIn: boolean;
  backendUrl: string | null;
  authRequired: boolean;
}

interface TrailStep {
  iteration?: number;
  thought?: string;
  action?: string;
}

/**
 * M2 slim chat: proves the end-to-end streaming pipeline (webview → host proxy →
 * backend SSE → back). Renders a plain thread, a live ReAct step trail, and the
 * streamed answer. M3 swaps these plain elements for the reused Mission Control
 * components (MissionControlHeader / ToolTrail / Diagnosis / CommandBar).
 */
export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [draft, setDraft] = useState("");
  const [trail, setTrail] = useState<TrailStep[]>([]);
  const abortRef = useRef<(() => void) | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const off = onHostMessage((msg) => {
      if (msg.type === "auth-state") {
        setAuth({
          signedIn: msg.signedIn,
          backendUrl: msg.backendUrl,
          authRequired: msg.authRequired,
        });
      } else if (msg.type === "prompt") {
        setDraft(msg.text);
      }
    });
    ready();
    return off;
  }, []);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, trail]);

  const canSend = draft.trim().length > 0 && !streaming;
  const ready4Chat = auth?.backendUrl && (auth.signedIn || !auth.authRequired);

  function send() {
    const text = draft.trim();
    if (!text || streaming) return;
    const history = messages;
    setMessages([...history, { role: "user", content: text }, { role: "assistant", content: "" }]);
    setDraft("");
    setTrail([]);
    setStreaming(true);

    const onEvent = (evt: ChatStreamEvent) => {
      if (evt.type === "iteration_planned" || evt.type === "step_complete") {
        setTrail((t) => [...t, { iteration: evt.iteration, thought: evt.thought, action: evt.action }]);
      } else if (evt.type === "token" && evt.text) {
        appendAssistant(evt.text);
      }
    };

    const { result, abort } = sendChatStream(text, history, onEvent);
    abortRef.current = abort;
    result
      .then((res) => {
        // If no tokens streamed, fall back to the final reply.
        setMessages((m) => {
          const last = m[m.length - 1];
          if (last && last.role === "assistant" && last.content === "") {
            return [...m.slice(0, -1), { role: "assistant", content: res.reply }];
          }
          return m;
        });
      })
      .catch((err: Error) => {
        if (!isAbortError(err)) appendAssistant(`\n\n⚠️ ${err.message}`);
      })
      .finally(() => {
        setStreaming(false);
        abortRef.current = null;
      });
  }

  function appendAssistant(text: string) {
    setMessages((m) => {
      const last = m[m.length - 1];
      if (!last || last.role !== "assistant") return m;
      return [...m.slice(0, -1), { role: "assistant", content: last.content + text }];
    });
  }

  function stop() {
    abortRef.current?.();
    abortRef.current = null;
    setStreaming(false);
  }

  return (
    <div style={styles.root}>
      <header style={styles.header}>
        <strong>KubeAstra</strong>
        <span style={styles.status}>
          {!auth
            ? "Connecting…"
            : !auth.backendUrl
              ? "No backend — run “KubeAstra: Sign in”"
              : ready4Chat
                ? auth.backendUrl
                : "Sign-in required"}
        </span>
      </header>

      {!ready4Chat ? (
        <div style={styles.notice}>
          {auth && !auth.backendUrl
            ? "Set a backend URL and sign in from the Command Palette (“KubeAstra: Sign in”) to start."
            : "Sign in from the Command Palette (“KubeAstra: Sign in”) to start."}
        </div>
      ) : (
        <>
          <div ref={threadRef} style={styles.thread}>
            {messages.map((m, i) => (
              <div key={i} style={m.role === "user" ? styles.userMsg : styles.asstMsg}>
                {m.content || (streaming && i === messages.length - 1 ? "…" : "")}
              </div>
            ))}
            {streaming && trail.length > 0 && (
              <div style={styles.trail}>
                {trail.map((s, i) => (
                  <div key={i} style={styles.trailStep}>
                    {s.action ? `▸ ${s.action}` : s.thought ? `• ${s.thought}` : "…"}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div style={styles.composer}>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  send();
                }
              }}
              placeholder="Ask about your cluster…  (⌘/Ctrl+Enter to send)"
              rows={3}
              style={styles.textarea}
            />
            {streaming ? (
              <button onClick={stop} style={styles.button}>
                Stop
              </button>
            ) : (
              <button onClick={send} disabled={!canSend} style={styles.button}>
                Send
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  root: { display: "flex", flexDirection: "column", height: "100%" },
  header: {
    display: "flex",
    alignItems: "baseline",
    justifyContent: "space-between",
    gap: 8,
    padding: "8px 12px",
    borderBottom: "1px solid var(--vscode-panel-border)",
  },
  status: { fontSize: 11, opacity: 0.7, overflow: "hidden", textOverflow: "ellipsis" },
  notice: { padding: 16, opacity: 0.8, lineHeight: 1.5 },
  thread: { flex: 1, overflow: "auto", padding: 12, display: "flex", flexDirection: "column", gap: 8 },
  userMsg: {
    alignSelf: "flex-end",
    maxWidth: "90%",
    background: "var(--vscode-textBlockQuote-background)",
    borderRadius: 8,
    padding: "6px 10px",
    whiteSpace: "pre-wrap",
  },
  asstMsg: { alignSelf: "flex-start", maxWidth: "100%", whiteSpace: "pre-wrap", lineHeight: 1.5 },
  trail: {
    borderLeft: "2px solid var(--vscode-panel-border)",
    paddingLeft: 8,
    marginTop: 4,
    fontSize: 11,
    opacity: 0.7,
  },
  trailStep: { padding: "1px 0" },
  composer: { display: "flex", gap: 8, padding: 12, borderTop: "1px solid var(--vscode-panel-border)" },
  textarea: {
    flex: 1,
    resize: "none",
    background: "var(--vscode-input-background)",
    color: "var(--vscode-input-foreground)",
    border: "1px solid var(--vscode-input-border, var(--vscode-panel-border))",
    borderRadius: 6,
    padding: 8,
    fontFamily: "inherit",
    fontSize: 13,
  },
  button: {
    alignSelf: "flex-end",
    background: "var(--vscode-button-background)",
    color: "var(--vscode-button-foreground)",
    border: "none",
    borderRadius: 6,
    padding: "6px 14px",
    cursor: "pointer",
  },
};
