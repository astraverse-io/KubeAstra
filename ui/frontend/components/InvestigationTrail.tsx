import React, { useState, useEffect } from "react";

export interface ReactStep {
  thought?: string;
  action: string;
  params?: Record<string, unknown>;
  duration_ms?: number;
}

export interface InvestigationTrailProps {
  steps: ReactStep[];
  thinking: boolean;
}

const TOOL_COLORS: Record<string, { bg: string; text: string; dot: string }> = {
  kubectl: { bg: "rgba(96,165,250,0.14)", text: "#93C5FD", dot: "#60A5FA" },
  prometheus: { bg: "rgba(251,191,36,0.14)", text: "#FCD34D", dot: "#FBBF24" },
  logs: { bg: "rgba(34,211,238,0.14)", text: "#67E8F9", dot: "#22D3EE" },
  events: { bg: "rgba(167,139,250,0.16)", text: "#C4B5FD", dot: "#A78BFA" },
  topology: { bg: "rgba(74,222,128,0.14)", text: "#86EFAC", dot: "#4ADE80" },
};

function ToolPing({ tool, status, delay = 0, label }: { tool: string; status: "pending" | "running" | "done"; delay?: number, label: string }) {
  // If we don't have a specific color, use gray. But we can map common ones.
  // We'll map backend tool names to UI tool names.
  let uiTool = "tool";
  if (tool.includes("pod") || tool.includes("deployment")) uiTool = "kubectl";
  if (tool.includes("event")) uiTool = "events";
  if (tool.includes("log")) uiTool = "logs";
  if (tool.includes("graph") || tool.includes("workload")) uiTool = "topology";
  if (tool.includes("analyze")) uiTool = "analyze";

  const tc = TOOL_COLORS[uiTool] || { bg: "#F1F5F9", text: "#475569", dot: "#94A3B8" };

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: "8px",
      animation: `springIn 0.35s cubic-bezier(0.34,1.56,0.64,1) ${delay}ms both`,
    }}>
      {/* Dot */}
      <div style={{ position: "relative", width: "8px", height: "8px", flexShrink: 0 }}>
        <div style={{
          width: "8px", height: "8px", borderRadius: "50%",
          background: status === "pending" ? "var(--rule-2)" : tc.dot,
          boxShadow: status === "running" ? `0 0 0 2px ${tc.dot}40` : "none",
          animation: status === "running" ? "blink 1s ease-in-out infinite" : "none",
        }} />
        {status === "running" && (
          <div style={{
            position: "absolute", inset: -3, borderRadius: "50%",
            border: `1.5px solid ${tc.dot}`,
            animation: "pulseRing 1.4s ease-out infinite",
          }} />
        )}
      </div>
      {/* Badge */}
      <span style={{
        fontSize: "10px", fontFamily: "var(--mono)", fontWeight: 500,
        background: status === "pending" ? "rgba(255,255,255,0.03)" : tc.bg,
        color: status === "pending" ? "var(--ink-4)" : tc.text,
        border: `1px solid ${status === "pending" ? "var(--rule)" : tc.dot + "55"}`,
        borderRadius: "4px", padding: "1px 7px", letterSpacing: "0.03em",
        transition: "all 0.3s ease",
      }}>
        {tool}
      </span>
      {/* Result */}
      {(status === "done" || (status === "running" && label)) && (
        <span style={{ fontSize: "10px", color: "var(--ink-3)", fontFamily: "var(--mono)" }}>
          {label}
        </span>
      )}
      {status === "running" && !label && (
        <span style={{
          fontSize: "10px", color: tc.dot, fontFamily: "var(--mono)",
          animation: "blink 0.9s step-end infinite",
        }}>running…</span>
      )}
    </div>
  );
}

export function InvestigationTrail({ steps, thinking }: InvestigationTrailProps) {
  const [collapsed, setCollapsed] = useState(false);
  const doneCount = steps.filter(s => s.action !== "answer" && s.duration_ms !== undefined).length;
  // all tools that are not "answer"
  const toolSteps = steps.filter(s => s.action !== "answer");
  const total = toolSteps.length || 1;

  // Auto-collapse when thinking stops
  useEffect(() => {
    if (!thinking) {
      const id = window.setTimeout(() => setCollapsed(true), 0);
      return () => window.clearTimeout(id);
    }
  }, [thinking]);

  return (
    <div style={{
      background: "var(--paper-2)",
      border: "1px solid var(--rule)",
      borderRadius: "10px",
      overflow: "hidden",
      width: "100%",
      maxWidth: "500px",
      boxShadow: "0 2px 8px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.02)",
      animation: "springIn 0.4s cubic-bezier(0.34,1.56,0.64,1) both",
    }}>
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{
          padding: "9px 12px",
          display: "flex", alignItems: "center", gap: "8px",
          cursor: "pointer",
          borderBottom: collapsed ? "none" : "1px solid var(--rule)",
          background: "var(--paper-3)",
        }}
      >
        {/* AI DevOps Assistant mark / KubeAstra Emblem */}
        <svg width="14" height="14" viewBox="0 0 44 44" fill="none" style={{ flexShrink: 0 }}>
          <circle cx="22" cy="22" r="22" fill="var(--brand)" />
          <path d="M30 15H19a4 4 0 0 0 0 8h6a4 4 0 0 1 0 8H14" stroke="#0a0a0a" strokeWidth="3.5" strokeLinecap="round" fill="none" />
        </svg>
        <span style={{ fontSize: "11px", fontWeight: 600, color: "var(--brand)", letterSpacing: "0.04em" }}>
          Investigation Trail
        </span>
        <span style={{ fontSize: "10px", color: "var(--ink-3)", fontFamily: "var(--mono)" }}>
          {doneCount}/{total} tools
        </span>
        {thinking && (
          <div style={{ display: "flex", gap: "3px", alignItems: "center" }}>
            {[0, 1, 2].map(d => (
              <div key={d} style={{
                width: "3px", height: "3px", borderRadius: "50%", background: "var(--brand)",
                animation: `dotBounce 1.2s ${d * 0.2}s ease-in-out infinite`,
              }} />
            ))}
          </div>
        )}
        {/* Progress bar */}
        <div style={{
          flex: 1, height: "2px",
          background: "var(--rule)", borderRadius: "1px", overflow: "hidden", marginLeft: "4px",
        }}>
          <div style={{
            height: "100%", borderRadius: "1px",
            background: "var(--brand)",
            width: `${(doneCount / total) * 100}%`,
            transition: "width 0.5s ease",
          }} />
        </div>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round"
          style={{ transform: collapsed ? "rotate(-90deg)" : "none", transition: "transform 0.2s", flexShrink: 0 }}>
          <path d="M6 9l6 6 6-6" />
        </svg>
      </div>
      {!collapsed && (
        <div style={{ padding: "12px", display: "flex", flexDirection: "column", gap: "10px" }}>
          {toolSteps.map((s, i) => {
            const status = s.duration_ms !== undefined ? "done" : (thinking && i === toolSteps.length - 1 ? "running" : "pending");
            return (
              <ToolPing
                key={`${s.action}-${i}`}
                tool={s.action}
                status={status}
                delay={i * 60}
                label={s.thought || (status === "done" ? "Completed" : "")}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
