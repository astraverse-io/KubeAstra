import React, { useState } from "react";
import { SlideToConfirm } from "./SlideToConfirm";

export interface ApprovalOverlayProps {
  onClose: () => void;
  onConfirm: () => void;
  commandInfo: {
    command: string;
    explanation?: string;
    stdin?: string;
  };
  contextName?: string;
}

export function ApprovalOverlay({ onClose, onConfirm, commandInfo, contextName }: ApprovalOverlayProps) {
  const [confirmed, setConfirmed] = useState(false);

  const handleConfirm = () => {
    setConfirmed(true);
    // Add a small delay so the user sees the confirmed state before it executes and closes
    setTimeout(() => {
      onConfirm();
    }, 1200);
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 100,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(0,0,0,0.65)", backdropFilter: "blur(8px)",
      animation: "overlayIn 0.25s ease both",
      padding: "1rem"
    }}>
      <div style={{
        width: "540px", maxWidth: "100%", maxHeight: "88vh",
        background: "var(--paper-2)",
        border: "1px solid var(--rule)",
        borderRadius: "16px", overflow: "hidden",
        boxShadow: "0 32px 80px rgba(0,0,0,0.6), 0 0 0 1px rgba(255,255,255,0.04), inset 0 1px 0 rgba(255,255,255,0.03)",
        animation: "modalSpring 0.4s cubic-bezier(0.34,1.56,0.64,1) both",
        display: "flex", flexDirection: "column",
      }}>
        {/* Header */}
        <div style={{
          padding: "14px 18px", background: "var(--amber-bg)",
          borderBottom: "1px solid var(--amber-bd)",
          display: "flex", alignItems: "center", gap: "10px", flexShrink: 0,
        }}>
          <div style={{
            width: "32px", height: "32px", borderRadius: "8px",
            background: "rgba(251,191,36,0.18)", border: "1px solid var(--amber-bd)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
              stroke="var(--amber)" strokeWidth="2.5" strokeLinecap="round">
              <rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0110 0v4" />
            </svg>
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--ink)" }}>
              Execution Approval Required
            </div>
            <div style={{ fontSize: "10px", color: "var(--amber)", fontFamily: "var(--mono)", marginTop: "1px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {contextName || "Local"} · Recovery command
            </div>
          </div>
          <button onClick={onClose} style={{
            background: "none", border: "none", cursor: "pointer",
            color: "var(--ink-3)", padding: "4px", borderRadius: "5px", flexShrink: 0
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "16px 18px", display: "flex", flexDirection: "column", gap: "14px" }}>
          
          <div style={{ fontSize: "13px", color: "var(--ink-2)", lineHeight: 1.5 }}>
            {commandInfo.explanation || "AI DevOps Assistant requires your approval to execute the following recovery command on your cluster."}
          </div>

          {/* Diff / Command View */}
          <div>
            <div style={{ fontSize: "10px", color: "var(--ink-3)", letterSpacing: "0.12em", textTransform: "uppercase", fontWeight: 600, marginBottom: "8px" }}>
              Proposed Command
            </div>
            <div style={{
              border: "1px solid var(--rule)", borderRadius: "8px", overflow: "hidden",
              background: "var(--paper)",
            }}>
              <div style={{ padding: "10px 14px", fontFamily: "var(--mono)", fontSize: "11px", lineHeight: 1.9, color: "var(--ink)" }}>
                <code style={{ whiteSpace: "pre-wrap" }}>{commandInfo.command}</code>
              </div>
            </div>
          </div>

          {commandInfo.stdin && (
            <div>
              <div style={{ fontSize: "10px", color: "var(--ink-3)", letterSpacing: "0.12em", textTransform: "uppercase", fontWeight: 600, marginBottom: "8px" }}>
                Input Payload (YAML)
              </div>
              <div style={{
                border: "1px solid var(--rule)", borderRadius: "8px", overflow: "hidden",
                background: "var(--paper)", maxHeight: "200px", overflowY: "auto"
              }}>
                <div style={{ padding: "10px 14px", fontFamily: "var(--mono)", fontSize: "11px", lineHeight: 1.6, color: "var(--ink)" }}>
                  <code style={{ whiteSpace: "pre-wrap" }}>{commandInfo.stdin}</code>
                </div>
              </div>
            </div>
          )}

          {/* Slide */}
          {!confirmed ? (
            <div style={{ marginTop: "10px" }}>
              <SlideToConfirm 
                onConfirm={handleConfirm} 
                label="Slide to Confirm Execution" 
                confirmedLabel="✓ Fix Queued for Rollout"
              />
            </div>
          ) : (
            <div style={{
              padding: "18px", textAlign: "center", marginTop: "10px",
              background: "var(--brand-bg)", border: "1px solid var(--brand-bd)",
              borderRadius: "10px", animation: "springIn 0.35s ease both",
            }}>
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none"
                stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round"
                style={{ margin: "0 auto 10px", display: "block" }}>
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><path d="M22 4L12 14.01l-3-3" />
              </svg>
              <div style={{ fontSize: "13px", color: "var(--brand)", fontWeight: 600, marginBottom: "4px" }}>
                Fix Approved — Executing
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
