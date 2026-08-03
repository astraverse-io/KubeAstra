"use client";

import React, { useState } from "react";

export type MissionControlSession = { id: string; title: string; timestamp: number };

type MissionControlLeftRailProps = {
  sessions: MissionControlSession[];
  currentSessionId: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
};

type SectionProps = {
  title: string;
  count?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
};

function Section({ title, count, defaultOpen = true, children }: SectionProps) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ borderBottom: "1px solid var(--line, var(--rule))" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          width: "100%",
          padding: "10px 14px",
          background: "transparent",
          border: "none",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 8,
          color: "inherit",
          textAlign: "left",
        }}
      >
        <span
          aria-hidden="true"
          style={{
            fontFamily: "var(--mono)",
            fontSize: 10,
            color: "var(--ink-3, var(--fg-3))",
            transform: open ? "rotate(90deg)" : "none",
            transition: "transform 0.2s",
            display: "inline-block",
            width: 10,
          }}
        >
          ›
        </span>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.10em",
            color: "var(--ink-3, var(--fg-3))",
            flex: 1,
          }}
        >
          {title}
        </span>
        {count && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-4, var(--fg-4))" }}>
            {count}
          </span>
        )}
      </button>
      {open && children}
    </div>
  );
}

export function MissionControlLeftRail({
  sessions,
  currentSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: MissionControlLeftRailProps) {

  return (
    <aside
      aria-label="Investigations"
      className="mc-grid-bg"
      style={{
        width: 260,
        flexShrink: 0,
        background: "var(--bg-1, var(--paper-2))",
        borderRight: "1px solid var(--line, var(--rule))",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <div style={{ padding: 12, borderBottom: "1px solid var(--line, var(--rule))" }}>
        <button
          type="button"
          onClick={onNewSession}
          style={{
            width: "100%",
            padding: "8px 10px",
            background: "var(--cyan-bg, var(--brand-bg))",
            border: "1px solid var(--cyan-bd, var(--brand-bd))",
            color: "var(--cyan, var(--brand))",
            borderRadius: 5,
            fontFamily: "var(--mono)",
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.05em",
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 6,
            transition: "background 0.15s, box-shadow 0.15s",
          }}
        >
          <span aria-hidden="true">+</span>
          NEW INVESTIGATION
        </button>
      </div>

      <div style={{ flex: 1, overflowY: "auto" }}>
        <Section title="Investigations" count={String(sessions.length)}>
          <div style={{ padding: "4px 8px 12px" }}>
            {sessions.length === 0 && (
              <div
                style={{
                  padding: "10px 6px",
                  fontFamily: "var(--mono)",
                  fontSize: 10,
                  color: "var(--ink-4, var(--fg-4))",
                }}
              >
                no prior investigations
              </div>
            )}
            {sessions.map((s) => {
              const isActive = s.id === currentSessionId;
              return (
                <div
                  key={s.id}
                  onClick={() => onSelectSession(s.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelectSession(s.id);
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  aria-current={isActive ? "page" : undefined}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "6px 8px",
                    marginBottom: 2,
                    background: isActive ? "var(--bg-2, var(--paper-3))" : "transparent",
                    border: `1px solid ${isActive ? "var(--cyan-bd, var(--brand-bd))" : "transparent"}`,
                    borderRadius: 4,
                    cursor: "pointer",
                    transition: "background 0.15s, border-color 0.15s",
                  }}
                >
                  <div style={{ display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
                    <span
                      style={{
                        fontFamily: "var(--sans)",
                        fontSize: 12,
                        color: isActive ? "var(--ink, var(--fg-0))" : "var(--ink-2, var(--fg-2))",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {s.title || "untitled"}
                    </span>
                    <span
                      style={{
                        fontFamily: "var(--mono)",
                        fontSize: 9,
                        color: "var(--ink-4, var(--fg-4))",
                        marginTop: 2,
                      }}
                    >
                      {new Date(s.timestamp).toLocaleDateString()}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDeleteSession(s.id);
                    }}
                    aria-label={`Delete session ${s.title || s.id}`}
                    style={{
                      background: "transparent",
                      border: "none",
                      color: "var(--ink-4, var(--fg-4))",
                      cursor: "pointer",
                      fontSize: 14,
                      padding: "0 4px",
                      opacity: 0.6,
                      lineHeight: 1,
                    }}
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
        </Section>

        {/* The Cluster section that used to sit here is gone. It restated the
            header's target — connected, cluster, context, namespace, mode —
            and ended in a CONNECT CLUSTER button that opened the same popover
            the target block opens. Two places showing one fact is how they
            drift apart: this panel read from `clusterStatus` while the header
            pills read their own props, so a session connected over SSH showed
            "not connected" here and a host name up there.

            The rail is now only what it is named for: the investigations. */}
      </div>
    </aside>
  );
}


export default MissionControlLeftRail;
