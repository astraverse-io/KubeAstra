"use client";

import React, { useState } from "react";
import type { ClusterStatus } from "../lib/api";

export type MissionControlSession = { id: string; title: string; timestamp: number };

type MissionControlLeftRailProps = {
  sessions: MissionControlSession[];
  currentSessionId: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
  clusterStatus: ClusterStatus | null;
  onEditCluster?: () => void;
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
  clusterStatus,
  onEditCluster,
}: MissionControlLeftRailProps) {
  const connected = !!clusterStatus?.connected;

  return (
    <aside
      aria-label="Sessions and cluster context"
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
        <Section title="Sessions" count={String(sessions.length)}>
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
                no prior sessions
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

        <Section title="Cluster" count={connected ? "online" : "offline"}>
          <div style={{ padding: "6px 14px 14px" }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginBottom: 8,
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: 4,
                  background: connected ? "var(--green)" : "var(--ink-4, var(--fg-4))",
                  boxShadow: connected ? "0 0 8px var(--green)" : "none",
                }}
              />
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 11,
                  fontWeight: 600,
                  color: connected ? "var(--green)" : "var(--ink-3, var(--fg-3))",
                }}
              >
                {connected ? "connected" : "not connected"}
              </span>
            </div>

            <MetaRow label="cluster" value={clusterStatus?.cluster_name ?? "—"} />
            <MetaRow label="context" value={clusterStatus?.context_name ?? "—"} />
            <MetaRow label="namespace" value={clusterStatus?.namespace ?? "default"} />
            {clusterStatus?.mode && <MetaRow label="mode" value={clusterStatus.mode} />}

            {onEditCluster && (
              <button
                type="button"
                onClick={onEditCluster}
                style={{
                  marginTop: 10,
                  width: "100%",
                  padding: "6px 8px",
                  background: "transparent",
                  border: "1px solid var(--line-2, var(--rule-2))",
                  color: "var(--ink-2, var(--fg-2))",
                  fontFamily: "var(--mono)",
                  fontSize: 10,
                  letterSpacing: "0.05em",
                  borderRadius: 4,
                  cursor: "pointer",
                  transition: "border-color 0.15s, color 0.15s",
                }}
              >
                {connected ? "SWITCH CONTEXT" : "CONNECT CLUSTER"}
              </button>
            )}
          </div>
        </Section>
      </div>
    </aside>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "baseline",
        gap: 8,
        padding: "3px 0",
        borderBottom: "1px dashed var(--line, var(--rule))",
      }}
    >
      <span
        style={{
          fontFamily: "var(--mono)",
          fontSize: 9,
          textTransform: "uppercase",
          letterSpacing: "0.10em",
          color: "var(--ink-3, var(--fg-3))",
          minWidth: 62,
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontFamily: "var(--mono)",
          fontSize: 10,
          color: "var(--ink-2, var(--fg-2))",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

export default MissionControlLeftRail;
