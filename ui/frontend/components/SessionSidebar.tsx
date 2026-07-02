import React from "react";

interface SessionSidebarProps {
  isOpen: boolean;
  onClose: () => void;
  sessions: { id: string; title: string; timestamp: number }[];
  currentSessionId: string;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onDeleteSession: (id: string) => void;
}

export function SessionSidebar({
  isOpen,
  onClose,
  sessions,
  currentSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: SessionSidebarProps) {
  if (!isOpen) return null;

  return (
    <div
      style={{
        width: "16rem",
        flexShrink: 0,
        height: "100vh",
        background: "var(--paper-2)",
        borderRight: "1px solid var(--rule)",
        display: "flex",
        flexDirection: "column",
        animation: "slideRight 0.25s ease-out both",
        position: "relative",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "1rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: "1px solid var(--rule)",
        }}
      >
        <span style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--ink)" }}>History</span>
        <button
          onClick={onClose}
          style={{ background: "none", border: "none", color: "var(--ink-3)", cursor: "pointer" }}
        >
          &times;
        </button>
      </div>

      {/* New chat button */}
      <div style={{ padding: "1rem" }}>
        <button
          onClick={onNewSession}
          style={{
            width: "100%",
            padding: "0.5rem",
            background: "var(--brand-bg)",
            border: "1px solid var(--brand-bd)",
            color: "var(--brand)",
            borderRadius: "0.5rem",
            fontSize: "0.875rem",
            fontWeight: 500,
            cursor: "pointer",
            transition: "all 0.15s",
          }}
        >
          + New chat
        </button>
      </div>

      {/* Session list */}
      <div style={{ flex: 1, overflowY: "auto", padding: "0 1rem 1rem 1rem", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {sessions.map((s) => {
          const isActive = s.id === currentSessionId;
          return (
            <div
              key={s.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "0.5rem 0.75rem",
                background: isActive ? "var(--paper-3)" : "transparent",
                border: `1px solid ${isActive ? "var(--rule)" : "transparent"}`,
                borderRadius: "0.5rem",
                cursor: "pointer",
                transition: "all 0.15s",
              }}
              onClick={() => onSelectSession(s.id)}
            >
              <div style={{ display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
                <span
                  style={{
                    fontSize: "0.875rem",
                    color: isActive ? "var(--ink)" : "var(--ink-2)",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {s.title}
                </span>
                <span style={{ fontSize: "0.75rem", color: "var(--ink-4)", fontFamily: "var(--mono)" }}>
                  {new Date(s.timestamp).toLocaleDateString()}
                </span>
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onDeleteSession(s.id);
                }}
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--danger)",
                  cursor: "pointer",
                  fontSize: "1rem",
                  padding: "0 0.25rem",
                  opacity: 0.6,
                }}
              >
                &times;
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
