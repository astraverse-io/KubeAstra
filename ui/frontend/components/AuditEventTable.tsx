"use client";

import React from "react";
import type { AuditEvent } from "@/lib/api";

/**
 * The dashboard table. One row per event, newest first.
 *
 * Severity is the only colour used. Everything here is a record of something
 * that already happened, so colouring by event type would decorate rather
 * than inform — the reader is scanning for the two or three rows that
 * changed a cluster.
 */

const SEVERITY_COLOUR: Record<string, string> = {
  critical: "var(--danger, #b3261e)",
  warn: "var(--warn, #a16207)",
  info: "var(--ink-3, #6b7280)",
};

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export default function AuditEventTable({
  events,
  onSelectSession,
}: {
  events: AuditEvent[];
  onSelectSession?: (sessionId: string) => void;
}) {
  if (events.length === 0) {
    return (
      <p style={{ color: "var(--ink-3)", fontSize: "0.875rem", padding: "1rem 0" }}>
        No events match these filters.
      </p>
    );
  }

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8125rem" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--ink-3)" }}>
            <th style={{ padding: "0.5rem 0.75rem" }}>When</th>
            <th style={{ padding: "0.5rem 0.75rem" }}>Event</th>
            <th style={{ padding: "0.5rem 0.75rem" }}>Actor</th>
            <th style={{ padding: "0.5rem 0.75rem" }}>Cluster</th>
            <th style={{ padding: "0.5rem 0.75rem" }}>Subject</th>
            <th style={{ padding: "0.5rem 0.75rem" }}>Session</th>
          </tr>
        </thead>
        <tbody>
          {events.map((event) => (
            <tr key={event.id} style={{ borderTop: "1px solid var(--rule)" }}>
              <td
                style={{ padding: "0.5rem 0.75rem", whiteSpace: "nowrap" }}
                title={event.ts}
              >
                {relativeTime(event.ts)}
              </td>
              <td style={{ padding: "0.5rem 0.75rem" }}>
                <span
                  style={{
                    color: SEVERITY_COLOUR[event.severity] ?? SEVERITY_COLOUR.info,
                    fontFamily: "var(--font-mono, monospace)",
                  }}
                >
                  {event.event_type}
                </span>
              </td>
              <td style={{ padding: "0.5rem 0.75rem" }}>
                {event.actor_id}
                <span style={{ color: "var(--ink-3)" }}> ({event.actor_type})</span>
              </td>
              <td style={{ padding: "0.5rem 0.75rem" }}>{event.cluster ?? "—"}</td>
              <td style={{ padding: "0.5rem 0.75rem" }}>{event.subject ?? "—"}</td>
              <td style={{ padding: "0.5rem 0.75rem" }}>
                {event.session_id ? (
                  <button
                    type="button"
                    onClick={() => onSelectSession?.(event.session_id as string)}
                    style={{
                      background: "none",
                      border: "none",
                      padding: 0,
                      color: "var(--accent, #2563eb)",
                      cursor: "pointer",
                      fontFamily: "inherit",
                      fontSize: "inherit",
                    }}
                  >
                    {event.session_id.slice(0, 8)}…
                  </button>
                ) : (
                  "—"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
