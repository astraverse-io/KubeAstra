import React from "react";
import { CodeBlock } from "./ResultCard";

export interface YamlProposerProps {
  yamlText: string;
  onApply: (yaml: string) => void;
}

export function YamlProposer({ yamlText, onApply }: YamlProposerProps) {
  return (
    <div style={{
      border: "1px solid var(--rule)",
      borderRadius: "12px",
      overflow: "hidden",
      background: "var(--paper-2)",
      marginTop: "16px",
      marginBottom: "16px",
      boxShadow: "0 2px 8px rgba(0,0,0,0.1)",
    }}>
      <div style={{
        padding: "12px 16px",
        background: "var(--paper-3)",
        borderBottom: "1px solid var(--rule)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between"
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" strokeWidth="2.5" strokeLinecap="round">
            <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
            <polyline points="2 17 12 22 22 17"></polyline>
            <polyline points="2 12 12 17 22 12"></polyline>
          </svg>
          <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--ink)", letterSpacing: "0.02em" }}>
            Proposed Fix (YAML)
          </span>
        </div>
        <button
          onClick={() => onApply(yamlText)}
          className="app-btn-primary"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "6px",
            padding: "6px 12px",
            fontSize: "12px",
            background: "var(--brand)",
            color: "#fff",
            border: "none",
            borderRadius: "6px",
            cursor: "pointer",
            fontWeight: 500,
            transition: "opacity 0.2s"
          }}
          onMouseEnter={(e) => (e.currentTarget.style.opacity = "0.9")}
          onMouseLeave={(e) => (e.currentTarget.style.opacity = "1")}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <path d="M22 4L12 14.01l-3-3" />
          </svg>
          Apply Fix
        </button>
      </div>
      <div style={{ maxHeight: "300px", overflowY: "auto" }}>
        <CodeBlock code={yamlText} />
      </div>
    </div>
  );
}
