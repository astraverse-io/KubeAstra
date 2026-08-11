"use client";

import { useState } from "react";
import ResourceGraph from "./ResourceGraph";
import { copyToClipboard } from "../lib/clipboard";

interface Props {
  tool: string;
  result: Record<string, unknown>;
  footerSlot?: React.ReactNode;
}

interface ResourceGraphData {
  namespace?: string;
  nodes?: Array<{
    id: string;
    label: string;
    type: string;
    status?: string;
    meta?: Record<string, unknown>;
  }>;
  edges?: Array<{ source: string; target: string; kind?: string }>;
  summary?: Record<string, number>;
}

/* ── shared sub-components ───────────────────────────────────── */

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => { copyToClipboard(text).then(success => { if (success) { setCopied(true); setTimeout(() => setCopied(false), 1500); } }); }}
      style={{ fontSize: "0.75rem", padding: "0.125rem 0.5rem", borderRadius: "0.25rem", transition: "background-color 0.15s", background: "var(--paper-3)", color: "var(--ink-2)", border: "1px solid var(--rule)" }}
    >
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}

function Badge({ text, color }: { text: string; color: "orange" | "green" | "yellow" | "red" | "muted" }) {
  const styles: Record<string, React.CSSProperties> = {
    orange: { background: "var(--brand-bg)", color: "var(--brand)", border: "1px solid var(--brand-bd)" },
    green: { background: "rgba(34,197,94,0.1)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.25)" },
    yellow: { background: "rgba(245,158,11,0.1)", color: "var(--warning)", border: "1px solid rgba(245,158,11,0.25)" },
    red: { background: "rgba(239,68,68,0.1)", color: "var(--danger)", border: "1px solid rgba(239,68,68,0.25)" },
    muted: { background: "var(--paper-3)", color: "var(--ink-3)", border: "1px solid var(--rule)" },
  };
  return (
    <span style={{ display: "inline-block", padding: "0.125rem 0.5rem", borderRadius: "0.25rem", fontSize: "0.75rem", fontWeight: 600, ...styles[color] }}>
      {text}
    </span>
  );
}

function severityBadgeColor(s: unknown): "orange" | "green" | "yellow" | "red" | "muted" {
  const v = String(s ?? "").toLowerCase();
  if (v === "critical" || v === "high") return "red";
  if (v === "medium") return "yellow";
  return "green";
}

export function CodeBlock({ code }: { code: string }) {
  return (
    <div style={{ position: "relative", marginTop: "0.25rem", marginBottom: "0.5rem" }} className="group">
      <pre
        style={{ borderRadius: "0.5rem", padding: "0.75rem", fontSize: "0.75rem", overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word", background: "var(--paper)", border: "1px solid var(--rule)", color: "#4ADE80" }}
      >
        {code}
      </pre>
      <div style={{ position: "absolute", top: "0.5rem", right: "0.5rem", transition: "opacity 0.15s" }} className="opacity-0 group-hover:opacity-100">
        <CopyButton text={code} />
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: "0.75rem" }}>
      <p style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "0.25rem", color: "var(--ink-3)", margin: 0 }}>{title}</p>
      {children}
    </div>
  );
}

function ResourceName({ children }: { children: React.ReactNode }) {
  return <span style={{ fontFamily: "var(--mono)", fontSize: "0.75rem", color: "var(--brand)" }}>{children}</span>;
}

function asRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value as Record<string, unknown>[] : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function formatEvidenceValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(formatEvidenceValue).filter(Boolean).join(", ");
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
  return String(value);
}

function formatLabels(labels: unknown): string {
  if (!labels || typeof labels !== "object") return "";
  return Object.entries(labels as Record<string, unknown>).map(([k, v]) => `${k}=${String(v)}`).join(", ");
}

function formatPorts(ports: unknown): string {
  if (!Array.isArray(ports)) return "";
  return ports.map((p) => {
    if (typeof p === "string") return p;
    if (!p || typeof p !== "object") return "";
    const port = p as Record<string, unknown>;
    const name = port.name ? `${String(port.name)} ` : "";
    const target = port.target_port ? `->${String(port.target_port)}` : "";
    const node = port.node_port ? ` node:${String(port.node_port)}` : "";
    return `${name}${String(port.port ?? "")}${target}/${String(port.protocol ?? "TCP")}${node}`.trim();
  }).filter(Boolean).join(", ");
}

function renderKeyValueGrid(items: Array<[string, unknown]>) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0.25rem 1rem", fontSize: "0.75rem" }}>
      {items.map(([k, v]) => (
        <div key={k} style={{ display: "flex", gap: "0.25rem" }}>
          <span style={{ minWidth: "110px", color: "var(--ink-3)" }}>{k}:</span>
          <span style={{ color: "var(--ink-2)", wordBreak: "break-word" }}>{String(v ?? "")}</span>
        </div>
      ))}
    </div>
  );
}

/* ── per-tool renderers ──────────────────────────────────────── */

function renderAnalyzeError(r: Record<string, unknown>) {
  return (
    <>
      {r.severity && (
        <div style={{ marginBottom: "0.5rem" }}>
          <Badge text={String(r.severity)} color={severityBadgeColor(r.severity)} />
        </div>
      )}
      {r.error_type && (
        <Section title="Error type">
          <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>{String(r.error_type)}</p>
        </Section>
      )}
      {r.root_cause && (
        <Section title="Root cause">
          <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>{String(r.root_cause)}</p>
        </Section>
      )}
      {r.solution && (
        <Section title="Solution">
          <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>{String(r.solution)}</p>
        </Section>
      )}
      {Array.isArray(r.steps) && r.steps.length > 0 && (
        <Section title="Steps">
          <ol style={{ listStyleType: "decimal", listStylePosition: "inside", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            {(r.steps as string[]).map((s, i) => (
              <li key={i} style={{ fontSize: "0.875rem", color: "var(--ink-2)" }}>{s}</li>
            ))}
          </ol>
        </Section>
      )}
      {Array.isArray(r.commands) && r.commands.length > 0 && (
        <Section title="Commands">
          {(r.commands as Array<{ command?: string; cmd?: string; description?: string } | string>).map((c, i) => {
            // support both "command" (analyze_error) and "cmd" (analyze_live_investigation)
            const cmd = typeof c === "string" ? c : (c.command ?? c.cmd ?? "");
            const desc = typeof c === "string" ? "" : c.description ?? "";
            return (
              <div key={i}>
                {desc && <p style={{ fontSize: "0.75rem", marginTop: "0.25rem", color: "var(--ink-3)", margin: 0 }}>{desc}</p>}
                {cmd && <CodeBlock code={cmd} />}
              </div>
            );
          })}
        </Section>
      )}
      {r.prevention && (
        <Section title="Prevention">
          <p className="text-sm" style={{ color: "var(--ink-2)" }}>{String(r.prevention)}</p>
        </Section>
      )}
      {r.corrected_snippet && (
        <Section title="Corrected code">
          <CodeBlock code={String(r.corrected_snippet)} />
        </Section>
      )}
      {r.corrected_file && (
        <Section title="">
          <details>
            <summary
              style={{ fontSize: "0.75rem", fontWeight: 500, cursor: "pointer", userSelect: "none", padding: "0.25rem 0", color: "var(--brand)", listStyle: "none" }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: "0.375rem" }}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                Full corrected file
              </span>
            </summary>
            <div style={{ marginTop: "0.5rem", maxHeight: "24rem", overflowY: "auto", borderRadius: "0.5rem", border: "1px solid var(--rule)" }}>
              <CodeBlock code={String(r.corrected_file)} />
            </div>
          </details>
        </Section>
      )}
    </>
  );
}

function renderPodList(r: Record<string, unknown>) {
  const pods = Array.isArray(r.pods) ? r.pods as Record<string, unknown>[] : [];
  const namespace = String(r.namespace ?? "");
  const statusFilter = r.status_filter ? String(r.status_filter) : "";
  const scope = namespace === "*" ? "across all namespaces" : namespace ? `in namespace ${namespace}` : "in this scope";
  const focusedModes = Array.isArray(r.focused_modes) ? r.focused_modes.map(String) : [];

  if (!pods.length) {
    const allSummary = r.all_pods_health_summary as Record<string, unknown> | undefined;
    const breakdown = allSummary?.status_breakdown as Record<string, number> | undefined;
    const total = Number(allSummary?.total ?? 0);
    const breakdownText = breakdown && Object.keys(breakdown).length > 0
      ? Object.entries(breakdown).map(([status, count]) => `${count} ${status}`).join(", ")
      : "";
    const message = r.error
      ? String(r.error)
      : statusFilter
        ? `There are no pods in ${statusFilter} status ${scope}.`
        : `There are no pods ${scope}.`;

    return (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
        <p style={{ fontSize: "0.875rem", color: "var(--ink)", margin: 0 }}>{message}</p>
        {statusFilter && total > 0 && (
          <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", margin: 0 }}>
            Checked {total} pods total{breakdownText ? ` (${breakdownText}).` : "."}
          </p>
        )}
      </div>
    );
  }

  if (focusedModes.length > 0) {
    const mode = focusedModes[0];
    return (
      <div style={{ overflowX: "auto" }}>
        <p style={{ fontSize: "0.875rem", margin: "0 0 0.5rem 0", color: "var(--ink)" }}>
          Focused pod {mode} view for {pods.length} pod{pods.length === 1 ? "" : "s"} {scope}.
        </p>
        <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
              <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Pod</th>
              <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Namespace</th>
              <th style={{ paddingBottom: "0.25rem" }}>Details</th>
            </tr>
          </thead>
          <tbody>
            {pods.map((p, i) => {
              const details = mode === "labels"
                ? formatLabels(p.labels)
                : mode === "images"
                  ? [...asRecords(p.containers), ...asRecords(p.init_containers)].map(c => `${String(c.name ?? "")}: ${String(c.image ?? "")}`).join(", ")
                  : mode === "resources"
                    ? [...asRecords(p.containers), ...asRecords(p.init_containers)].map(c => `${String(c.name ?? "")}: ${JSON.stringify(c.resources ?? {})}`).join(", ")
                    : `node=${String(p.node_name ?? "")} selector=${formatLabels(p.node_selector)}`;
              return (
                <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(p.name ?? "")}</ResourceName></td>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0", color: "var(--ink-3)" }}>{String(p.namespace ?? "")}</td>
                  <td style={{ padding: "0.25rem 0", color: "var(--ink-2)" }}>{details || "none"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div style={{ overflowX: "auto" }}>
      {statusFilter && (
        <p style={{ fontSize: "0.875rem", marginBottom: "0.5rem", color: "var(--ink)", margin: "0 0 0.5rem 0" }}>
          Found {pods.length} pod{pods.length === 1 ? "" : "s"} in {statusFilter} status {scope}.
        </p>
      )}
      <table style={{ width: "100%", fontSize: "0.75rem", marginTop: "0.5rem", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Status</th>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Ready</th>
            <th style={{ paddingBottom: "0.25rem" }}>Restarts</th>
          </tr>
        </thead>
        <tbody>
          {pods.map((p, i) => {
            const status = String(p.status ?? "");
            const isOk = status === "Running";
            return (
              <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(p.name ?? "")}</ResourceName></td>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontWeight: 600, fontSize: "0.75rem", color: isOk ? "var(--success)" : "var(--danger)" }}>{status}</td>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{String(p.ready ?? "")}</td>
                <td style={{ padding: "0.25rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{String(p.restarts ?? "0")}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function renderLogs(r: Record<string, unknown>) {
  return <CodeBlock code={String(r.logs ?? r.output ?? "No logs")} />;
}

function renderEvents(r: Record<string, unknown>) {
  const events = Array.isArray(r.events) ? r.events as Record<string, unknown>[] : [];
  if (!events.length) return <p style={{ fontSize: "0.875rem", fontStyle: "italic", color: "var(--ink-3)", margin: 0 }}>No events found.</p>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginTop: "0.25rem" }}>
      {events.map((e, i) => {
        const isWarn = String(e.type ?? "").toLowerCase() === "warning";
        return (
          <div
            key={i}
            style={{
              padding: "0.5rem", borderRadius: "0.25rem", fontSize: "0.75rem",
              borderLeft: `2px solid ${isWarn ? "var(--warning)" : "var(--brand)"}`,
              background: isWarn ? "rgba(245,158,11,0.06)" : "var(--brand-bg)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.125rem" }}>
              <span style={{ fontWeight: 600, color: isWarn ? "var(--warning)" : "var(--brand)" }}>
                {String(e.type ?? "Normal")}
              </span>
              <span style={{ color: "var(--ink-3)" }}>{String(e.reason ?? "")}</span>
              <span style={{ marginLeft: "auto", color: "var(--ink-3)" }}>{String(e.age ?? e.first_time ?? "")}</span>
            </div>
            <p style={{ color: "var(--ink-2)", margin: 0 }}>{String(e.message ?? "")}</p>
          </div>
        );
      })}
    </div>
  );
}

function renderInvestigate(r: Record<string, unknown>) {
  return (
    <>
      {Array.isArray(r.steps_run) && (
        <Section title="Steps completed">
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.25rem" }}>
            {(r.steps_run as string[]).map((s, i) => (
              <span key={i} style={{ padding: "0.125rem 0.5rem", borderRadius: "0.25rem", fontSize: "0.75rem", background: "var(--brand-bg)", color: "var(--brand)", border: "1px solid var(--brand-bd)" }}>{s}</span>
            ))}
          </div>
        </Section>
      )}
      {r.pod_info && typeof r.pod_info === "object" && (
        <Section title="Pod info">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", columnGap: "1rem", fontSize: "0.75rem", marginTop: "0.25rem" }}>
            {Object.entries(r.pod_info as Record<string, unknown>).slice(0, 8).map(([k, v]) => (
              <div key={k} style={{ display: "flex", gap: "0.25rem", padding: "0.125rem 0" }}>
                <span style={{ minWidth: "90px", color: "var(--ink-3)" }}>{k}:</span>
                <span style={{ color: "var(--ink)" }}>{String(v)}</span>
              </div>
            ))}
          </div>
        </Section>
      )}
      {r.pod_spec_summary && typeof r.pod_spec_summary === "object" && (() => {
        const spec = r.pod_spec_summary as Record<string, unknown>;
        return (
          <Section title="Pod Spec Summary">
            {renderKeyValueGrid([
              ["service account", spec.service_account_name],
              ["node", spec.node_name],
              ["labels", formatLabels(spec.labels)],
              ["images", Array.isArray(spec.images) ? spec.images.join(", ") : ""],
              ["node selector", formatLabels(spec.node_selector)],
              ["containers", asRecords(spec.containers).map(c => `${String(c.name ?? "")}: ${String(c.image ?? "")}`).join(", ")],
            ])}
          </Section>
        );
      })()}
      {r.logs && typeof r.logs === "object" && (
        <Section title="Logs">
          {Object.entries(r.logs as Record<string, unknown>).map(([container, lines]) => (
            <div key={container}>
              <p style={{ fontSize: "0.75rem", marginTop: "0.25rem", color: "var(--ink-3)", margin: "0.25rem 0 0 0" }}>Container: {container}</p>
              <CodeBlock code={String(lines)} />
            </div>
          ))}
        </Section>
      )}
      {Array.isArray(r.container_log_findings) && r.container_log_findings.length > 0 && (
        <Section title="Container Findings">
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {asRecords(r.container_log_findings).map((finding, i) => {
              const current = isRecord(finding.logs_current) ? finding.logs_current : {};
              const previous = isRecord(finding.logs_previous) ? finding.logs_previous : {};
              const excerpt = String(previous.excerpt || current.excerpt || "");
              return (
                <div key={i} style={{ borderRadius: "0.375rem", border: "1px solid var(--rule)", padding: "0.5rem", background: "var(--paper-3)" }}>
                  <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: excerpt ? "0.375rem" : 0 }}>
                    <ResourceName>{String(finding.container ?? "container")}</ResourceName>
                    {finding.reason ? <Badge text={String(finding.reason)} color="red" /> : null}
                    {finding.restart_count !== undefined && finding.restart_count !== null ? (
                      <span style={{ color: "var(--ink-3)", fontSize: "0.75rem" }}>restarts: {String(finding.restart_count)}</span>
                    ) : null}
                  </div>
                  {excerpt ? (
                    <p style={{ color: "var(--ink-2)", fontSize: "0.75rem", margin: 0, whiteSpace: "pre-wrap" }}>
                      {excerpt.length > 500 ? `${excerpt.slice(0, 500)}...` : excerpt}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </Section>
      )}
      {r.evidence_summary && typeof r.evidence_summary === "object" && (() => {
        const evidence = r.evidence_summary as Record<string, unknown>;
        const items = Array.isArray(evidence.evidence) ? evidence.evidence : [];
        const checks = Array.isArray(evidence.dependency_checks) ? evidence.dependency_checks : [];
        return (
          <Section title="Verified Evidence">
            {evidence.suspected_root_cause ? (
              <p style={{ fontSize: "0.75rem", fontWeight: 500, color: "var(--ink)", margin: 0 }}>
                {String(evidence.suspected_root_cause)}
              </p>
            ) : null}
            {items.length > 0 && (
              <ul style={{ listStyleType: "disc", paddingLeft: "1rem", marginTop: "0.5rem", fontSize: "0.75rem", color: "var(--ink-2)" }}>
                {items.map((item, i) => <li key={i}>{formatEvidenceValue(item)}</li>)}
              </ul>
            )}
            {checks.length > 0 && (
              <div style={{ marginTop: "0.5rem", display: "flex", flexDirection: "column", gap: "0.25rem" }}>
                {(checks as Record<string, unknown>[]).map((check, i) => (
                  <div key={i} style={{ fontSize: "0.75rem", borderRadius: "0.25rem", padding: "0.25rem 0.5rem", background: "var(--paper-3)", color: "var(--ink-2)" }}>
                    <ResourceName>{String(check.target ?? check.service ?? "dependency")}</ResourceName>
                    {" service_exists="}{String(check.service_exists ?? check.checked ?? "unknown")}
                    {" endpoints_exist="}{String(check.endpoints_exist ?? "unknown")}
                  </div>
                ))}
              </div>
            )}
            {evidence.suggested_fix ? (
              <p style={{ fontSize: "0.75rem", marginTop: "0.5rem", color: "var(--ink-2)", margin: "0.5rem 0 0 0" }}>
                Suggested fix: {String(evidence.suggested_fix)}
              </p>
            ) : null}
          </Section>
        );
      })()}
      {r.ai && typeof r.ai === "object" && (() => {
        const aiObj = r.ai as Record<string, unknown>;
        // ai_enabled=false means no API key configured
        if (!aiObj.ai_enabled) {
          return (
            <Section title="AI Analysis">
              <p style={{ fontSize: "0.75rem", fontStyle: "italic", color: "var(--ink-3)", margin: 0 }}>
                {String(aiObj.message ?? "AI analysis not available — set GEMINI_API_KEY in backend/.env")}
              </p>
            </Section>
          );
        }
        // ai_analysis may be null if Gemini call failed
        const analysis = aiObj.ai_analysis as Record<string, unknown> | null;
        if (!analysis) {
          return (
            <Section title="AI Analysis">
              <p style={{ fontSize: "0.75rem", fontStyle: "italic", color: "var(--ink-3)", margin: 0 }}>
                {String(aiObj.error ?? "AI analysis failed — check backend logs")}
              </p>
            </Section>
          );
        }
        return (
          <Section title="AI Analysis">
            {renderAnalyzeError(analysis)}
          </Section>
        );
      })()}
    </>
  );
}

function renderInvestigateWorkload(r: Record<string, unknown>) {
  return (
    <>
      <Section title="Investigation Target">
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", fontSize: "0.75rem" }}>
          <Badge text={String(r.workload_type ?? "workload")} color="muted" />
          <ResourceName>{String(r.workload_name ?? "")}</ResourceName>
          <span style={{ color: "var(--ink-3)" }}>in namespace {String(r.namespace ?? "")}</span>
        </div>
      </Section>

      {r.workload_summary && typeof r.workload_summary === "object" && (
        <Section title="Workload Summary">
          {renderDeployment(r.workload_summary as Record<string, unknown>)}
        </Section>
      )}
      
      {r.related_pods_summary && typeof r.related_pods_summary === "object" && (
        <Section title={`Associated Pods (${Number((r.related_pods_summary as Record<string, unknown>).pod_count ?? 0)})`}>
          {asRecords((r.related_pods_summary as Record<string, unknown>).pods).length > 0 ? (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", fontSize: "0.75rem", marginTop: "0.5rem", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
                    <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th>
                    <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Status</th>
                    <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Ready</th>
                  </tr>
                </thead>
                <tbody>
                  {asRecords((r.related_pods_summary as Record<string, unknown>).pods).map((p, i) => {
                    const status = String(p.status ?? p.phase ?? "");
                    const isOk = status === "Running";
                    const isReady = String(p.ready ?? "");
                    return (
                      <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                        <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "11px", whiteSpace: "nowrap", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis" }}><ResourceName>{String(p.name ?? "")}</ResourceName></td>
                        <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontWeight: 600, fontSize: "0.75rem", color: isOk ? "var(--success)" : "var(--warning)" }}>{status}</td>
                        <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{isReady}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p style={{ fontSize: "0.75rem", fontStyle: "italic", marginTop: "0.25rem", color: "var(--ink-3)" }}>No matching pods found</p>
          )}
        </Section>
      )}

      {r.ai && typeof r.ai === "object" && (() => {
        const aiObj = r.ai as Record<string, unknown>;
        if (!aiObj.ai_enabled) return null;
        const analysis = aiObj.ai_analysis as Record<string, unknown> | null;
        if (!analysis) return null;
        return (
          <Section title="AI Workload Analysis">
            {renderAnalyzeError(analysis)}
          </Section>
        );
      })()}
    </>
  );
}

function renderAnalyzeNamespace(r: Record<string, unknown>) {
  return (
    <>
      <Section title="Namespace Health Check">
        <p style={{ fontSize: "0.75rem", color: "var(--ink-2)", margin: 0 }}>
          Analysis for namespace <ResourceName>{String(r.namespace ?? "")}</ResourceName>
        </p>
      </Section>

      {r.issue_summary && typeof r.issue_summary === "object" && (() => {
        const issue = r.issue_summary as Record<string, unknown>;
        return (
          <Section title="Deterministic Issue Summary">
            {renderKeyValueGrid([
              ["unhealthy pods", issue.unhealthy_pod_count],
              ["unavailable workloads", issue.unavailable_workload_count],
              ["warning groups", issue.warning_event_group_count],
              ["services without ready endpoints", issue.services_without_ready_endpoints_count],
              ["selector mismatches", issue.services_with_selector_mismatch_count],
            ])}
          </Section>
        );
      })()}

      {r.ai && typeof r.ai === "object" && (() => {
        const aiObj = r.ai as Record<string, unknown>;
        if (!aiObj.ai_enabled) return null;
        const analysis = aiObj.ai_analysis as Record<string, unknown> | null;
        if (!analysis) return null;
        return (
          <Section title="Holistic AI Diagnosis">
            {renderAnalyzeError(analysis)}
          </Section>
        );
      })()}
      
      {r.resources && typeof r.resources === "object" && (
        <div style={{ marginTop: "1rem", borderTop: "1px solid var(--rule)", paddingTop: "1rem" }}>
          <p style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: "0.5rem", color: "var(--ink-3)" }}>Raw Resources</p>
          {renderNamespaceResources(r.resources as Record<string, unknown>)}
        </div>
      )}
    </>
  );
}

function renderContextList(r: Record<string, unknown>) {
  const contexts = Array.isArray(r.contexts) ? r.contexts as Record<string, unknown>[] : [];
  const current = String(r.current_context ?? "");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem", marginTop: "0.25rem" }}>
      {contexts.map((c, i) => {
        const name = String(c.name ?? c);
        const isActive = name === current;
        return (
          <div
            key={i}
            style={{
              display: "flex", alignItems: "center", gap: "0.5rem", padding: "0.5rem", borderRadius: "0.25rem", fontSize: "0.875rem",
              background: isActive ? "var(--brand-bg)" : "var(--paper-3)",
              border: `1px solid ${isActive ? "var(--brand-bd)" : "var(--rule)"}`,
            }}
          >
            <span style={{ width: "0.5rem", height: "0.5rem", borderRadius: "50%", display: "inline-block", background: isActive ? "var(--success)" : "var(--rule)" }} />
            <span style={{ fontFamily: "var(--mono)", color: "var(--ink)" }}>{name}</span>
            {isActive && <Badge text="active" color="green" />}
          </div>
        );
      })}
    </div>
  );
}

function renderListServices(r: Record<string, unknown>) {
  const services = Array.isArray(r.services) ? r.services as Record<string, unknown>[] : [];
  if (!services.length) return <p style={{ fontSize: "0.875rem", fontStyle: "italic", color: "var(--ink-3)" }}>No services found in this namespace.</p>;
  return (
    <div style={{ overflowX: "auto", marginTop: "0.25rem" }}>
      <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Type</th>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Cluster IP</th>
            <th style={{ paddingBottom: "0.25rem" }}>Ports</th>
          </tr>
        </thead>
        <tbody>
          {services.map((s, i) => (
            <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
              <td style={{ padding: "0.375rem 1rem 0.375rem 0" }}><ResourceName>{String(s.name ?? "")}</ResourceName></td>
              <td style={{ padding: "0.375rem 1rem 0.375rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{String(s.type ?? "")}</td>
              <td style={{ padding: "0.375rem 1rem 0.375rem 0", fontFamily: "var(--mono)", fontSize: "11px", color: "var(--ink-3)" }}>{String(s.cluster_ip ?? "")}</td>
              <td style={{ padding: "0.375rem 0", fontSize: "0.75rem", color: "var(--ink-3)" }}>{formatPorts(s.ports)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ fontSize: "0.75rem", marginTop: "0.5rem", color: "var(--ink-3)" }}>{services.length} services in namespace {String(r.namespace ?? "")}</p>
    </div>
  );
}

// ── Shared Helm helpers ──────────────────────────────────────────────────────

// Tool results arrive wrapped in a ToolEnvelope ({ evidence: { items: [...] } }).
// Unwrap to the payload that matches `predicate`, falling back to the raw result
// so a flat (un-enveloped) shape also renders.
function unwrapHelm(
  r: Record<string, unknown>,
  predicate: (item: Record<string, unknown>) => boolean,
): Record<string, unknown> {
  const evidence = isRecord(r.evidence) ? r.evidence : undefined;
  const items = asRecords(evidence?.items);
  return items.find(predicate) ?? (items.length === 1 ? items[0] : r);
}

function helmStatusColor(status: unknown): "green" | "red" | "yellow" | "muted" {
  const s = String(status ?? "").toLowerCase();
  if (s === "deployed") return "green";
  if (s === "failed") return "red";
  if (s.startsWith("pending")) return "yellow";
  return "muted"; // superseded / uninstalled / uninstalling / unknown
}

function HelmRevisionList({ revisions }: { revisions: Record<string, unknown>[] }) {
  if (revisions.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      {revisions.map((rev, i) => (
        <div key={`${String(rev.revision ?? i)}`} style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.75rem" }}>
          <span style={{ color: "var(--ink-3)", minWidth: "3.5rem" }}>rev {String(rev.revision ?? "")}</span>
          <Badge text={String(rev.status ?? "unknown")} color={helmStatusColor(rev.status)} />
          <span style={{ color: "var(--ink-2)", wordBreak: "break-word" }}>{String(rev.chart ?? "")}</span>
          {rev.updated ? <span style={{ color: "var(--ink-3)" }}>{String(rev.updated)}</span> : null}
        </div>
      ))}
    </div>
  );
}

// Redacted, size-capped text section (values / manifest / hooks / notes).
function HelmTextBlock({ label, text }: { label: string; text: string }) {
  return (
    <div style={{ marginTop: "0.5rem" }}>
      <p style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--ink-2)", margin: "0 0 0.25rem 0" }}>{label}</p>
      <pre style={{ margin: 0, padding: "0.5rem", borderRadius: "0.375rem", background: "var(--paper-3)", border: "1px solid var(--rule)", fontFamily: "var(--mono)", fontSize: "11px", color: "var(--ink-2)", overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: "20rem" }}>
        {text}
      </pre>
    </div>
  );
}

function isHelmAvailabilityProblem(src: Record<string, unknown>) {
  return src.available === false && (
    src.reason === "helm_unavailable" ||
    src.reason === "helm_check_failed" ||
    Boolean(src.remediation_hint)
  );
}

function renderHelmAvailabilityProblem(src: Record<string, unknown>, title = "Helm Unavailable") {
  const message = src.remediation_hint ?? src.message ?? src.error ?? "Helm is not available on the active target.";
  return (
    <Section title={title}>
      <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>{String(message)}</p>
      {src.error ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", marginTop: "0.25rem" }}>
          {String(src.error)}
        </p>
      ) : null}
    </Section>
  );
}

function renderHelmReleases(r: Record<string, unknown>) {
  const evidence = isRecord(r.evidence) ? r.evidence : undefined;
  const items = asRecords(evidence?.items);
  const source = items.find((item) => Array.isArray(item.releases)) ?? r;
  const releases = asRecords(source.releases);
  const filterCriteria = isRecord(evidence?.filter_criteria) ? evidence?.filter_criteria : undefined;
  const namespace = source.namespace ?? filterCriteria?.namespace ?? r.namespace;
  const available = source.available ?? r.available;
  const releaseCount = source.release_count ?? releases.length;
  const statusFilter = source.status_filter ?? r.status_filter;
  const unavailableMessage = r.remediation_hint ?? source.remediation_hint ?? r.message ?? source.message ?? r.error ?? source.error;

  if (available === false) return renderHelmAvailabilityProblem({ ...source, remediation_hint: unavailableMessage });

  return (
    <Section title="Helm Releases">
      <p style={{ fontSize: "0.875rem", color: "var(--ink)", margin: "0 0 0.5rem 0" }}>
        Found {String(releaseCount)} Helm {Number(releaseCount) === 1 ? "release" : "releases"}
        {namespace ? <> in <ResourceName>{String(namespace)}</ResourceName></> : null}
        {statusFilter ? <> with status <ResourceName>{String(statusFilter)}</ResourceName></> : null}.
      </p>
      {releases.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.375rem" }}>
          {releases.map((release, i) => (
            <div
              key={`${String(release.name ?? "release")}-${i}`}
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(150px, 1.2fr) minmax(120px, 1fr) minmax(70px, 0.5fr)",
                gap: "0.5rem",
                alignItems: "start",
                padding: "0.5rem",
                borderRadius: "0.375rem",
                background: "var(--paper-3)",
                border: "1px solid var(--rule)",
                fontSize: "0.75rem",
              }}
            >
              <div>
                <ResourceName>{String(release.name ?? "")}</ResourceName>
                <div style={{ color: "var(--ink-3)", marginTop: "0.125rem" }}>
                  rev {String(release.revision ?? "")}
                </div>
              </div>
              <div style={{ color: "var(--ink-2)", wordBreak: "break-word" }}>
                {String(release.chart ?? "unknown chart")}
                {release.app_version ? (
                  <div style={{ color: "var(--ink-3)", marginTop: "0.125rem" }}>
                    app {String(release.app_version)}
                  </div>
                ) : null}
              </div>
              <div>
                <Badge
                  text={String(release.status ?? "unknown")}
                  color={helmStatusColor(release.status)}
                />
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p style={{ fontSize: "0.875rem", fontStyle: "italic", color: "var(--ink-3)", margin: 0 }}>No Helm releases found.</p>
      )}
    </Section>
  );
}

function renderHelmRelease(r: Record<string, unknown>) {
  const src = unwrapHelm(r, (it) => isRecord(it.sections) || "sections" in it);
  const sections = isRecord(src.sections) ? src.sections : {};
  const errors = isRecord(src.errors) ? src.errors : {};
  const release = src.release ?? r.release;
  const namespace = src.namespace ?? r.namespace;
  const revision = src.revision ?? null;
  const found = src.found ?? (Object.keys(sections).length > 0);

  if (isHelmAvailabilityProblem(src)) return renderHelmAvailabilityProblem(src);

  if (found === false) {
    return (
      <Section title="Helm Release">
        <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>
          Release <ResourceName>{String(release ?? "")}</ResourceName> was not found
          {namespace ? <> in <ResourceName>{String(namespace)}</ResourceName></> : null}.
        </p>
      </Section>
    );
  }

  const status = isRecord(sections.status) ? sections.status : undefined;
  const history = asRecords(sections.history);
  const metadata = isRecord(sections.metadata) ? sections.metadata : undefined;
  const textSections: Array<[string, string]> = [];
  for (const [key, label] of [["values", "Values"], ["manifest", "Manifest"], ["hooks", "Hooks"], ["notes", "Notes"]] as const) {
    if (typeof sections[key] === "string" && (sections[key] as string).length > 0) {
      textSections.push([label, sections[key] as string]);
    }
  }

  return (
    <Section title="Helm Release">
      <p style={{ fontSize: "0.875rem", color: "var(--ink)", margin: "0 0 0.5rem 0" }}>
        <ResourceName>{String(release ?? "")}</ResourceName>
        {namespace ? <> in <ResourceName>{String(namespace)}</ResourceName></> : null}
        {revision != null ? <> · revision {String(revision)}</> : null}
      </p>

      {status ? (
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.5rem", fontSize: "0.75rem" }}>
          <Badge text={String(status.status ?? "unknown")} color={helmStatusColor(status.status)} />
          {status.chart ? <span style={{ color: "var(--ink-2)" }}>{String(status.chart)}{status.chart_version ? `-${String(status.chart_version)}` : ""}</span> : null}
          {status.app_version ? <span style={{ color: "var(--ink-3)" }}>app {String(status.app_version)}</span> : null}
          {status.last_deployed ? <span style={{ color: "var(--ink-3)" }}>{String(status.last_deployed)}</span> : null}
        </div>
      ) : null}

      {metadata && !status ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-2)", margin: "0 0 0.5rem 0" }}>
          {String(metadata.chart ?? "")}{metadata.version ? `-${String(metadata.version)}` : ""}
          {metadata.app_version ? ` · app ${String(metadata.app_version)}` : ""}
        </p>
      ) : null}

      {history.length > 0 ? (
        <div style={{ marginBottom: "0.5rem" }}>
          <p style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--ink-2)", margin: "0 0 0.25rem 0" }}>History</p>
          <HelmRevisionList revisions={history} />
        </div>
      ) : null}

      {textSections.map(([label, text]) => (
        <HelmTextBlock key={label} label={label} text={text} />
      ))}

      {Object.keys(errors).length > 0 ? (
        <p style={{ fontSize: "0.75rem", color: "var(--warning)", marginTop: "0.5rem" }}>
          Some sections could not be read: {Object.keys(errors).join(", ")}.
        </p>
      ) : null}
    </Section>
  );
}

function renderHelmDiff(r: Record<string, unknown>) {
  const src = unwrapHelm(r, (it) => "diff" in it || "from_revision" in it);
  const errors = isRecord(src.errors) ? src.errors : undefined;
  const release = src.release ?? r.release;
  const section = src.section ?? "values";
  const from = src.from_revision;
  const to = src.to_revision;
  const diff = typeof src.diff === "string" ? src.diff : null;
  const changed = src.changed;
  const truncated = src.truncated;
  const secretCaveat = src.redaction_may_hide_secret_only_changes;

  if (isHelmAvailabilityProblem(src)) return renderHelmAvailabilityProblem(src, "Helm Revision Diff");

  return (
    <Section title="Helm Revision Diff">
      <p style={{ fontSize: "0.875rem", color: "var(--ink)", margin: "0 0 0.5rem 0" }}>
        <ResourceName>{String(release ?? "")}</ResourceName> · {String(section)} · revision {String(from ?? "?")} → {String(to ?? "?")}
      </p>

      {diff == null ? (
        <p style={{ fontSize: "0.875rem", color: "var(--warning)" }}>
          Could not produce the diff{errors && Object.keys(errors).length > 0 ? `: ${Object.values(errors).join("; ")}` : "."}
        </p>
      ) : changed === false ? (
        <p style={{ fontSize: "0.875rem", fontStyle: "italic", color: "var(--ink-2)", margin: 0 }}>
          No non-secret changes between these revisions.
        </p>
      ) : (
        <HelmTextBlock label="Diff" text={diff} />
      )}

      {truncated ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", marginTop: "0.25rem" }}>Diff truncated for display.</p>
      ) : null}
      {secretCaveat ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", marginTop: "0.25rem" }}>
          Compared after redaction — a change limited to secret values would not appear here.
        </p>
      ) : null}
    </Section>
  );
}

function renderInvestigateHelmRelease(r: Record<string, unknown>) {
  const src = unwrapHelm(r, (it) => "release_healthy" in it || isRecord(it.pod_health));
  const release = src.release ?? r.release;
  const namespace = src.namespace ?? r.namespace;
  const found = src.found;

  if (isHelmAvailabilityProblem(src)) return renderHelmAvailabilityProblem(src, "Helm Release Investigation");

  if (found === false) {
    return (
      <Section title="Helm Release Investigation">
        <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>
          Release <ResourceName>{String(release ?? "")}</ResourceName> was not found
          {namespace ? <> in <ResourceName>{String(namespace)}</ResourceName></> : null}.
        </p>
      </Section>
    );
  }

  const status = isRecord(src.status) ? src.status : undefined;
  const recentRevisions = asRecords(src.recent_revisions);
  const priorFailed = asRecords(src.prior_failed_revisions);
  const workloads = asRecords(src.workloads);
  const podHealth = isRecord(src.pod_health) ? src.pod_health : undefined;
  const warnings = isRecord(src.recent_warnings) ? src.recent_warnings : undefined;
  const healthy = src.release_healthy;
  const unhealthyPods = asRecords(podHealth?.unhealthy);
  const warningList = asRecords(warnings?.warnings);

  return (
    <Section title="Helm Release Investigation">
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.5rem" }}>
        <ResourceName>{String(release ?? "")}</ResourceName>
        {namespace ? <span style={{ fontSize: "0.75rem", color: "var(--ink-3)" }}>in {String(namespace)}</span> : null}
        {healthy === true ? <Badge text="healthy" color="green" /> : healthy === false ? <Badge text="unhealthy" color="red" /> : null}
        {status?.status ? <Badge text={String(status.status)} color={helmStatusColor(status.status)} /> : null}
      </div>

      {status ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-2)", margin: "0 0 0.5rem 0" }}>
          {String(status.chart ?? "")}{status.chart_version ? `-${String(status.chart_version)}` : ""}
          {status.app_version ? ` · app ${String(status.app_version)}` : ""}
          {status.revision != null ? ` · rev ${String(status.revision)}` : ""}
        </p>
      ) : null}

      {podHealth ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-2)", margin: "0 0 0.25rem 0" }}>
          Pods: {String(podHealth.unhealthy_count ?? 0)} unhealthy of {String(podHealth.pod_count ?? 0)}
          {podHealth.scoped === false ? <span style={{ color: "var(--ink-3)" }}> (namespace-wide — could not scope to this release)</span> : null}
        </p>
      ) : null}
      {unhealthyPods.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.125rem", marginBottom: "0.5rem" }}>
          {unhealthyPods.map((p, i) => (
            <div key={`${String(p.name ?? i)}`} style={{ fontSize: "0.75rem", color: "var(--ink-2)" }}>
              <ResourceName>{String(p.name ?? "")}</ResourceName>{" "}
              <Badge text={String(p.status ?? "")} color="red" />
              {p.restarts != null ? <span style={{ color: "var(--ink-3)" }}> · {String(p.restarts)} restarts</span> : null}
            </div>
          ))}
        </div>
      ) : null}

      {warningList.length > 0 ? (
        <div style={{ marginBottom: "0.5rem" }}>
          <p style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--ink-2)", margin: "0 0 0.25rem 0" }}>
            Recent warnings ({String(warnings?.count ?? warningList.length)})
          </p>
          {warningList.map((w, i) => (
            <div key={i} style={{ fontSize: "0.75rem", color: "var(--ink-2)", marginBottom: "0.125rem" }}>
              <span style={{ fontWeight: 600 }}>{String(w.reason ?? "")}</span>
              {w.name ? <span style={{ color: "var(--ink-3)" }}> · {String(w.kind ?? "")}/{String(w.name)}</span> : null}
              <div style={{ color: "var(--ink-3)" }}>{String(w.message ?? "")}</div>
            </div>
          ))}
        </div>
      ) : null}

      {priorFailed.length > 0 ? (
        <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", margin: "0 0 0.25rem 0" }}>
          Prior failed revisions (historical): {priorFailed.map((rev) => `rev ${String(rev.revision ?? "")}`).join(", ")}.
        </p>
      ) : null}

      {recentRevisions.length > 0 ? (
        <div style={{ marginBottom: "0.25rem" }}>
          <p style={{ fontSize: "0.75rem", fontWeight: 600, color: "var(--ink-2)", margin: "0 0 0.25rem 0" }}>Recent revisions</p>
          <HelmRevisionList revisions={recentRevisions} />
        </div>
      ) : null}

      <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", margin: "0.25rem 0 0 0" }}>
        {String(workloads.length)} workload{workloads.length === 1 ? "" : "s"} · {String(src.resource_count ?? 0)} resources
      </p>
    </Section>
  );
}

function renderNamespaceResources(r: Record<string, unknown>) {
  const summary = r.summary as Record<string, number> | undefined;
  const pods = Array.isArray(r.pods) ? r.pods as Record<string, unknown>[] : [];
  const services = Array.isArray(r.services) ? r.services as Record<string, unknown>[] : [];
  const deployments = Array.isArray(r.deployments) ? r.deployments as Record<string, unknown>[] : [];
  const statefulsets = Array.isArray(r.statefulsets) ? r.statefulsets as Record<string, unknown>[] : [];
  const daemonsets = Array.isArray(r.daemonsets) ? r.daemonsets as Record<string, unknown>[] : [];
  const configmaps = Array.isArray(r.configmaps) ? r.configmaps : [];
  const pvcs = asRecords(r.persistent_volume_claims);
  const ingresses = Array.isArray(r.ingresses) ? r.ingresses as Record<string, unknown>[] : [];

  const SectionHeader = ({ title, count }: { title: string; count: number }) => (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "1rem", marginBottom: "0.25rem" }}>
      <p style={{ fontSize: "0.75rem", fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--ink-3)", margin: 0 }}>{title}</p>
      <span style={{ fontSize: "10px", padding: "0.125rem 0.375rem", borderRadius: "9999px", background: "var(--paper-3)", color: "var(--ink-3)", border: "1px solid var(--rule)" }}>{count}</span>
    </div>
  );

  return (
    <>
      {summary && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem", marginBottom: "0.75rem" }}>
          {Object.entries(summary).filter(([, v]) => v > 0).map(([k, v]) => (
            <span key={k} style={{ fontSize: "0.75rem", padding: "0.125rem 0.5rem", borderRadius: "0.5rem", background: "var(--paper-3)", color: "var(--ink-2)", border: "1px solid var(--rule)" }}>
              {v} {k}
            </span>
          ))}
        </div>
      )}

      {deployments.length > 0 && (
        <>
          <SectionHeader title="Deployments" count={deployments.length} />
          <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
            <thead><tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
              <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th><th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Replicas</th><th style={{ paddingBottom: "0.25rem" }}>Ready</th>
            </tr></thead>
            <tbody>{deployments.map((d, i) => {
              const ready = Number(d.ready ?? 0);
              const total = Number(d.replicas ?? 0);
              return (
                <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(d.name ?? "")}</ResourceName></td>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{total}</td>
                  <td style={{ padding: "0.25rem 0", fontWeight: 600, fontSize: "0.75rem", color: ready === total ? "var(--success)" : "var(--warning)" }}>{ready}/{total}</td>
                </tr>
              );
            })}</tbody>
          </table>
        </>
      )}

      {pods.length > 0 && (
        <>
          <SectionHeader title="Pods" count={pods.length} />
          <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
            <thead><tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
              <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th><th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Status</th><th style={{ paddingBottom: "0.25rem" }}>Restarts</th>
            </tr></thead>
            <tbody>{pods.map((p, i) => {
              const status = String(p.status ?? "");
              const isOk = status === "Running" && p.ready;
              return (
                <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "11px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "200px" }}><ResourceName>{String(p.name ?? "")}</ResourceName></td>
                  <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontWeight: 600, fontSize: "0.75rem", color: isOk ? "var(--success)" : "var(--warning)" }}>{status}</td>
                  <td style={{ padding: "0.25rem 0", fontSize: "0.75rem", color: "var(--ink-3)" }}>{String(p.restarts ?? 0)}</td>
                </tr>
              );
            })}</tbody>
          </table>
        </>
      )}

      {services.length > 0 && (
        <>
          <SectionHeader title="Services" count={services.length} />
          <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
            <thead><tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
              <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Name</th><th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Type</th><th style={{ paddingBottom: "0.25rem" }}>Ports</th>
            </tr></thead>
            <tbody>{services.map((s, i) => (
              <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(s.name ?? "")}</ResourceName></td>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0", fontSize: "0.75rem", color: "var(--ink-2)" }}>{String(s.type ?? "")}</td>
                <td style={{ padding: "0.25rem 0", fontSize: "0.75rem", color: "var(--ink-3)" }}>{formatPorts(s.ports)}</td>
              </tr>
            ))}</tbody>
          </table>
        </>
      )}

      {statefulsets.length > 0 && (
        <>
          <SectionHeader title="StatefulSets" count={statefulsets.length} />
          {statefulsets.map((s, i) => (
            <div key={i} style={{ fontSize: "0.75rem", padding: "0.125rem 0", fontFamily: "var(--mono)", color: "var(--ink-2)" }}>
              {String(s.name ?? "")} <span style={{ color: "var(--ink-3)" }}>({String(s.ready ?? 0)}/{String(s.replicas ?? 0)} ready)</span>
            </div>
          ))}
        </>
      )}

      {daemonsets.length > 0 && (
        <>
          <SectionHeader title="DaemonSets" count={daemonsets.length} />
          {daemonsets.map((d, i) => (
            <div key={i} style={{ fontSize: "0.75rem", padding: "0.125rem 0", fontFamily: "var(--mono)", color: "var(--ink-2)" }}>
              {String(d.name ?? "")} <span style={{ color: "var(--ink-3)" }}>({String(d.ready ?? 0)}/{String(d.desired ?? 0)} ready)</span>
            </div>
          ))}
        </>
      )}

      {ingresses.length > 0 && (
        <>
          <SectionHeader title="Ingresses" count={ingresses.length} />
          {ingresses.map((ing, i) => (
            <div key={i} style={{ fontSize: "0.75rem", padding: "0.125rem 0" }}>
              <ResourceName>{String(ing.name ?? "")}</ResourceName>
              {Array.isArray(ing.hosts) && ing.hosts.length > 0 && (
                <span style={{ marginLeft: "0.5rem", color: "var(--ink-3)" }}>{(ing.hosts as string[]).join(", ")}</span>
              )}
            </div>
          ))}
        </>
      )}

      {pvcs.length > 0 && (
        <>
          <SectionHeader title="PVCs" count={pvcs.length} />
          {pvcs.map((pvc, i) => (
            <div key={i} style={{ fontSize: "0.75rem", padding: "0.125rem 0", color: "var(--ink-2)" }}>
              <ResourceName>{String(pvc.name ?? "")}</ResourceName>
              <span style={{ marginLeft: "0.5rem", color: "var(--ink-3)" }}>
                {String(pvc.status ?? "")} {JSON.stringify(pvc.capacity ?? {})}
              </span>
            </div>
          ))}
        </>
      )}

      {configmaps.length > 0 && (
        <>
          <SectionHeader title="ConfigMaps" count={configmaps.length} />
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.25rem" }}>
            {configmaps.map((cm, i) => (
              <span key={i} style={{ fontSize: "10px", fontFamily: "var(--mono)", padding: "0.125rem 0.375rem", borderRadius: "0.25rem", background: "var(--paper-3)", color: "var(--ink-3)", border: "1px solid var(--rule)" }}>
                {typeof cm === "string" ? cm : String((cm as Record<string, unknown>).name ?? "")}
              </span>
            ))}
          </div>
        </>
      )}
    </>
  );
}

function renderNamespaces(r: Record<string, unknown>) {
  const namespaces = Array.isArray(r.namespaces) ? r.namespaces as Record<string, unknown>[] : [];
  if (!namespaces.length) return <p style={{ fontSize: "0.875rem", fontStyle: "italic", color: "var(--ink-3)" }}>No namespaces found.</p>;
  return (
    <div style={{ overflowX: "auto", marginTop: "0.25rem" }}>
      <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
        <thead>
          <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Namespace</th>
            <th style={{ paddingBottom: "0.25rem" }}>Status</th>
          </tr>
        </thead>
        <tbody>
          {namespaces.map((ns, i) => {
            const name = String(ns.name ?? "");
            const status = String(ns.status ?? "");
            const isActive = status === "Active";
            const isSystem = name.startsWith("kube-");
            return (
              <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                <td style={{ padding: "0.375rem 1rem 0.375rem 0" }}>
                  <span style={{ fontFamily: "var(--mono)", color: isSystem ? "var(--ink-3)" : "var(--brand)" }}>{name}</span>
                  {isSystem && <span style={{ marginLeft: "0.5rem", fontSize: "10px", padding: "0 0.25rem", borderRadius: "0.25rem", color: "var(--ink-3)", background: "var(--paper-3)" }}>system</span>}
                </td>
                <td style={{ padding: "0.375rem 0", fontWeight: 600, fontSize: "0.75rem", color: isActive ? "var(--success)" : "var(--warning)" }}>{status}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p style={{ fontSize: "0.75rem", marginTop: "0.5rem", color: "var(--ink-3)" }}>{namespaces.length} namespaces total</p>
    </div>
  );
}

function renderFindWorkload(r: Record<string, unknown>) {
  const deployments = Array.isArray(r.deployments) ? r.deployments as Record<string, unknown>[] : [];
  const pods = Array.isArray(r.pods) ? r.pods as Record<string, unknown>[] : [];
  const services = Array.isArray(r.services) ? r.services as Record<string, unknown>[] : [];
  const total = deployments.length + pods.length + services.length;

  if (total === 0) {
    return (
      <p className="text-sm italic" style={{ color: "var(--ink-3)" }}>
        No workloads found matching &quot;{String(r.query ?? "")}&quot;.
      </p>
    );
  }

  const Row = ({ name, ns, extra, badge }: { name: string; ns: string; extra?: string; badge?: string }) => (
    <div className="flex items-center gap-3 py-1.5 text-sm" style={{ borderBottom: "1px solid var(--rule)" }}>
      <ResourceName>{name}</ResourceName>
      <span className="text-xs px-1.5 py-0.5 rounded flex-shrink-0" style={{ color: "var(--ink-3)", background: "var(--paper-3)" }}>{ns}</span>
      {badge && <span className="text-xs" style={{ color: "var(--success)" }}>{badge}</span>}
      {extra && <span className="text-xs ml-auto" style={{ color: "var(--ink-3)" }}>{extra}</span>}
    </div>
  );

  return (
    <>
      {deployments.length > 0 && (
        <Section title={`Deployments (${deployments.length})`}>
          {deployments.map((d, i) => {
            const ready = d.ready !== undefined ? String(d.ready) : null;
            const replicas = d.replicas !== undefined ? String(d.replicas) : null;
            const badge = ready !== null && replicas !== null ? `${ready}/${replicas} ready` : undefined;
            return <Row key={i} name={String(d.name ?? "")} ns={String(d.namespace ?? "")} badge={badge} />;
          })}
        </Section>
      )}
      {pods.length > 0 && (
        <Section title={`Pods (${pods.length})`}>
          {pods.map((p, i) => (
            <Row key={i} name={String(p.name ?? "")} ns={String(p.namespace ?? "")} extra={String(p.phase ?? "")} />
          ))}
        </Section>
      )}
      {services.length > 0 && (
        <Section title={`Services (${services.length})`}>
          {services.map((s, i) => (
            <Row key={i} name={String(s.name ?? "")} ns={String(s.namespace ?? "")} extra={String(s.type ?? "")} />
          ))}
        </Section>
      )}
    </>
  );
}

function renderNodes(r: Record<string, unknown>) {
  const nodes = asRecords(r.nodes);
  if (!nodes.length) return <p style={{ fontSize: "0.875rem", color: "var(--ink-3)" }}>No nodes found.</p>;
  const focused = Array.isArray(r.focused_modes) ? r.focused_modes.map(String).join(", ") : "";
  return (
    <>
      {focused && <p style={{ fontSize: "0.75rem", color: "var(--ink-3)", margin: "0 0 0.5rem 0" }}>Focused node view: {focused}</p>}
      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
          <thead><tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Node</th>
            <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Status</th>
            <th style={{ paddingBottom: "0.25rem" }}>Details</th>
          </tr></thead>
          <tbody>{nodes.map((n, i) => {
            const details = n.taints
              ? `taints=${asRecords(n.taints).map(t => `${String(t.key ?? "")}:${String(t.effect ?? "")}`).join(", ") || "none"}`
              : n.addresses
                ? asRecords(n.addresses).map(a => `${String(a.type ?? "")}=${String(a.address ?? "")}`).join(", ")
                : n.conditions
                  ? asRecords(n.conditions).map(c => `${String(c.type ?? "")}=${String(c.status ?? "")}`).join(", ")
                  : formatLabels(n.labels);
            return (
              <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(n.name ?? "")}</ResourceName></td>
                <td style={{ padding: "0.25rem 1rem 0.25rem 0", color: String(n.status ?? "") === "Ready" ? "var(--success)" : "var(--ink-3)" }}>{String(n.status ?? "")}</td>
                <td style={{ padding: "0.25rem 0", color: "var(--ink-2)" }}>{details || "none"}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </>
  );
}

function renderInvestigateNode(r: Record<string, unknown>) {
  const allocated = (r.allocated ?? {}) as Record<string, unknown>;
  const capacity = (r.capacity ?? {}) as Record<string, unknown>;
  const allocatable = (r.allocatable ?? {}) as Record<string, unknown>;
  const pods = asRecords(r.pods);

  return (
    <>
      <Section title="Node CPU Allocation">
        {renderKeyValueGrid([
          ["node", r.name ?? r.query],
          ["status", r.status],
          ["allocatable CPU", `${String(allocatable.cpu ?? "")} cores`],
          ["requested CPU", `${String(allocated.cpu_requests_cores ?? 0)} cores (${String(allocated.cpu_requests_percent_of_allocatable ?? 0)}%)`],
          ["limited CPU", `${String(allocated.cpu_limits_cores ?? 0)} cores (${String(allocated.cpu_limits_percent_of_allocatable ?? 0)}%)`],
          ["non-terminated pods", allocated.non_terminated_pods],
        ])}
      </Section>

      <Section title="Memory Allocation">
        {renderKeyValueGrid([
          ["capacity", `${String(capacity.memory_gib ?? "")} GiB`],
          ["allocatable", `${String(allocatable.memory_gib ?? "")} GiB`],
          ["requested", `${String(allocated.memory_requests_gib ?? 0)} GiB (${String(allocated.memory_requests_percent_of_allocatable ?? 0)}%)`],
          ["limited", `${String(allocated.memory_limits_gib ?? 0)} GiB (${String(allocated.memory_limits_percent_of_allocatable ?? 0)}%)`],
        ])}
      </Section>

      {pods.length > 0 && (
        <Section title={`Pods Contributing Resources (${pods.length})`}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", fontSize: "0.75rem", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--ink-3)", borderBottom: "1px solid var(--rule)" }}>
                  <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Pod</th>
                  <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>Namespace</th>
                  <th style={{ paddingBottom: "0.25rem", paddingRight: "1rem" }}>CPU Req</th>
                  <th style={{ paddingBottom: "0.25rem" }}>CPU Limit</th>
                </tr>
              </thead>
              <tbody>
                {pods.slice(0, 20).map((p, i) => (
                  <tr key={i} style={{ borderBottom: "1px solid var(--rule)" }}>
                    <td style={{ padding: "0.25rem 1rem 0.25rem 0" }}><ResourceName>{String(p.name ?? "")}</ResourceName></td>
                    <td style={{ padding: "0.25rem 1rem 0.25rem 0", color: "var(--ink-3)" }}>{String(p.namespace ?? "")}</td>
                    <td style={{ padding: "0.25rem 1rem 0.25rem 0", color: "var(--ink-2)" }}>{String(p.cpu_requests_millicores ?? 0)}m</td>
                    <td style={{ padding: "0.25rem 0", color: "var(--ink-2)" }}>{String(p.cpu_limits_millicores ?? 0)}m</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
    </>
  );
}

function renderDeployment(r: Record<string, unknown>) {
  const template = (r.pod_template ?? {}) as Record<string, unknown>;
  return (
    <>
      <Section title="Deployment">
        {renderKeyValueGrid([
          ["name", r.name],
          ["namespace", r.namespace],
          ["health", r.health_status],
          ["replicas", r.replicas && typeof r.replicas === "object" ? JSON.stringify(r.replicas) : ""],
          ["selector", r.selector && typeof r.selector === "object" ? JSON.stringify(r.selector) : ""],
          ["revision", r.revision],
        ])}
      </Section>
      {(template.images || template.containers || r.images || r.containers) && (
        <Section title="Pod Template">
          {renderKeyValueGrid([
            ["labels", formatLabels(template.labels)],
            ["service account", template.service_account_name],
            ["images", Array.isArray(template.images) ? template.images.join(", ") : Array.isArray(r.images) ? r.images.join(", ") : ""],
            ["containers", [...asRecords(template.containers), ...asRecords(r.containers)].map(c => `${String(c.name ?? "")}: ${String(c.image ?? "") || JSON.stringify(c.resources ?? {})}`).join(", ")],
            ["node selector", formatLabels(template.node_selector)],
          ])}
        </Section>
      )}
    </>
  );
}

function renderService(r: Record<string, unknown>) {
  return (
    <>
      <Section title="Service Routing">
        {renderKeyValueGrid([
          ["name", r.name],
          ["namespace", r.namespace],
          ["type", r.type],
          ["cluster IP", r.cluster_ip],
          ["selector", formatLabels(r.selector)],
          ["ports", formatPorts(r.ports)],
          ["session affinity", r.session_affinity],
          ["external policy", r.external_traffic_policy],
          ["internal policy", r.internal_traffic_policy],
          ["IP families", Array.isArray(r.ip_families) ? r.ip_families.join(", ") : ""],
        ])}
      </Section>
      {r.load_balancer && typeof r.load_balancer === "object" && (
        <Section title="Load Balancer">
          <CodeBlock code={JSON.stringify(r.load_balancer, null, 2)} />
        </Section>
      )}
    </>
  );
}

function renderEndpoints(r: Record<string, unknown>) {
  const slices = (r.endpoint_slices ?? {}) as Record<string, unknown>;
  const endpoints = asRecords(slices.endpoints);
  return (
    <>
      <Section title="Endpoint Health">
        {renderKeyValueGrid([
          ["legacy ready", r.ready_count],
          ["legacy not ready", r.not_ready_count],
          ["slice endpoints", slices.endpoint_count ?? r.endpoint_slice_endpoint_count],
          ["slice ready", slices.ready_count],
          ["serving", slices.serving_count],
          ["terminating", slices.terminating_count],
          ["diagnostic", r.diagnostic_hint],
        ])}
      </Section>
      {endpoints.length > 0 && (
        <Section title="EndpointSlice Endpoints">
          <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
            {endpoints.slice(0, 20).map((e, i) => {
              const conditions = (e.conditions ?? {}) as Record<string, unknown>;
              const target = (e.target_ref ?? {}) as Record<string, unknown>;
              return (
                <div key={i} style={{ fontSize: "0.75rem", padding: "0.375rem", borderRadius: "0.25rem", background: "var(--paper-3)", color: "var(--ink-2)" }}>
                  <ResourceName>{Array.isArray(e.addresses) ? e.addresses.join(", ") : ""}</ResourceName>
                  {" -> "}{String(target.kind ?? "")}/{String(target.name ?? "")}
                  {" on "}{String(e.node_name ?? "unknown-node")}
                  {" ready="}{String(conditions.ready ?? "unknown")}
                  {" serving="}{String(conditions.serving ?? "unknown")}
                  {" terminating="}{String(conditions.terminating ?? "unknown")}
                </div>
              );
            })}
          </div>
        </Section>
      )}
    </>
  );
}

function renderGeneric(r: Record<string, unknown>) {
  const textFields = ["message", "output", "description", "result", "summary", "runbook", "report"];
  for (const f of textFields) {
    if (r[f] && typeof r[f] === "string") {
      return (
        <>
          <p className="text-sm whitespace-pre-wrap" style={{ color: "var(--ink)" }}>{String(r[f])}</p>
          {Object.keys(r).filter(k => k !== f && typeof r[k] !== "object").length > 0 && (
            <div className="mt-2 grid grid-cols-2 gap-x-4 text-xs">
              {Object.entries(r)
                .filter(([k, v]) => k !== f && typeof v !== "object")
                .map(([k, v]) => (
                  <div key={k} className="flex gap-1 py-0.5">
                    <span className="min-w-[80px]" style={{ color: "var(--ink-3)" }}>{k}:</span>
                    <span style={{ color: "var(--ink-2)" }}>{String(v)}</span>
                  </div>
                ))}
            </div>
          )}
        </>
      );
    }
  }
  if (r.message && Object.keys(r).length === 1) {
    return <p style={{ fontSize: "0.875rem", color: "var(--ink)" }}>{String(r.message)}</p>;
  }
  return <CodeBlock code={JSON.stringify(r, null, 2)} />;
}

function renderRagGrounded(r: Record<string, unknown>) {
  const decision = isRecord(r.rag_decision) ? r.rag_decision : r;
  const citations = asRecords(decision.citations);
  const chunks = asRecords(decision.grounded_chunks);
  const topScore = typeof decision.top_score === "number" ? decision.top_score.toFixed(3) : "";
  const collection = String(decision.top_collection ?? "");

  const seen = new Set<string>();
  const uniqueCitations = citations.filter((citation) => {
    const key = [
      String(citation.title ?? ""),
      String(citation.section ?? ""),
      String(citation.url ?? ""),
    ].join("|");
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  return (
    <>
      <Section title="Knowledge Sources">
        <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
          {uniqueCitations.slice(0, 5).map((citation, i) => {
            const title = String(citation.title ?? "source");
            const section = String(citation.section ?? "");
            const url = String(citation.url ?? "");
            const similarity = typeof citation.similarity === "number" ? citation.similarity.toFixed(3) : "";
            return (
              <div key={`${title}-${section}-${i}`} style={{ fontSize: "0.75rem", padding: "0.5rem", borderRadius: "0.375rem", background: "var(--paper-3)", color: "var(--ink-2)", border: "1px solid var(--rule)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
                  <ResourceName>{title}</ResourceName>
                  {similarity && <Badge text={`similarity ${similarity}`} color="muted" />}
                </div>
                {section && <div style={{ marginTop: "0.25rem", color: "var(--ink-3)" }}>{section}</div>}
                {url && (
                  <a href={url} target="_blank" rel="noreferrer" style={{ display: "block", marginTop: "0.25rem", color: "var(--brand)", wordBreak: "break-all" }}>
                    {url}
                  </a>
                )}
              </div>
            );
          })}
          {uniqueCitations.length === 0 && (
            <p style={{ fontSize: "0.875rem", color: "var(--ink-2)", margin: 0 }}>No source citations were returned.</p>
          )}
        </div>
      </Section>
      {chunks.length > 0 && (
        <Section title="Retrieved Snippets">
          <div style={{ display: "flex", flexDirection: "column", gap: "0.375rem" }}>
            {chunks.slice(0, 3).map((chunk, i) => {
              const title = String(chunk.title ?? `chunk ${i + 1}`);
              const content = String(chunk.content ?? chunk.solution_text ?? "").replace(/\s+/g, " ").trim();
              const score = typeof chunk.score === "number" ? chunk.score.toFixed(3) : "";
              return (
                <div key={`${title}-${i}`} style={{ fontSize: "0.75rem", color: "var(--ink-2)" }}>
                  <ResourceName>{title}</ResourceName>
                  {score && <span style={{ color: "var(--ink-3)" }}> similarity={score}</span>}
                  {content && <div style={{ marginTop: "0.125rem", color: "var(--ink-2)" }}>{content.length > 220 ? `${content.slice(0, 217)}...` : content}</div>}
                </div>
              );
            })}
          </div>
        </Section>
      )}
      {(collection || topScore) && (
        <Section title="Retrieval Summary">
          {renderKeyValueGrid([
            ["collection", collection],
            ["top score", topScore],
            ["mode", decision.mode],
          ])}
        </Section>
      )}
    </>
  );
}

/* ── main export ─────────────────────────────────────────────── */

const ANALYZE_TOOLS = ["analyze_error", "get_fix_commands", "generate_runbook", "cluster_report", "error_summary"];

export default function ResultCard({ tool, result: rawResult, footerSlot }: Props) {
  const [showRaw, setShowRaw] = useState(false);

  // ReAct-surface results arrive as a ToolEnvelope — a *summary* built for the
  // model (get_events keeps eight deduplicated messages, not the events).
  // Every renderer below looks for `events`, `pods`, `logs`, and an envelope
  // has none of them, so an enveloped result rendered as an empty card. The
  // backend now carries the original result through as `payload`; prefer it,
  // and fall back to the result itself for the un-enveloped surfaces.
  const result = ((): typeof rawResult => {
    if (rawResult && typeof rawResult === "object" && !Array.isArray(rawResult)) {
      const payload = (rawResult as Record<string, unknown>).payload;
      if (payload && typeof payload === "object" && !Array.isArray(payload)) {
        return payload as typeof rawResult;
      }
    }
    return rawResult;
  })();

  let body: React.ReactNode;
  if (ANALYZE_TOOLS.includes(tool)) body = renderAnalyzeError(result);
  else if (tool === "investigate_pod") body = renderInvestigate(result);
  else if (tool === "investigate_workload") body = renderInvestigateWorkload(result);
  else if (tool === "analyze_namespace") body = renderAnalyzeNamespace(result);
  else if (tool === "get_pods") body = renderPodList(result);
  else if (tool === "get_pod_logs") body = renderLogs(result);
  else if (tool === "get_events") body = renderEvents(result);
  else if (tool === "list_contexts" || tool === "list_kubeconfig_contexts") body = renderContextList(result);
  else if (tool === "get_namespaces") body = renderNamespaces(result);
  else if (tool === "get_nodes") body = renderNodes(result);
  else if (tool === "investigate_node") body = renderInvestigateNode(result);
  else if (tool === "get_deployment") body = renderDeployment(result);
  else if (tool === "get_service") body = renderService(result);
  else if (tool === "get_endpoints") body = renderEndpoints(result);
  else if (tool === "list_namespace_resources") body = renderNamespaceResources(result);
  else if (tool === "list_services") body = renderListServices(result);
  else if (tool === "list_helm_releases") body = renderHelmReleases(result);
  else if (tool === "get_helm_release") body = renderHelmRelease(result);
  else if (tool === "diff_helm_revisions") body = renderHelmDiff(result);
  else if (tool === "investigate_helm_release") body = renderInvestigateHelmRelease(result);
  else if (tool === "find_workload") body = renderFindWorkload(result);
  else if (tool === "rag_grounded") body = renderRagGrounded(result);
  else if (tool === "get_resource_graph") body = <ResourceGraph data={result as ResourceGraphData} />;
  else body = renderGeneric(result);

  return (
    <div
      style={{ borderRadius: "0.75rem", padding: "1rem", fontSize: "0.875rem", background: "var(--paper-2)", border: "1px solid var(--rule)" }}
    >
      {body}
      <div style={{ marginTop: "0.75rem", display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.75rem", borderTop: "1px solid var(--rule)", paddingTop: "0.5rem", flexWrap: "wrap" }}>
        <span style={{ fontSize: "0.75rem", fontFamily: "var(--mono)", color: "var(--ink-3)" }}>tool: {tool}</span>
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap", justifyContent: "flex-end" }}>
          {footerSlot}
          <button
            onClick={() => setShowRaw(!showRaw)}
            style={{ fontSize: "0.75rem", textDecoration: "underline", transition: "color 0.15s", color: "var(--ink-3)", background: "none", border: "none", cursor: "pointer", padding: 0 }}
            onMouseEnter={(e) => (e.currentTarget.style.color = "var(--brand)")}
            onMouseLeave={(e) => (e.currentTarget.style.color = "var(--ink-3)")}
          >
            {showRaw ? "Hide raw" : "Show raw JSON"}
          </button>
        </div>
      </div>
      {showRaw && <CodeBlock code={JSON.stringify(result, null, 2)} />}
    </div>
  );
}
