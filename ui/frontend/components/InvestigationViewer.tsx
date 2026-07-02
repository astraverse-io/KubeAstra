"use client";

import React, { useState } from "react";

type Evidence = {
  evidence_id: string;
  evidence_type: string;
  tool: string;
  summary?: string;
  collected_at?: string;
};

type Finding = {
  title: string;
  detail: string;
  confidence?: number;
};

type RCA = {
  summary?: string;
  confidence?: number;
  root_causes?: string[];
  recommendations?: string[];
  supporting_evidence?: string[];
  what_was_ruled_out?: string[];
  next_checks?: string[];
};

type AuditEvent = {
  sequence: number;
  event_type: string;
  payload?: Record<string, unknown>;
  created_at: string;
};

type ExecutedTool = {
  tool: string;
  args?: Record<string, unknown>;
  reason?: string;
};

type InvestigationDoc = {
  investigation_id: string;
  status?: string;
  alert?: {
    name?: string;
    source?: string;
    severity?: string;
    labels?: Record<string, string>;
  };
  selected_playbook?: string;
  classification?: { confidence?: number; matched_rules?: string[]; playbook_id?: string };
  executed_tools?: ExecutedTool[];
  evidence?: Evidence[];
  findings?: Finding[];
  rca?: RCA;
  audit_log?: AuditEvent[];
  created_at?: string;
  updated_at?: string;
};

// ── Style helpers ────────────────────────────────────────────────────────────

const card: React.CSSProperties = {
  background: "var(--paper)",
  border: "1px solid var(--rule)",
  borderRadius: "0.625rem",
  padding: "0.875rem 1rem",
  marginBottom: "0.75rem",
};

const sectionTitle: React.CSSProperties = {
  fontSize: "0.6875rem",
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  color: "var(--ink-3)",
  fontWeight: 700,
  marginBottom: "0.5rem",
};

const kvLabel: React.CSSProperties = {
  fontSize: "0.625rem",
  letterSpacing: "0.05em",
  textTransform: "uppercase",
  color: "var(--ink-3)",
  fontWeight: 600,
};

const kvValue: React.CSSProperties = {
  fontFamily: "var(--mono)",
  fontSize: "0.8125rem",
  color: "var(--ink)",
  wordBreak: "break-all",
};

function statusPill(status?: string): React.CSSProperties {
  const s = (status || "").toLowerCase();
  const tokens =
    s === "completed"
      ? { fg: "var(--green)", bg: "var(--green-bg)", bd: "var(--green-bd)" }
      : s === "failed"
      ? { fg: "var(--red)", bg: "var(--red-bg)", bd: "var(--red-bd)" }
      : s === "running"
      ? { fg: "var(--brand)", bg: "var(--brand-bg)", bd: "var(--brand-bd)" }
      : { fg: "var(--ink-3)", bg: "var(--paper-3)", bd: "var(--rule)" };
  return {
    display: "inline-flex",
    alignItems: "center",
    padding: "0.1875rem 0.5rem",
    borderRadius: "999px",
    fontSize: "0.6875rem",
    fontWeight: 700,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
    background: tokens.bg,
    color: tokens.fg,
    border: `1px solid ${tokens.bd}`,
  };
}

// ── Sub-components ───────────────────────────────────────────────────────────

function BulletList({ items, mono = false }: { items?: string[]; mono?: boolean }) {
  if (!items || items.length === 0) return <p style={{ color: "var(--ink-3)", fontSize: "0.8125rem" }}>—</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: "1.125rem", display: "flex", flexDirection: "column", gap: "0.3125rem" }}>
      {items.map((item, i) => (
        <li
          key={i}
          style={{
            fontSize: "0.8125rem",
            color: "var(--ink-2)",
            lineHeight: 1.5,
            fontFamily: mono ? "var(--mono)" : "var(--sans)",
          }}
        >
          {item}
        </li>
      ))}
    </ul>
  );
}

function RcaCard({ rca }: { rca?: RCA }) {
  if (!rca || !rca.summary) return null;
  const conf = rca.confidence != null ? `${Math.round(rca.confidence * 100)}%` : null;
  return (
    <div style={{ ...card, borderColor: "var(--brand-bd)", background: "var(--brand-bg)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "0.625rem" }}>
        <div style={sectionTitle}>Root Cause Analysis</div>
        {conf && (
          <span style={{ fontSize: "0.75rem", color: "var(--ink-3)", fontFamily: "var(--mono)" }}>
            confidence {conf}
          </span>
        )}
      </div>
      <p style={{ fontSize: "0.9375rem", color: "var(--ink)", margin: "0 0 0.875rem 0", lineHeight: 1.55 }}>
        {rca.summary}
      </p>

      {rca.root_causes && rca.root_causes.length > 0 && (
        <div style={{ marginBottom: "0.75rem" }}>
          <div style={{ ...kvLabel, marginBottom: "0.375rem" }}>Root causes</div>
          <BulletList items={rca.root_causes} />
        </div>
      )}
      {rca.recommendations && rca.recommendations.length > 0 && (
        <div style={{ marginBottom: "0.75rem" }}>
          <div style={{ ...kvLabel, marginBottom: "0.375rem" }}>Recommendations</div>
          <BulletList items={rca.recommendations} />
        </div>
      )}
      {rca.what_was_ruled_out && rca.what_was_ruled_out.length > 0 && (
        <div style={{ marginBottom: "0.75rem" }}>
          <div style={{ ...kvLabel, marginBottom: "0.375rem" }}>Ruled out</div>
          <BulletList items={rca.what_was_ruled_out} />
        </div>
      )}
      {rca.next_checks && rca.next_checks.length > 0 && (
        <div>
          <div style={{ ...kvLabel, marginBottom: "0.375rem" }}>Next checks</div>
          <BulletList items={rca.next_checks} />
        </div>
      )}
    </div>
  );
}

function FindingsCard({ findings }: { findings?: Finding[] }) {
  if (!findings || findings.length === 0) return null;
  return (
    <div style={card}>
      <div style={sectionTitle}>Findings ({findings.length})</div>
      {findings.map((f, i) => (
        <div
          key={i}
          style={{
            paddingBottom: i === findings.length - 1 ? 0 : "0.625rem",
            marginBottom: i === findings.length - 1 ? 0 : "0.625rem",
            borderBottom: i === findings.length - 1 ? "none" : "1px solid var(--rule)",
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "0.25rem" }}>
            <span style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--ink)" }}>{f.title}</span>
            {f.confidence != null && (
              <span style={{ fontSize: "0.6875rem", color: "var(--ink-3)", fontFamily: "var(--mono)" }}>
                {Math.round(f.confidence * 100)}%
              </span>
            )}
          </div>
          <p style={{ margin: 0, fontSize: "0.8125rem", color: "var(--ink-2)", lineHeight: 1.5 }}>{f.detail}</p>
        </div>
      ))}
    </div>
  );
}

function EvidenceCard({ evidence }: { evidence?: Evidence[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (!evidence || evidence.length === 0) return null;
  const toggle = (id: string) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExpanded(next);
  };
  return (
    <div style={card}>
      <div style={sectionTitle}>Evidence ({evidence.length})</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {evidence.map((e) => {
          const isOpen = expanded.has(e.evidence_id);
          return (
            <div
              key={e.evidence_id}
              style={{ border: "1px solid var(--rule)", borderRadius: "0.5rem", background: "var(--paper-2)" }}
            >
              <button
                onClick={() => toggle(e.evidence_id)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  background: "transparent",
                  border: "none",
                  padding: "0.5rem 0.75rem",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  cursor: "pointer",
                  color: "var(--ink)",
                  fontFamily: "var(--sans)",
                }}
              >
                <span style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  <span style={{ fontFamily: "var(--mono)", fontSize: "0.8125rem", color: "var(--brand)" }}>{e.tool}</span>
                  <span style={{ fontSize: "0.6875rem", color: "var(--ink-3)" }}>{e.evidence_type}</span>
                </span>
                <span style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>{isOpen ? "▾" : "▸"}</span>
              </button>
              {isOpen && (
                <pre
                  style={{
                    margin: 0,
                    padding: "0.5rem 0.75rem 0.75rem",
                    borderTop: "1px solid var(--rule)",
                    fontSize: "0.75rem",
                    fontFamily: "var(--mono)",
                    color: "var(--ink-2)",
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    maxHeight: "20rem",
                    overflow: "auto",
                  }}
                >
                  {e.summary || "(no summary)"}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function AuditTimeline({ events }: { events?: AuditEvent[] }) {
  if (!events || events.length === 0) return null;
  return (
    <div style={card}>
      <div style={sectionTitle}>Audit Timeline ({events.length})</div>
      <ol style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {events.map((e) => (
          <li
            key={e.sequence}
            style={{
              display: "grid",
              gridTemplateColumns: "1.5rem 1fr auto",
              gap: "0.5rem",
              alignItems: "baseline",
              fontSize: "0.8125rem",
              color: "var(--ink-2)",
            }}
          >
            <span style={{ color: "var(--ink-4)", fontFamily: "var(--mono)", fontSize: "0.6875rem" }}>
              {e.sequence}
            </span>
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink)" }}>{e.event_type}</span>
            <span style={{ color: "var(--ink-3)", fontSize: "0.6875rem", fontFamily: "var(--mono)" }}>
              {new Date(e.created_at).toLocaleTimeString()}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function ExecutedToolsCard({ tools }: { tools?: ExecutedTool[] }) {
  if (!tools || tools.length === 0) return null;
  return (
    <div style={card}>
      <div style={sectionTitle}>Executed Tools ({tools.length})</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        {tools.map((t, i) => (
          <div
            key={i}
            style={{
              border: "1px solid var(--rule)",
              borderRadius: "0.5rem",
              padding: "0.5rem 0.625rem",
              background: "var(--paper-2)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "0.25rem" }}>
              <span style={{ fontFamily: "var(--mono)", fontSize: "0.8125rem", color: "var(--brand)" }}>{t.tool}</span>
            </div>
            {t.args && Object.keys(t.args).length > 0 && (
              <pre
                style={{
                  margin: 0,
                  fontSize: "0.6875rem",
                  fontFamily: "var(--mono)",
                  color: "var(--ink-3)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {JSON.stringify(t.args)}
              </pre>
            )}
            {t.reason && (
              <p style={{ margin: "0.25rem 0 0 0", fontSize: "0.75rem", color: "var(--ink-2)", lineHeight: 1.45 }}>
                {t.reason}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────

export default function InvestigationViewer({ document: doc }: { document: InvestigationDoc | null }) {
  const [showRaw, setShowRaw] = useState(false);
  if (!doc) return null;

  const alert = doc.alert || {};
  const labels = alert.labels || {};
  const ns = labels.namespace || "—";
  const resource = labels.resource || "—";
  const kind = labels.kind || "—";

  return (
    <div style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      {/* Sticky header */}
      <div
        style={{
          padding: "0.875rem 1rem",
          borderBottom: "1px solid var(--rule)",
          background: "var(--paper-2)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.75rem" }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: "0.875rem", fontWeight: 700, color: "var(--ink)" }}>
              {alert.name || "Investigation"}
            </div>
            <div
              style={{
                fontFamily: "var(--mono)",
                fontSize: "0.6875rem",
                color: "var(--ink-3)",
                marginTop: "0.125rem",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {doc.investigation_id}
            </div>
          </div>
          <span style={statusPill(doc.status)}>{doc.status || "unknown"}</span>
        </div>

        {/* Compact key-value strip */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(8rem, 1fr))",
            gap: "0.75rem",
            marginTop: "0.75rem",
          }}
        >
          {[
            ["Namespace", ns],
            ["Kind", kind],
            ["Resource", resource],
            ["Source", alert.source || "—"],
            ["Severity", alert.severity || "—"],
            ["Playbook", doc.selected_playbook || "—"],
          ].map(([k, v]) => (
            <div key={k}>
              <div style={kvLabel}>{k}</div>
              <div style={kvValue}>{v}</div>
            </div>
          ))}
        </div>
      </div>

      {/* Scrollable body */}
      <div style={{ flex: 1, overflowY: "auto", padding: "1rem" }}>
        <RcaCard rca={doc.rca} />
        <FindingsCard findings={doc.findings} />
        <ExecutedToolsCard tools={doc.executed_tools} />
        <EvidenceCard evidence={doc.evidence} />
        <AuditTimeline events={doc.audit_log} />

        {/* Collapsed raw doc — kept available but not in the way */}
        <div style={{ marginTop: "0.5rem" }}>
          <button
            onClick={() => setShowRaw((s) => !s)}
            style={{
              background: "transparent",
              border: "1px solid var(--rule)",
              color: "var(--ink-3)",
              borderRadius: "0.5rem",
              padding: "0.375rem 0.625rem",
              fontSize: "0.75rem",
              fontFamily: "var(--mono)",
              cursor: "pointer",
            }}
          >
            {showRaw ? "▾ Hide raw document" : "▸ Show raw document"}
          </button>
          {showRaw && (
            <pre
              style={{
                marginTop: "0.5rem",
                padding: "0.75rem",
                background: "var(--paper)",
                border: "1px solid var(--rule)",
                borderRadius: "0.5rem",
                fontSize: "0.6875rem",
                fontFamily: "var(--mono)",
                color: "var(--ink-2)",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                maxHeight: "24rem",
                overflow: "auto",
              }}
            >
              {JSON.stringify(doc, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
