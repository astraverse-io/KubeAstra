"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { HoldToAuthorize } from "./HoldToAuthorize";
import type { DiagnosisDiffLine } from "./MissionControlDiagnosis";

export type ApprovalCoordinate = { label: string; value: string };
export type ApprovalImpact = { label: string; before?: string; after: string };

type MissionControlApprovalOverlayProps = {
  title: string;
  subtitle?: string;
  coordinates?: ApprovalCoordinate[];
  impacts?: ApprovalImpact[];
  preflightChecks?: string[];
  diffFileHeader?: string;
  diffLines?: DiagnosisDiffLine[];
  diffAdditions?: number;
  diffDeletions?: number;
  executionCommand?: string;
  onClose: () => void;
  onConfirm: () => void;
};

export function MissionControlApprovalOverlay({
  title,
  subtitle = "Mission brief · approval required",
  coordinates = [],
  impacts = [],
  preflightChecks = [],
  diffFileHeader,
  diffLines = [],
  diffAdditions,
  diffDeletions,
  executionCommand,
  onClose,
  onConfirm,
}: MissionControlApprovalOverlayProps) {
  const [executing, setExecuting] = useState(false);
  const cardRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);

  const handle = useCallback(() => {
    setExecuting(true);
    window.setTimeout(() => {
      onConfirm();
      onClose();
    }, 1400);
  }, [onClose, onConfirm]);

  useEffect(() => {
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !executing) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = prevOverflow;
      document.removeEventListener("keydown", onKey);
    };
  }, [executing, onClose]);

  const additionCount = diffAdditions ?? diffLines.filter((l) => l.kind === "add").length;
  const deletionCount = diffDeletions ?? diffLines.filter((l) => l.kind === "remove").length;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="mc-approval-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !executing) onClose();
      }}
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(7,9,15,0.75)",
        backdropFilter: "blur(8px)",
        animation: "mcFadeIn 0.25s ease both",
        padding: 24,
      }}
    >
      <div
        ref={cardRef}
        style={{
          width: "100%",
          maxWidth: 640,
          maxHeight: "92vh",
          background: "var(--bg-1, var(--paper-2))",
          border: "1px solid var(--line-2, var(--rule-2))",
          borderRadius: 10,
          overflow: "hidden",
          boxShadow: "0 32px 80px rgba(0,0,0,0.6), 0 0 0 1px var(--line, var(--rule))",
          animation: "mcOverlayCard 0.35s ease both",
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          aria-hidden="true"
          style={{ height: 3, background: "linear-gradient(90deg, var(--amber) 0%, var(--cyan, var(--brand)) 100%)" }}
        />

        <div
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid var(--line, var(--rule))",
            background: "var(--bg-2, var(--paper-3))",
            display: "flex",
            alignItems: "center",
            gap: 12,
            flexShrink: 0,
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: 6,
              background: "var(--cyan-bg, var(--brand-bg))",
              border: "1px solid var(--cyan-bd, var(--brand-bd))",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <svg
              width="15"
              height="15"
              viewBox="0 0 24 24"
              fill="none"
              stroke="var(--cyan, var(--brand))"
              strokeWidth={2}
              strokeLinecap="round"
              aria-hidden="true"
            >
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
            </svg>
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.10em",
                color: "var(--cyan, var(--brand))",
              }}
            >
              {subtitle}
            </div>
            <div
              id="mc-approval-title"
              style={{
                fontFamily: "var(--sans)",
                fontSize: 15,
                fontWeight: 600,
                color: "var(--ink, var(--fg-0))",
                marginTop: 2,
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {title}
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close approval dialog"
            disabled={executing}
            style={{
              background: "transparent",
              border: "1px solid var(--line-2, var(--rule-2))",
              borderRadius: 5,
              padding: "4px 8px",
              cursor: executing ? "default" : "pointer",
              color: "var(--ink-3, var(--fg-3))",
              opacity: executing ? 0.4 : 1,
            }}
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              aria-hidden="true"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "16px 18px 20px" }}>
          {coordinates.length > 0 && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: `repeat(${Math.min(coordinates.length, 3)}, 1fr)`,
                gap: 1,
                background: "var(--line, var(--rule))",
                border: "1px solid var(--line, var(--rule))",
                borderRadius: 5,
                overflow: "hidden",
                marginBottom: 16,
              }}
            >
              {coordinates.map((c) => (
                <div key={c.label} style={{ background: "var(--bg-2, var(--paper-3))", padding: "8px 12px" }}>
                  <div
                    style={{
                      fontFamily: "var(--mono)",
                      fontSize: 8,
                      textTransform: "uppercase",
                      letterSpacing: "0.10em",
                      color: "var(--ink-3, var(--fg-3))",
                    }}
                  >
                    {c.label}
                  </div>
                  <div
                    style={{
                      fontFamily: "var(--mono)",
                      fontSize: 12,
                      color: "var(--ink-2, var(--fg-1))",
                      marginTop: 4,
                      fontWeight: 500,
                    }}
                  >
                    {c.value}
                  </div>
                </div>
              ))}
            </div>
          )}

          {impacts.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <SectionLabel>Impact</SectionLabel>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                {impacts.map((imp) => (
                  <div
                    key={imp.label}
                    style={{
                      background: "var(--bg-2, var(--paper-3))",
                      border: "1px solid var(--line, var(--rule))",
                      borderRadius: 5,
                      padding: "10px 12px",
                    }}
                  >
                    <div
                      style={{
                        fontFamily: "var(--mono)",
                        fontSize: 8,
                        textTransform: "uppercase",
                        letterSpacing: "0.10em",
                        color: "var(--ink-3, var(--fg-3))",
                      }}
                    >
                      {imp.label}
                    </div>
                    <div style={{ marginTop: 6 }}>
                      {imp.before && (
                        <>
                          <span
                            style={{
                              fontFamily: "var(--mono)",
                              fontSize: 13,
                              color: "var(--red)",
                              textDecoration: "line-through",
                            }}
                          >
                            {imp.before}
                          </span>
                          <span
                            style={{
                              fontFamily: "var(--mono)",
                              fontSize: 11,
                              color: "var(--ink-3, var(--fg-3))",
                              margin: "0 6px",
                            }}
                          >
                            →
                          </span>
                        </>
                      )}
                      <span
                        style={{
                          fontFamily: "var(--mono)",
                          fontSize: 13,
                          fontWeight: 600,
                          color: "var(--green)",
                        }}
                      >
                        {imp.after}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {preflightChecks.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <SectionLabel>Preflight checks</SectionLabel>
              <ul
                style={{
                  background: "var(--bg-2, var(--paper-3))",
                  border: "1px solid var(--line, var(--rule))",
                  borderRadius: 5,
                  padding: "4px 0",
                  listStyle: "none",
                  margin: 0,
                }}
              >
                {preflightChecks.map((c, i) => (
                  <li
                    key={i}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      padding: "5px 14px",
                      animation: `mcFadeIn 0.3s ${Math.min(i, 6) * 70}ms ease both`,
                    }}
                  >
                    <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--green)" }}>[✓]</span>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-2, var(--fg-1))" }}>
                      {c}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {diffLines.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              <SectionLabel>Diff</SectionLabel>
              <DiffPanel
                fileHeader={diffFileHeader}
                lines={diffLines}
                additions={additionCount}
                deletions={deletionCount}
              />
            </div>
          )}

          {!executing ? (
            <HoldToAuthorize onConfirm={handle} />
          ) : (
            <div
              role="status"
              aria-live="polite"
              style={{
                padding: 20,
                background: "var(--green-bg)",
                border: "1px solid var(--green-bd)",
                borderRadius: 6,
                textAlign: "center",
                animation: "mcFadeIn 0.3s ease both",
              }}
            >
              <div
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 10,
                  textTransform: "uppercase",
                  letterSpacing: "0.10em",
                  color: "var(--green)",
                }}
              >
                Rollout dispatched
              </div>
              {executionCommand && (
                <div
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: 11,
                    color: "var(--ink-2, var(--fg-2))",
                    marginTop: 8,
                  }}
                >
                  $ {executionCommand}
                  <span className="mc-caret" />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "block",
        marginBottom: 8,
        fontFamily: "var(--mono)",
        fontSize: 9,
        textTransform: "uppercase",
        letterSpacing: "0.10em",
        color: "var(--ink-3, var(--fg-3))",
      }}
    >
      {children}
    </div>
  );
}

type DiffPanelProps = {
  fileHeader?: string;
  lines: DiagnosisDiffLine[];
  additions: number;
  deletions: number;
};

function DiffPanel({ fileHeader, lines, additions, deletions }: DiffPanelProps) {
  return (
    <div
      style={{
        border: "1px solid var(--line, var(--rule))",
        borderRadius: 5,
        overflow: "hidden",
        background: "var(--bg-0, var(--paper))",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid var(--line, var(--rule))",
          background: "var(--bg-2, var(--paper-3))",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 10,
            color: "var(--ink-3, var(--fg-3))",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {fileHeader ?? "changes"}
        </span>
        <div style={{ flex: 1 }} />
        {deletions > 0 && (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 9,
              background: "var(--red-bg)",
              color: "var(--red)",
              border: "1px solid var(--red-bd)",
              padding: "1px 6px",
              borderRadius: 3,
            }}
          >
            −{deletions}
          </span>
        )}
        {additions > 0 && (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 9,
              background: "var(--green-bg)",
              color: "var(--green)",
              border: "1px solid var(--green-bd)",
              padding: "1px 6px",
              borderRadius: 3,
            }}
          >
            +{additions}
          </span>
        )}
      </div>
      <div style={{ padding: "6px 0", maxHeight: 260, overflowY: "auto" }}>
        {lines.map((l, i) => {
          const isAdd = l.kind === "add";
          const isRemove = l.kind === "remove";
          return (
            <div
              key={i}
              style={{
                padding: "0 12px",
                fontFamily: "var(--mono)",
                fontSize: 11,
                lineHeight: 1.85,
                background: isAdd ? "rgba(52,211,153,0.08)" : isRemove ? "rgba(251,113,133,0.08)" : "transparent",
                color: isAdd ? "var(--green)" : isRemove ? "var(--red)" : "var(--ink-2, var(--fg-2))",
                borderLeft: `2px solid ${isAdd ? "var(--green)" : isRemove ? "var(--red)" : "transparent"}`,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {isAdd ? "+ " : isRemove ? "- " : "  "}
              {l.text}
              {l.comment && (
                <span style={{ color: "var(--ink-3, var(--fg-3))", marginLeft: 12 }}>{"// " + l.comment}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default MissionControlApprovalOverlay;
