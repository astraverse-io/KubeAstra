"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  applyPendingWrite,
  discardPendingWrite,
  getPendingWrite,
  type ProposedWrite,
  type WriteValidationCheck,
} from "../lib/api";

/**
 * Approval card for a local file edit the desktop agent proposed
 * (`write_proposed` SSE event). The agent never writes: nothing touches disk
 * until the user clicks "Write to disk".
 *
 * The stream only carries a diff of the *sanitized* file (secrets redacted) —
 * that's what the model saw. The human must approve the exact bytes that will
 * be written, so the card fetches the real diff from the local desktop endpoint
 * and keeps "Write to disk" disabled until it has it. The real diff is never
 * stored in chat history.
 *
 * Validation is shown check by check; an edit where an applicable check was
 * skipped (e.g. kubeconform not installed) is stamped UNVALIDATED.
 */

type Phase =
  | { kind: "loading" }
  | { kind: "ready"; write: ProposedWrite }
  | { kind: "writing"; write: ProposedWrite }
  | { kind: "written"; path: string; created: boolean }
  | { kind: "discarded" }
  | { kind: "expired" }
  | { kind: "error"; message: string; write?: ProposedWrite };

const STATUS_STYLE: Record<WriteValidationCheck["status"], { mark: string; color: string }> = {
  pass: { mark: "✓", color: "var(--green)" },
  fail: { mark: "✗", color: "var(--red)" },
  warn: { mark: "!", color: "var(--amber)" },
  skipped: { mark: "–", color: "var(--ink-3)" },
};

function errorText(err: unknown): string {
  if (err instanceof ApiError || err instanceof Error) return err.message;
  return String(err);
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre
      aria-label="Exact change to be written"
      style={{
        margin: 0,
        padding: "0.5rem 0",
        maxHeight: "22rem",
        overflow: "auto",
        background: "var(--paper-2)",
        border: "1px solid var(--rule)",
        borderRadius: "0.5rem",
        fontFamily: "var(--mono)",
        fontSize: "0.72rem",
        lineHeight: 1.5,
      }}
    >
      {lines.map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---")
          ? "meta"
          : line.startsWith("@@")
            ? "hunk"
            : line.startsWith("+")
              ? "add"
              : line.startsWith("-")
                ? "del"
                : "ctx";
        const bg = kind === "add" ? "var(--green-bg)" : kind === "del" ? "var(--red-bg)" : "transparent";
        const color = kind === "meta" || kind === "hunk" ? "var(--ink-3)" : "var(--ink-2)";
        return (
          <div key={i} data-diff={kind} style={{ background: bg, color, padding: "0 0.75rem", whiteSpace: "pre" }}>
            {line || " "}
          </div>
        );
      })}
    </pre>
  );
}

export default function WriteApprovalCard({ proposal }: { proposal: ProposedWrite }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    getPendingWrite(proposal.token)
      .then((write) => { if (!cancelled) setPhase({ kind: "ready", write }); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) setPhase({ kind: "expired" });
        else setPhase({ kind: "error", message: errorText(err) });
      });
    return () => { cancelled = true; };
  }, [proposal.token]);

  const onWrite = useCallback(async () => {
    if (phase.kind !== "ready") return;
    const write = phase.write;
    setPhase({ kind: "writing", write });
    try {
      const res = await applyPendingWrite(write.token);
      setPhase({ kind: "written", path: res.path, created: res.created });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) setPhase({ kind: "expired" });
      else setPhase({ kind: "error", message: errorText(err), write });
    }
  }, [phase]);

  const onDiscard = useCallback(async () => {
    if (phase.kind !== "ready") return;
    try {
      await discardPendingWrite(phase.write.token);
    } catch {
      // Discard is best-effort: an expired token is already gone.
    }
    setPhase({ kind: "discarded" });
  }, [phase]);

  // Before the real diff arrives, show the proposal's metadata (from the event).
  const shown: ProposedWrite =
    phase.kind === "ready" || phase.kind === "writing" ? phase.write
      : phase.kind === "error" && phase.write ? phase.write
        : proposal;
  const v = shown.validation;
  const created = phase.kind === "written" ? phase.created : shown.created;
  const actionable = phase.kind === "loading" || phase.kind === "ready" || phase.kind === "writing";

  return (
    <section
      aria-label={`Proposed edit to ${proposal.path}`}
      style={{
        width: "100%",
        border: "1px solid var(--rule)",
        borderRadius: "0.75rem",
        padding: "0.75rem",
        background: "var(--paper-3)",
        display: "flex",
        flexDirection: "column",
        gap: "0.6rem",
      }}
    >
      <header style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
        <strong style={{ fontSize: "0.8125rem" }}>Proposed edit</strong>
        <code style={{ fontFamily: "var(--mono)", fontSize: "0.75rem" }}>{proposal.path}</code>
        {created && (
          <span style={{ fontSize: "0.7rem", color: "var(--ink-3)", border: "1px solid var(--rule)",
                         borderRadius: "999px", padding: "0 0.4rem" }}>new file</span>
        )}
        {actionable && (v.validated ? (
          <span style={{ fontSize: "0.7rem", color: "var(--green)" }}>✓ validated</span>
        ) : (
          <span
            title={v.unvalidated_reason ?? undefined}
            style={{ fontSize: "0.7rem", fontWeight: 700, letterSpacing: "0.04em", color: "var(--amber)",
                     background: "var(--amber-bg)", border: "1px solid var(--amber-bd)",
                     borderRadius: "0.25rem", padding: "0 0.4rem" }}
          >
            UNVALIDATED
          </span>
        ))}
      </header>

      {proposal.reason && (
        <p style={{ margin: 0, fontSize: "0.8125rem", color: "var(--ink-2)" }}>{proposal.reason}</p>
      )}

      {actionable && (
        <>
          {!v.validated && v.unvalidated_reason && (
            <p style={{ margin: 0, fontSize: "0.75rem", color: "var(--amber)" }}>
              Not fully checked: {v.unvalidated_reason}
            </p>
          )}
          <ul style={{ margin: 0, padding: 0, listStyle: "none", fontSize: "0.75rem",
                       display: "flex", flexDirection: "column", gap: "0.15rem" }}>
            {v.checks.map((c) => {
              const s = STATUS_STYLE[c.status] ?? STATUS_STYLE.skipped;
              return (
                <li key={c.name} style={{ display: "flex", gap: "0.4rem" }}>
                  <span aria-label={c.status} style={{ color: s.color, width: "1ch" }}>{s.mark}</span>
                  <span style={{ fontFamily: "var(--mono)" }}>{c.name}</span>
                  {c.detail && <span style={{ color: "var(--ink-3)" }}>— {c.detail}</span>}
                </li>
              );
            })}
          </ul>
          {phase.kind === "loading" ? (
            <p style={{ margin: 0, fontSize: "0.75rem", color: "var(--ink-3)" }}>Loading the exact change…</p>
          ) : (
            <DiffView diff={shown.diff} />
          )}
          <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
            <button type="button" onClick={onWrite} disabled={phase.kind !== "ready"}>
              {phase.kind === "writing" ? "Writing…" : "Write to disk"}
            </button>
            <button type="button" onClick={onDiscard} disabled={phase.kind !== "ready"}>
              Discard
            </button>
            <span style={{ fontSize: "0.7rem", color: "var(--ink-3)" }}>
              Nothing is written until you approve. Git is left to you.
            </span>
          </div>
        </>
      )}

      {phase.kind === "written" && (
        <p role="status" style={{ margin: 0, fontSize: "0.8125rem", color: "var(--green)" }}>
          {phase.created ? "Created" : "Written to"} {phase.path}. Review it with <code>git diff</code> and
          commit when you&apos;re ready.
        </p>
      )}
      {phase.kind === "discarded" && (
        <p role="status" style={{ margin: 0, fontSize: "0.8125rem", color: "var(--ink-3)" }}>
          Discarded. Nothing was written.
        </p>
      )}
      {phase.kind === "expired" && (
        <p role="status" style={{ margin: 0, fontSize: "0.8125rem", color: "var(--ink-3)" }}>
          This proposal expired or was already handled. Nothing was written — ask the agent to propose it again.
        </p>
      )}
      {phase.kind === "error" && (
        <p role="alert" style={{ margin: 0, fontSize: "0.8125rem", color: "var(--red)" }}>
          {phase.message}
        </p>
      )}
    </section>
  );
}
