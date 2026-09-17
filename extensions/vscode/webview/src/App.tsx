import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import ReactMarkdown from "react-markdown";
import type { ChatMessage, ChatStreamEvent, ChatResponse, ClusterStatus } from "@lib/api";
import type { ReactStep } from "@components/InvestigationTrail";
import { MissionControlHeader } from "@components/MissionControlHeader";
import { MissionControlToolTrail } from "@components/MissionControlToolTrail";
import { MissionControlDiagnosis } from "@components/MissionControlDiagnosis";
import { MissionControlApprovalOverlay } from "@components/MissionControlApprovalOverlay";
import { CommandBar } from "@components/CommandBar";
import { resultToMissionControlDiagnosis } from "@lib/missionControlAdapters";
import { onHostMessage, ready, sendChatStream, executeCommand, isAbortError } from "./webviewApi";

interface AuthState {
  signedIn: boolean;
  backendUrl: string | null;
  authRequired: boolean;
}

interface Turn {
  id: number;
  user: string;
  steps: ReactStep[];
  reply: string;
  result: ChatResponse | null;
}

type ApprovableAction = NonNullable<ChatResponse["suggested_actions"]>[number];

/** The first suggested action that mutates the cluster, if any. */
function approvableAction(result: ChatResponse | null): ApprovableAction | null {
  const actions = result?.suggested_actions ?? [];
  return (
    actions.find(
      (a) => a.requires_approval || a.action_kind === "write_command" || a.action_kind === "apply_yaml",
    ) ?? null
  );
}

/**
 * M3: the slim chat, now composed from the real Mission Control components
 * imported in place from ui/frontend/components (MissionControlHeader,
 * MissionControlToolTrail, MissionControlDiagnosis, CommandBar,
 * MissionControlApprovalOverlay) — the same source the Next app renders. The
 * ReAct step stream feeds the ToolTrail; the final result feeds the Diagnosis
 * card via the shared resultToMissionControlDiagnosis adapter.
 */
export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [approval, setApproval] = useState<{ action: ApprovableAction; turnId: number } | null>(null);
  const abortRef = useRef<(() => void) | null>(null);
  const mainRef = useRef<HTMLElement>(null);
  const nextId = useRef(0);

  useEffect(() => {
    const off = onHostMessage((msg) => {
      if (msg.type === "auth-state") {
        setAuth({ signedIn: msg.signedIn, backendUrl: msg.backendUrl, authRequired: msg.authRequired });
      } else if (msg.type === "prompt") {
        send(msg.text); // investigate-file / investigate-selection run immediately
      }
    });
    ready();
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    mainRef.current?.scrollTo({ top: mainRef.current.scrollHeight });
  }, [turns]);

  const ready4Chat = !!auth?.backendUrl && (auth.signedIn || !auth.authRequired);
  const cluster: ClusterStatus = { connected: ready4Chat };

  function updateLastTurn(fn: (t: Turn) => Turn) {
    setTurns((ts) => (ts.length === 0 ? ts : [...ts.slice(0, -1), fn(ts[ts.length - 1])]));
  }

  function send(text: string) {
    const message = text.trim();
    if (!message || streaming) return;
    const history: ChatMessage[] = turns.flatMap((t) => [
      { role: "user", content: t.user },
      { role: "assistant", content: t.reply },
    ]);
    setTurns((ts) => [...ts, { id: nextId.current++, user: message, steps: [], reply: "", result: null }]);
    setStreaming(true);

    const onEvent = (evt: ChatStreamEvent) => {
      if (evt.type === "iteration_planned") {
        updateLastTurn((t) => ({
          ...t,
          steps: [...t.steps, { action: evt.action ?? "step", thought: evt.thought, params: evt.params }],
        }));
      } else if (evt.type === "step_complete") {
        updateLastTurn((t) => {
          if (t.steps.length === 0) return t;
          const steps = [...t.steps];
          steps[steps.length - 1] = { ...steps[steps.length - 1], duration_ms: evt.duration_ms };
          return { ...t, steps };
        });
      } else if (evt.type === "token" && evt.text) {
        updateLastTurn((t) => ({ ...t, reply: t.reply + evt.text }));
      }
    };

    const { result, abort } = sendChatStream(message, history, onEvent);
    abortRef.current = abort;
    result
      .then((res) => updateLastTurn((t) => ({ ...t, result: res, reply: t.reply || res.reply })))
      .catch((err: Error) => {
        if (!isAbortError(err)) updateLastTurn((t) => ({ ...t, reply: `${t.reply}\n\n⚠️ ${err.message}` }));
      })
      .finally(() => {
        setStreaming(false);
        abortRef.current = null;
      });
  }

  function stop() {
    abortRef.current?.();
  }

  async function confirmApproval() {
    if (!approval?.action.command) {
      setApproval(null);
      return;
    }
    const { action, turnId } = approval;
    setApproval(null);
    try {
      const res = await executeCommand(action.command!, true);
      const note = res.success ? `✅ Ran \`${action.command}\`` : `⚠️ ${res.error || "command failed"}`;
      appendToTurn(turnId, `\n\n${note}`);
    } catch (err) {
      appendToTurn(turnId, `\n\n⚠️ ${(err as Error).message}`);
    }
  }

  function appendToTurn(turnId: number, text: string) {
    setTurns((ts) => ts.map((t) => (t.id === turnId ? { ...t, reply: t.reply + text } : t)));
  }

  if (!ready4Chat) {
    return (
      <div style={styles.root}>
        <div style={styles.notice}>
          {auth && !auth.backendUrl
            ? "Set a backend URL and sign in from the Command Palette (“KubeAstra: Sign in”) to start."
            : "Sign in from the Command Palette (“KubeAstra: Sign in”) to start."}
        </div>
      </div>
    );
  }

  return (
    <div style={styles.root}>
      <MissionControlHeader clusterStatus={cluster} busy={streaming} />

      <main ref={mainRef} style={styles.main}>
        {turns.map((turn, i) => {
          const last = i === turns.length - 1;
          const diagnosis = resultToMissionControlDiagnosis(turn.result?.result);
          const action = approvableAction(turn.result);
          return (
            <div key={turn.id} style={styles.turn}>
              <UserQueryBubble text={turn.user} />
              <MissionControlToolTrail steps={turn.steps} thinking={streaming && last && !turn.result} />
              {diagnosis ? (
                <MissionControlDiagnosis
                  {...diagnosis}
                  onAuthorize={action ? () => setApproval({ action, turnId: turn.id }) : undefined}
                  authorizeLabel={action?.label}
                />
              ) : (
                turn.reply && (
                  <div style={styles.reply}>
                    <ReactMarkdown>{turn.reply}</ReactMarkdown>
                  </div>
                )
              )}
            </div>
          );
        })}
      </main>

      <CommandBar
        onSend={send}
        busy={streaming}
        clusterConnected={ready4Chat}
        placeholder="Ask about your cluster…"
      />
      {streaming && (
        <button onClick={stop} style={styles.stop}>
          Stop
        </button>
      )}

      {approval && (
        <MissionControlApprovalOverlay
          title={approval.action.label}
          executionCommand={approval.action.command}
          onClose={() => setApproval(null)}
          onConfirm={confirmApproval}
        />
      )}
    </div>
  );
}

function UserQueryBubble({ text }: { text: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "flex-end" }}>
      <div style={styles.userBubble}>
        <span aria-hidden="true" style={{ color: "var(--cyan, var(--brand))", marginRight: 6 }}>
          you›
        </span>
        {text}
      </div>
    </div>
  );
}

const styles: Record<string, CSSProperties> = {
  root: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    background: "var(--bg-0, var(--paper))",
    color: "var(--ink, var(--fg-0))",
  },
  main: { flex: 1, overflowY: "auto", padding: "16px 18px", display: "flex", flexDirection: "column", gap: 20 },
  turn: { display: "flex", flexDirection: "column", gap: 12 },
  reply: {
    fontFamily: "var(--sans)",
    fontSize: 13,
    lineHeight: 1.6,
    color: "var(--ink-2, var(--fg-1))",
  },
  userBubble: {
    display: "inline-flex",
    maxWidth: "80%",
    padding: "8px 12px",
    background: "var(--cyan-bg, var(--brand-bg))",
    border: "1px solid var(--cyan-bd, var(--brand-bd))",
    borderRadius: 6,
    fontFamily: "var(--mono)",
    fontSize: 12,
    color: "var(--ink, var(--fg-0))",
    lineHeight: 1.5,
    whiteSpace: "pre-wrap",
  },
  notice: { padding: 16, opacity: 0.8, lineHeight: 1.5 },
  stop: {
    position: "absolute",
    bottom: 60,
    right: 16,
    background: "var(--vscode-button-secondaryBackground, var(--red-bg))",
    color: "var(--vscode-button-secondaryForeground, var(--red))",
    border: "1px solid var(--red-bd)",
    borderRadius: 6,
    padding: "4px 12px",
    cursor: "pointer",
  },
};
