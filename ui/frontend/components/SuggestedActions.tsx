"use client";

export interface SuggestedAction {
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
}

/**
 * The first action that can actually be executed: a non-manual action with a
 * non-empty command. Manual follow-ups (e.g. "Trace source/config") carry a
 * follow_up_prompt and no command, so they must never be routed to executeCommand.
 */
export function firstExecutableAction(actions?: SuggestedAction[]): SuggestedAction | undefined {
  return actions?.find(
    (a) => a.action_kind !== "manual" && (a.command ?? "").trim() !== ""
  );
}

interface SuggestedActionsProps {
  actions: SuggestedAction[];
  /** Run/review an executable kubectl action. */
  onExecute: (action: SuggestedAction) => void;
  /** Submit a manual follow-up prompt as a new chat message. */
  onFollowUp: (prompt: string) => void;
}

/**
 * Renders the suggested-action buttons under an assistant answer.
 *
 * Two distinct kinds:
 *  - executable actions (write_command / apply_yaml) show the command in a
 *    <code> block and run through the execute pipeline.
 *  - manual actions (action_kind === "manual", e.g. "Trace source/config")
 *    carry a follow_up_prompt, render as a plain follow-up button, and are
 *    resubmitted as chat — they never call executeCommand.
 */
export function SuggestedActions({ actions, onExecute, onFollowUp }: SuggestedActionsProps) {
  if (!actions || actions.length === 0) return null;

  return (
    <div
      style={{ width: "100%", borderRadius: "1rem", padding: "0.75rem", display: "flex", flexDirection: "column", gap: "0.5rem", background: "var(--paper-2)", border: "1px solid var(--rule)" }}
    >
      <p style={{ fontSize: "0.75rem", fontWeight: 500, color: "var(--ink-2)", margin: 0 }}>
        Suggested actions
      </p>
      {actions.map((action, idx) =>
        action.action_kind === "manual" ? (
          <div key={action.follow_up_prompt ?? action.label ?? idx} style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <span style={{ flex: 1, fontSize: "0.75rem", color: "var(--ink-2)" }}>
              Find where this is defined in source/config.
            </span>
            <button
              onClick={() => action.follow_up_prompt && onFollowUp(action.follow_up_prompt)}
              className="sym-btn-ghost"
              style={{ flexShrink: 0, borderRadius: "0.5rem", padding: "0.25rem 0.75rem", fontSize: "0.75rem", fontWeight: 500, border: "1px solid var(--rule)" }}
            >
              {action.label || "Trace source/config"}
            </button>
          </div>
        ) : (
          <div key={action.command ?? idx} style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            <code
              style={{ flex: 1, borderRadius: "0.5rem", padding: "0.25rem 0.5rem", fontSize: "11px", overflowX: "auto", background: "var(--paper-3)", color: "var(--ink-2)" }}
            >
              {action.command}
            </code>
            <button
              onClick={() => onExecute(action)}
              className="sym-btn-primary"
              style={{ flexShrink: 0, borderRadius: "0.5rem", padding: "0.25rem 0.75rem", fontSize: "0.75rem", fontWeight: 500 }}
            >
              {action.confirm ? "Review and execute fix" : "Run"}
            </button>
          </div>
        )
      )}
    </div>
  );
}
