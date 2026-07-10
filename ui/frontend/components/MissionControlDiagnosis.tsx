"use client";

import React from "react";

export type DiagnosisSeverity = "sev-1" | "sev-2" | "sev-3" | "info";

export type DiagnosisMetric = {
  label: string;
  value: string;
  tone?: "critical" | "warn" | "neutral";
};

export type DiagnosisDiffLine = {
  kind: "add" | "remove" | "context";
  text: string;
  comment?: string;
};

export type MissionControlDiagnosisProps = {
  severity: DiagnosisSeverity;
  title: string;
  summary: React.ReactNode;
  confidence?: number;
  metrics?: DiagnosisMetric[];
  diff?: DiagnosisDiffLine[];
  diffMeta?: string;
  onAuthorize?: () => void;
  authorizeLabel?: string;
};

const SEVERITY_COLORS: Record<DiagnosisSeverity, { border: string; grad: string; label: string }> = {
  "sev-1": {
    border: "var(--red)",
    grad: "linear-gradient(90deg, var(--red) 0%, var(--amber) 100%)",
    label: "SEV-1",
  },
  "sev-2": {
    border: "var(--amber)",
    grad: "linear-gradient(90deg, var(--amber) 0%, var(--cyan, var(--brand)) 100%)",
    label: "SEV-2",
  },
  "sev-3": {
    border: "var(--cyan, var(--brand))",
    grad: "linear-gradient(90deg, var(--cyan, var(--brand)) 0%, var(--green) 100%)",
    label: "SEV-3",
  },
  info: {
    border: "var(--cyan, var(--brand))",
    grad: "linear-gradient(90deg, var(--cyan, var(--brand)) 0%, var(--cyan-2, var(--brand-bright)) 100%)",
    label: "INFO",
  },
};

function ToneColor(tone: DiagnosisMetric["tone"]): string {
  switch (tone) {
    case "critical":
      return "var(--red)";
    case "warn":
      return "var(--amber)";
    default:
      return "var(--ink, var(--fg-1))";
  }
}

export function MissionControlDiagnosis({
  severity,
  title,
  summary,
  confidence,
  metrics,
  diff,
  diffMeta,
  onAuthorize,
  authorizeLabel = "REVIEW & AUTHORIZE FIX",
}: MissionControlDiagnosisProps) {
  const sev = SEVERITY_COLORS[severity];
  const isCritical = severity === "sev-1" || severity === "sev-2";
  const iconStroke = isCritical ? "var(--red)" : "var(--cyan, var(--brand))";
  const iconBg = isCritical ? "var(--red-bg)" : "var(--cyan-bg, var(--brand-bg))";
  const iconBd = isCritical ? "var(--red-bd)" : "var(--cyan-bd, var(--brand-bd))";
  const labelColor = isCritical ? "var(--red)" : "var(--cyan, var(--brand))";

  const bars = 5;
  const filled = confidence == null ? bars : Math.max(0, Math.min(bars, Math.round(confidence * bars)));

  return (
    <section
      aria-label={`Diagnosis: ${title}`}
      style={{
        background: "linear-gradient(180deg, var(--bg-2, var(--paper-3)) 0%, var(--bg-1, var(--paper-2)) 100%)",
        border: "1px solid var(--line-2, var(--rule-2))",
        borderRadius: 8,
        overflow: "hidden",
        boxShadow: "0 8px 24px rgba(0,0,0,0.3), 0 0 0 1px var(--line, var(--rule))",
        animation: "mcFadeIn 0.4s ease both",
      }}
    >
      <div aria-hidden="true" style={{ height: 3, background: sev.grad }} />

      <div
        style={{
          padding: "12px 16px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          borderBottom: "1px solid var(--line, var(--rule))",
          background: "var(--bg-2, var(--paper-3))",
        }}
      >
        <div
          style={{
            width: 32,
            height: 32,
            borderRadius: 6,
            background: iconBg,
            border: `1px solid ${iconBd}`,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke={iconStroke}
            strokeWidth={2.5}
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 9,
                textTransform: "uppercase",
                letterSpacing: "0.10em",
                color: labelColor,
              }}
            >
              Root cause · confirmed
            </span>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 9,
                fontWeight: 600,
                background: iconBg,
                color: labelColor,
                border: `1px solid ${iconBd}`,
                padding: "1px 6px",
                borderRadius: 3,
                letterSpacing: "0.1em",
              }}
            >
              {sev.label}
            </span>
          </div>
          <div
            style={{
              fontFamily: "var(--sans)",
              fontSize: 15,
              fontWeight: 600,
              color: "var(--ink, var(--fg-0))",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {title}
          </div>
        </div>
        {confidence != null && (
          <div style={{ textAlign: "right", flexShrink: 0 }} aria-label={`Confidence ${Math.round(confidence * 100)}%`}>
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: 8,
                textTransform: "uppercase",
                letterSpacing: "0.10em",
                color: "var(--ink-3, var(--fg-3))",
              }}
            >
              Confidence
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4, justifyContent: "flex-end" }}>
              <span style={{ display: "flex", gap: 2 }} aria-hidden="true">
                {Array.from({ length: bars }).map((_, i) => (
                  <span
                    key={i}
                    style={{
                      display: "block",
                      width: 4,
                      height: 12,
                      background: i < filled ? "var(--cyan, var(--brand))" : "var(--line-2, var(--rule-2))",
                      opacity: i < filled ? 1 - i * 0.05 : 1,
                    }}
                  />
                ))}
              </span>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, fontWeight: 600, color: "var(--cyan, var(--brand))" }}>
                {confidence.toFixed(2)}
              </span>
            </div>
          </div>
        )}
      </div>

      <div style={{ padding: 16 }}>
        <p
          style={{
            fontFamily: "var(--sans)",
            fontSize: 13,
            lineHeight: 1.6,
            color: "var(--ink-2, var(--fg-1))",
            marginBottom: 14,
          }}
        >
          {summary}
        </p>

        {metrics && metrics.length > 0 && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: `repeat(${Math.min(metrics.length, 4)}, 1fr)`,
              gap: 1,
              background: "var(--line, var(--rule))",
              border: "1px solid var(--line, var(--rule))",
              borderRadius: 4,
              overflow: "hidden",
              marginBottom: 14,
            }}
          >
            {metrics.map((m) => (
              <div key={m.label} style={{ background: "var(--bg-1, var(--paper-2))", padding: "10px 12px" }}>
                <div
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: 8,
                    textTransform: "uppercase",
                    letterSpacing: "0.10em",
                    color: "var(--ink-3, var(--fg-3))",
                  }}
                >
                  {m.label}
                </div>
                <div
                  style={{
                    display: "block",
                    fontFamily: "var(--mono)",
                    fontSize: 20,
                    fontWeight: 600,
                    color: ToneColor(m.tone),
                    marginTop: 4,
                    letterSpacing: "-0.02em",
                  }}
                >
                  {m.value}
                </div>
              </div>
            ))}
          </div>
        )}

        {diff && diff.length > 0 && (
          <div
            style={{
              background: "var(--bg-0, var(--paper))",
              border: "1px solid var(--line, var(--rule))",
              borderRadius: 5,
              padding: "10px 12px",
              marginBottom: 14,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 9,
                  textTransform: "uppercase",
                  letterSpacing: "0.10em",
                  color: "var(--cyan, var(--brand))",
                }}
              >
                Proposed remediation
              </span>
              {diffMeta && (
                <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--ink-3, var(--fg-3))" }}>
                  {diffMeta}
                </span>
              )}
            </div>
            <pre
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                lineHeight: 1.7,
                margin: 0,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                color: "var(--ink-2, var(--fg-2))",
              }}
            >
              {diff.map((line, i) => {
                const color =
                  line.kind === "add" ? "var(--green)" : line.kind === "remove" ? "var(--red)" : "var(--ink-3, var(--fg-3))";
                const prefix = line.kind === "add" ? "+ " : line.kind === "remove" ? "- " : "  ";
                return (
                  <span key={i} style={{ color, display: "block" }}>
                    {prefix}
                    {line.text}
                    {line.comment && (
                      <span style={{ color: "var(--ink-3, var(--fg-3))", marginLeft: 12 }}>{"// " + line.comment}</span>
                    )}
                  </span>
                );
              })}
            </pre>
          </div>
        )}

        {onAuthorize && (
          <button
            type="button"
            onClick={onAuthorize}
            className="mc-diagnosis-cta"
            style={{
              width: "100%",
              padding: "12px 16px",
              background: "var(--cyan-bg, var(--brand-bg))",
              border: "1px solid var(--cyan-bd, var(--brand-bd))",
              borderRadius: 6,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 10,
              transition: "background 0.2s, box-shadow 0.2s",
            }}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="var(--cyan, var(--brand))"
              strokeWidth={2}
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 12,
                fontWeight: 600,
                color: "var(--cyan, var(--brand))",
                letterSpacing: "0.05em",
              }}
            >
              {authorizeLabel}
            </span>
            <span
              aria-hidden="true"
              style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--cyan, var(--brand))", opacity: 0.7 }}
            >
              →
            </span>
          </button>
        )}
      </div>
    </section>
  );
}

/**
 * Inline color-coded token — use inside summary strings to highlight
 * resource names, values, or config paths (e.g. wrap "128Mi" in <red>).
 */
export function InlineToken({
  variant = "cyan",
  children,
}: {
  variant?: "red" | "amber" | "cyan" | "green";
  children: React.ReactNode;
}) {
  const map = {
    red: { bg: "var(--red-bg)", bd: "var(--red-bd)", color: "var(--red)" },
    amber: { bg: "var(--amber-bg)", bd: "var(--amber-bd)", color: "var(--amber)" },
    cyan: { bg: "var(--cyan-bg, var(--brand-bg))", bd: "var(--cyan-bd, var(--brand-bd))", color: "var(--cyan, var(--brand))" },
    green: { bg: "var(--green-bg)", bd: "var(--green-bd)", color: "var(--green)" },
  } as const;
  const c = map[variant];
  return (
    <span
      style={{
        fontFamily: "var(--mono)",
        fontSize: "0.85em",
        fontWeight: 500,
        background: c.bg,
        color: c.color,
        border: `1px solid ${c.bd}`,
        padding: "0 5px",
        borderRadius: 2,
        whiteSpace: "nowrap",
      }}
    >
      {children}
    </span>
  );
}

export default MissionControlDiagnosis;
