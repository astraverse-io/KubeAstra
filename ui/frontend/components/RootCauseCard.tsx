import React, { useState } from "react";

type RootCommand = { command?: string; cmd?: string; explanation?: string; description?: string };
type RootMetric = { label: string; value: string; status: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatEvidenceValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (isRecord(value)) {
    const label = value.message ?? value.reason ?? value.target ?? value.name ?? value.service;
    if (label) {
      const details = Object.entries(value)
        .filter(([key]) => !["message", "reason", "target", "name", "service"].includes(key))
        .map(([key, item]) => `${key}=${formatEvidenceValue(item)}`)
        .filter(Boolean)
        .join(", ");
      return details ? `${String(label)} (${details})` : String(label);
    }
    return JSON.stringify(value, null, 2);
  }
  if (Array.isArray(value)) {
    return value.map(formatEvidenceValue).filter(Boolean).join(", ");
  }
  return String(value);
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(formatEvidenceValue).filter(Boolean) : [];
}

function rootCauseSummary(result: Record<string, unknown>) {
  if (!isRecord(result.root_cause_summary)) return null;
  const rootCause = String(result.root_cause_summary.root_cause ?? "").trim();
  return rootCause ? result.root_cause_summary : null;
}

export function extractRootCause(result?: Record<string, unknown> | null) {
  if (!result) return null;
  const summary = rootCauseSummary(result);
  if (summary) {
    return {
      rootCause: String(summary.root_cause ?? ""),
      solution: String(summary.suggested_fix ?? ""),
      severity: String(summary.severity ?? ""),
      confidence: summary.confidence,
      commands: Array.isArray(summary.executable_actions) ? summary.executable_actions as RootCommand[] : [],
      metrics: Array.isArray(summary.metrics) ? summary.metrics as RootMetric[] : [],
      summary,
    };
  }

  const evidence = isRecord(result.evidence_summary) ? result.evidence_summary : {};
  const ai = result.ai;
  const analysis = isRecord(ai) && isRecord(ai.ai_analysis) ? ai.ai_analysis : {};
  const rootCause = String(
    analysis.root_cause ?? evidence.suspected_root_cause ?? "",
  );
  if (!rootCause) return null;
  return {
    rootCause,
    solution: String(analysis.solution ?? evidence.suggested_fix ?? ""),
    severity: String(analysis.severity ?? ""),
    confidence: analysis.confidence,
    commands: Array.isArray(analysis.commands) ? analysis.commands as RootCommand[] : [],
    metrics: Array.isArray(analysis.metrics) ? analysis.metrics as RootMetric[] : [],
  };
}

function formatEvidenceSummary(result: Record<string, unknown>) {
  const summary = rootCauseSummary(result);
  if (summary) {
    const lines: string[] = [];
    if (summary.root_cause) {
      lines.push("# Verified root cause");
      lines.push(String(summary.root_cause));
    }

    const target = isRecord(summary.target) ? summary.target : {};
    const targetParts = [
      summary.resource_kind ? `kind=${String(summary.resource_kind)}` : "",
      summary.resource_name ? `name=${String(summary.resource_name)}` : "",
      summary.namespace ? `namespace=${String(summary.namespace)}` : "",
      target.mode ? `mode=${String(target.mode)}` : "",
      target.container ? `container=${String(target.container)}` : "",
    ].filter(Boolean);
    if (targetParts.length) {
      lines.push("");
      lines.push("# Target");
      lines.push(`- ${targetParts.join(", ")}`);
    }

    const evidence = asStringList(summary.evidence);
    if (evidence.length) {
      lines.push("");
      lines.push("# Evidence");
      evidence.forEach((item) => lines.push(`- ${item}`));
    }

    const secondary = asStringList(summary.secondary_findings);
    if (secondary.length) {
      lines.push("");
      lines.push("# Secondary findings");
      secondary.forEach((item) => lines.push(`- ${item}`));
    }

    const related = asStringList(summary.related_resources);
    if (related.length) {
      lines.push("");
      lines.push("# Related resources");
      related.forEach((item) => lines.push(`- ${item}`));
    }

    if (summary.suggested_fix) {
      lines.push("");
      lines.push("# Suggested fix");
      lines.push(String(summary.suggested_fix));
    }

    if (summary.data_completeness || summary.source_tool) {
      lines.push("");
      lines.push("# Source");
      lines.push(`- source_tool=${String(summary.source_tool ?? "unknown")}, data_completeness=${String(summary.data_completeness ?? "unknown")}`);
    }

    return lines.join("\n").trim() || JSON.stringify(summary, null, 2);
  }

  const evidenceSummary = isRecord(result.evidence_summary) ? result.evidence_summary : null;
  if (!evidenceSummary) {
    const raw = result.raw_output;
    return typeof raw === "string" && raw.trim() ? raw : "No raw evidence provided.";
  }

  const lines: string[] = [];
  if (evidenceSummary.suspected_root_cause) {
    lines.push("# Verified root cause");
    lines.push(String(evidenceSummary.suspected_root_cause));
  }

  const evidence = asStringList(evidenceSummary.evidence);
  if (evidence.length) {
    lines.push("");
    lines.push("# Evidence");
    evidence.forEach((item) => lines.push(`- ${item}`));
  }

  const checks = Array.isArray(evidenceSummary.dependency_checks)
    ? evidenceSummary.dependency_checks.filter(isRecord)
    : [];
  if (checks.length) {
    lines.push("");
    lines.push("# Dependency checks");
    checks.forEach((check) => {
      const target = String(check.target ?? check.service ?? "dependency");
      const serviceExists = check.service_exists;
      const endpointsExist = check.endpoints_exist;
      const readyAddresses = check.ready_addresses;
      lines.push(
        `- ${target}: service_exists=${String(serviceExists)}, endpoints_exist=${String(endpointsExist)}, ready_addresses=${String(readyAddresses ?? "unknown")}`,
      );
    });
  }

  if (evidenceSummary.suggested_fix) {
    lines.push("");
    lines.push("# Suggested fix");
    lines.push(String(evidenceSummary.suggested_fix));
  }

  return lines.join("\n").trim() || JSON.stringify(evidenceSummary, null, 2);
}

export function RootCauseCard({ result, onReviewExecute }: { result: Record<string, unknown>, onReviewExecute?: () => void }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const root = extractRootCause(result);

  if (!root?.rootCause) return null;

  const rawEvidence = formatEvidenceSummary(result);

  return (
    <div style={{
      maxWidth: "520px",
      background: "var(--paper-2)",
      border: "1px solid var(--rule)",
      borderRadius: "12px",
      overflow: "hidden",
      boxShadow: "0 4px 20px rgba(0,0,0,0.45), 0 1px 4px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.025)",
      animation: "springIn 0.45s cubic-bezier(0.34,1.56,0.64,1) both",
    }}>
      {/* Header strip */}
      <div style={{
        padding: "11px 16px",
        background: root.severity.toLowerCase() === "critical" ? "var(--red-bg)" : "var(--amber-bg)",
        borderBottom: `1px solid ${root.severity.toLowerCase() === "critical" ? "var(--red-bd)" : "var(--amber-bd)"}`,
        display: "flex", alignItems: "center", gap: "10px",
      }}>
        <div style={{
          width: "26px", height: "26px", borderRadius: "6px",
          background: root.severity.toLowerCase() === "critical" ? "rgba(248,113,113,0.15)" : "rgba(251,191,36,0.15)",
          border: `1px solid ${root.severity.toLowerCase() === "critical" ? "var(--red-bd)" : "var(--amber-bd)"}`,
          display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
        }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
            stroke={root.severity.toLowerCase() === "critical" ? "var(--red)" : "var(--amber)"} strokeWidth="2.5" strokeLinecap="round">
            <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
            <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </div>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: "7px" }}>
            <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ink)" }}>Root Cause Identified</span>
            {root.severity && (
              <span style={{
                fontSize: "9px", fontWeight: 700, letterSpacing: "0.12em", textTransform: "uppercase",
                background: root.severity.toLowerCase() === "critical" ? "rgba(248,113,113,0.18)" : "rgba(251,191,36,0.18)",
                border: `1px solid ${root.severity.toLowerCase() === "critical" ? "var(--red-bd)" : "var(--amber-bd)"}`,
                color: root.severity.toLowerCase() === "critical" ? "var(--red)" : "var(--amber)",
                borderRadius: "3px", padding: "1px 6px",
              }}>{root.severity}</span>
            )}
          </div>
        </div>
        <div style={{ flex: 1 }} />
        {root.confidence !== undefined && (
          <div style={{
            fontSize: "10px", fontFamily: "var(--mono)", color: "var(--ink-3)",
            background: "rgba(255,255,255,0.025)", border: "1px solid var(--rule)",
            borderRadius: "4px", padding: "2px 7px",
          }}>
            confidence: {Number(root.confidence).toFixed(2)}
          </div>
        )}
      </div>

      <div style={{ padding: "14px 16px" }}>
        {/* Summary */}
        <p style={{ fontSize: "13px", color: "var(--ink-2)", lineHeight: 1.65, marginBottom: "14px" }}>
          {root.rootCause}
        </p>

        {root.metrics && root.metrics.length > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: `repeat(${Math.min(root.metrics.length, 3)}, 1fr)`, gap: "8px", marginBottom: "14px" }}>
            {root.metrics.map(m => {
              const isBad = m.status === "critical" || m.status === "error";
              const isWarn = m.status === "warning";
              const bg = isBad ? "var(--red-bg)" : isWarn ? "var(--amber-bg)" : "var(--paper-3)";
              const bd = isBad ? "var(--red-bd)" : isWarn ? "var(--amber-bd)" : "var(--rule)";
              const tx = isBad ? "var(--red)" : isWarn ? "var(--amber)" : "var(--ink-2)";
              return (
                <div key={m.label} style={{
                  background: bg, border: `1px solid ${bd}`,
                  borderRadius: "7px", padding: "9px 12px", textAlign: "center",
                }}>
                  <div style={{ fontSize: "18px", fontWeight: 700, color: tx, fontFamily: "var(--mono)", letterSpacing: "-0.02em" }}>
                    {m.value}
                  </div>
                  <div style={{ fontSize: "9px", color: "var(--ink-4)", marginTop: "3px", letterSpacing: "0.1em", textTransform: "uppercase", fontWeight: 500 }}>
                    {m.label}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Raw evidence */}
        <div style={{
          border: "1px solid var(--rule)", borderRadius: "7px",
          overflow: "hidden", marginBottom: "14px",
        }}>
          <button
            onClick={() => setEvidenceOpen(o => !o)}
            style={{
              width: "100%", padding: "7px 12px",
              background: "var(--paper-3)", border: "none", cursor: "pointer",
              display: "flex", alignItems: "center", gap: "6px",
              borderBottom: evidenceOpen ? "1px solid var(--rule)" : "none",
            }}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none"
              stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round">
              <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
            </svg>
            <span style={{ fontSize: "10px", color: "var(--ink-3)", fontFamily: "var(--mono)", fontWeight: 500 }}>
              Evidence Details
            </span>
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none"
              stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round"
              style={{ marginLeft: "auto", transform: evidenceOpen ? "rotate(180deg)" : "none", transition: "transform 0.2s" }}>
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
          {evidenceOpen && (
            <div style={{
              padding: "10px 14px", background: "var(--paper)",
              fontFamily: "var(--mono)", fontSize: "11px", lineHeight: 1.9,
              maxHeight: "200px", overflowY: "auto",
            }}>
              {String(rawEvidence).split("\n").map((line, i) => (
                <div key={i} style={{
                  color: line.startsWith("#") ? "var(--brand)" :
                    line.startsWith(">") ? "var(--amber)" :
                      line.startsWith("!") ? "var(--red)" :
                        line.startsWith("  ") ? "var(--ink-2)" : "var(--ink-3)",
                  fontWeight: line.startsWith("#") ? 600 : 400,
                  whiteSpace: "pre-wrap"
                }}>
                  {line || <br />}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* CTA */}
        {onReviewExecute && (
          <button
            onClick={() => onReviewExecute()}
            style={{
              width: "100%", padding: "11px",
              background: "var(--brand-bg)",
              border: "1px solid var(--brand-bd)",
              borderRadius: "8px", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center", gap: "8px",
              transition: "all 0.18s ease",
            }}
            onMouseEnter={e => {
              e.currentTarget.style.boxShadow = "0 2px 12px var(--brand-bd)";
            }}
            onMouseLeave={e => {
              e.currentTarget.style.boxShadow = "none";
            }}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
              stroke="var(--brand)" strokeWidth="2.5" strokeLinecap="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            </svg>
            <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--brand)", letterSpacing: "0.04em" }}>
              Review &amp; Execute Fix
            </span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
              stroke="var(--brand)" strokeWidth="2.5" strokeLinecap="round">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
