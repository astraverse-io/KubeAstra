"use client";

import React from "react";

export type ToolCardStatus = "pending" | "running" | "done" | "failed";

export type ToolCardStep = {
  tool: string;
  cmd: string;
  output?: string[];
  summary?: string;
  duration?: string;
  status: ToolCardStatus;
};

type ToolCardProps = {
  step: ToolCardStep;
  idx: number;
  expanded?: boolean;
  onToggle?: () => void;
};

function colorForLine(line: string): string {
  const lower = line.toLowerCase();
  if (line.includes("<--") || lower.includes("fatal") || lower.includes("killed") || lower.includes("error")) {
    return "var(--red)";
  }
  if (lower.includes("warning") || lower.includes("warn")) {
    return "var(--amber)";
  }
  return "var(--ink, var(--fg-1))";
}

/**
 * Terminal-style card for a single tool call inside an investigation.
 * Expandable when done — shows the command + output block. Uses the
 * mission-control token palette, but falls back to --paper/--rule/--brand
 * so it stays readable in light/dark themes too.
 */
export function ToolCard({ step, idx, expanded = false, onToggle }: ToolCardProps) {
  const { tool, cmd, output = [], summary, duration, status } = step;
  const isDone = status === "done";
  const isRunning = status === "running";
  const isPending = status === "pending";
  const isFailed = status === "failed";
  const statusColor = isFailed
    ? "var(--red)"
    : isDone
      ? "var(--green)"
      : isRunning
        ? "var(--cyan, var(--brand))"
        : "var(--ink-4, var(--fg-4))";
  const isToggleable = isDone || isFailed;

  return (
    <div
      style={{
        background: "var(--bg-1, var(--paper-2))",
        border: `1px solid ${isRunning ? "var(--cyan-bd, var(--brand-bd))" : "var(--line, var(--rule))"}`,
        borderRadius: 6,
        overflow: "hidden",
        transition: "border-color 0.25s, box-shadow 0.25s, opacity 0.25s",
        opacity: isPending ? 0.5 : 1,
        animation: "mcFadeIn 0.3s ease both",
        boxShadow: isRunning
          ? "0 0 0 1px var(--cyan-bd, var(--brand-bd)), 0 0 24px rgba(94,234,212,0.08)"
          : "none",
      }}
    >
      <button
        type="button"
        onClick={() => isToggleable && onToggle?.()}
        disabled={!isToggleable}
        aria-expanded={isToggleable ? expanded : undefined}
        aria-label={`Tool step ${idx + 1}: ${tool}${summary ? ` — ${summary}` : ""}`}
        style={{
          width: "100%",
          padding: "8px 12px",
          background: "transparent",
          border: "none",
          cursor: isToggleable ? "pointer" : "default",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderBottom: expanded ? "1px solid var(--line, var(--rule))" : "none",
          color: "inherit",
          textAlign: "left",
        }}
      >
        <span style={{ position: "relative", width: 8, height: 8, flexShrink: 0 }} aria-hidden="true">
          <span
            style={{
              display: "block",
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: statusColor,
              animation: isRunning ? "pulseRing 0.9s ease-in-out infinite" : "none",
            }}
          />
          {isRunning && (
            <span
              style={{
                position: "absolute",
                inset: -3,
                borderRadius: "50%",
                border: "1px solid var(--cyan, var(--brand))",
                animation: "mcRingExpand 1.6s ease-out infinite",
              }}
            />
          )}
        </span>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 10,
            color: "var(--ink-4, var(--fg-4))",
          }}
        >
          [{String(idx + 1).padStart(2, "0")}]
        </span>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 12,
            fontWeight: 600,
            color: "var(--cyan, var(--brand))",
          }}
        >
          {tool}
        </span>
        <span
          aria-hidden="true"
          style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3, var(--fg-3))" }}
        >
          ::
        </span>
        <span style={{ flex: 1, minWidth: 0, overflow: "hidden" }}>
          {isDone && summary && (
            <span
              style={{
                display: "block",
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--ink, var(--fg-1))",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {summary}
            </span>
          )}
          {isRunning && (
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--cyan, var(--brand))",
              }}
            >
              executing<span className="mc-caret" />
            </span>
          )}
          {isPending && (
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--ink-4, var(--fg-4))",
              }}
            >
              queued
            </span>
          )}
          {isFailed && (
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: "var(--red)",
              }}
            >
              {summary ?? "failed"}
            </span>
          )}
        </span>
        {(isDone || isFailed) && duration && (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 10,
              color: "var(--ink-3, var(--fg-3))",
            }}
          >
            {duration}
          </span>
        )}
        {isToggleable && (
          <span
            aria-hidden="true"
            style={{
              fontFamily: "var(--mono)",
              fontSize: 10,
              color: "var(--ink-3, var(--fg-3))",
              transform: expanded ? "rotate(90deg)" : "none",
              transition: "transform 0.2s",
              display: "inline-block",
            }}
          >
            ›
          </span>
        )}
      </button>
      {expanded && isToggleable && (
        <div style={{ padding: "10px 14px 12px", background: "var(--bg-0, var(--paper))" }}>
          <div
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              color: "var(--cyan, var(--brand))",
              marginBottom: 8,
              opacity: 0.9,
              wordBreak: "break-all",
            }}
          >
            <span style={{ color: "var(--ink-3, var(--fg-3))" }}>$ </span>
            {cmd}
          </div>
          {output.map((line, i) => (
            <div
              key={i}
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                lineHeight: 1.7,
                color: colorForLine(line),
                paddingLeft: 12,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {line}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default ToolCard;
