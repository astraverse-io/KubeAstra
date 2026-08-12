"use client";

import React from "react";
import type { AuditEvent } from "@/lib/api";

/**
 * One session, oldest first — the order things actually happened in.
 *
 * The dashboard answers "what happened lately". This answers "what happened
 * in this investigation, and in what order", which is the question asked
 * after something went wrong.
 *
 * Payloads are collapsed by default. They are the detail that matters when
 * you need it and noise when you do not, and an expanded payload per row
 * makes a twenty-step investigation unreadable.
 */

const SEVERITY_COLOUR: Record<string, string> = {
  critical: "var(--danger, #b3261e)",
  warn: "var(--warn, #a16207)",
  info: "var(--ink-3, #6b7280)",
};

function gapLabel(previous: AuditEvent | undefined, event: AuditEvent): string | null {
  if (!previous) return null;
  const delta = new Date(event.ts).getTime() - new Date(previous.ts).getTime();
  if (Number.isNaN(delta) || delta < 1000) return null;
  if (delta < 60_000) return `+${Math.round(delta / 1000)}s`;
  return `+${Math.round(delta / 60_000)}m`;
}

export default function AuditReplayTimeline({ events }: { events: AuditEvent[] }) {
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set());

  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (events.length === 0) {
    return (
      <p style={{ color: "var(--ink-3)", fontSize: "0.875rem" }}>
        Nothing auditable happened in this session.
      </p>
    );
  }

  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {events.map((event, index) => {
        const gap = gapLabel(events[index - 1], event);
        const isOpen = expanded.has(event.id);
        const hasPayload = event.payload && Object.keys(event.payload).length > 0;

        return (
          <li
            key={event.id}
            style={{
              borderLeft: `2px solid ${SEVERITY_COLOUR[event.severity] ?? "var(--rule)"}`,
              paddingLeft: "0.875rem",
              paddingBottom: "1rem",
              position: "relative",
            }}
          >
            {gap && (
              <div
                style={{
                  color: "var(--ink-3)",
                  fontSize: "0.6875rem",
                  fontFamily: "var(--font-mono, monospace)",
                  marginBottom: "0.25rem",
                }}
              >
                {gap}
              </div>
            )}

            <div style={{ display: "flex", gap: "0.5rem", alignItems: "baseline" }}>
              <span
                style={{
                  fontFamily: "var(--font-mono, monospace)",
                  fontSize: "0.8125rem",
                  color: SEVERITY_COLOUR[event.severity] ?? "inherit",
                }}
              >
                {event.event_type}
              </span>
              {event.subject && (
                <span style={{ fontSize: "0.8125rem" }}>{event.subject}</span>
              )}
            </div>

            <div style={{ color: "var(--ink-3)", fontSize: "0.75rem", marginTop: "0.125rem" }}>
              {event.actor_id} · {new Date(event.ts).toLocaleTimeString()}
              {event.cluster ? ` · ${event.cluster}` : ""}
            </div>

            {hasPayload && (
              <>
                <button
                  type="button"
                  onClick={() => toggle(event.id)}
                  style={{
                    marginTop: "0.375rem",
                    background: "none",
                    border: "none",
                    padding: 0,
                    color: "var(--ink-3)",
                    cursor: "pointer",
                    fontSize: "0.75rem",
                    fontFamily: "inherit",
                  }}
                >
                  {isOpen ? "▾ hide detail" : "▸ detail"}
                </button>
                {isOpen && (
                  <pre
                    style={{
                      marginTop: "0.375rem",
                      padding: "0.5rem",
                      background: "var(--surface-2, rgba(0,0,0,0.04))",
                      borderRadius: "0.375rem",
                      fontSize: "0.75rem",
                      overflowX: "auto",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}
                  >
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                )}
              </>
            )}
          </li>
        );
      })}
    </ol>
  );
}
