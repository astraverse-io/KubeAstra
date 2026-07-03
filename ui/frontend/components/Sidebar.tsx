"use client";

import { useState } from "react";
import {
  Brain,
  Search,
  Server,
  Layers,
  AlertTriangle,
  Activity,
  ChevronDown,
  ChevronRight,
} from "lucide-react";

export type TabId =
  | "analyze"
  | "investigate"
  | "cluster"
  | "multicluster"
  | "recovery";

const TABS: { id: TabId; label: string; icon: React.ReactNode; description: string }[] = [
  {
    id: "analyze",
    label: "AI Analysis",
    icon: <Brain size={18} />,
    description: "Paste errors, get AI diagnosis and fixes",
  },
  {
    id: "investigate",
    label: "Investigate",
    icon: <Search size={18} />,
    description: "Live pod triage & logs",
  },
  {
    id: "cluster",
    label: "Cluster Info",
    icon: <Server size={18} />,
    description: "Deployments, services, events",
  },
  {
    id: "multicluster",
    label: "Multi-cluster",
    icon: <Layers size={18} />,
    description: "Manage kubeconfig contexts",
  },
  {
    id: "recovery",
    label: "Recovery",
    icon: <AlertTriangle size={18} />,
    description: "Scale, restart, patch (write ops)",
  },
];

interface HealthCheck {
  status: "ok" | "degraded" | "failed";
  duration_ms?: number;
  detail?: string | null;
}

interface HealthInfo {
  kubectl_available: boolean;
  ai_enabled: boolean;
  kubectl_context: string | null;
  kubectl_mode?: "in_cluster" | "kubeconfig" | "unavailable";
  status?: "ok" | "degraded";
  checks?: Record<string, HealthCheck>;
}

interface Props {
  active: TabId;
  onChange: (id: TabId) => void;
  health: HealthInfo | null;
}

export default function Sidebar({ active, onChange, health }: Props) {
  const [diagOpen, setDiagOpen] = useState(false);
  return (
    <aside style={{
      width: "16rem",
      flexShrink: 0,
      backgroundColor: "var(--paper-2)",
      borderRight: "1px solid var(--rule)",
      display: "flex",
      flexDirection: "column"
    }}>
      {/* Header */}
      <div style={{ padding: "1.25rem", borderBottom: "1px solid var(--rule)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.25rem" }}>
          <Activity size={20} color="var(--brand)" />
          <span style={{ fontWeight: "bold", color: "var(--ink)", fontSize: "0.875rem", letterSpacing: "0.025em" }}>KubeAstra</span>
        </div>
        <p style={{ color: "var(--ink-3)", fontSize: "0.75rem" }}>Team Self-Service Portal</p>
      </div>

      {/* Status badges */}
      {health && (
        <div style={{ padding: "0.75rem 1.25rem", borderBottom: "1px solid var(--rule)", display: "flex", flexDirection: "column", gap: "0.375rem" }}>
          <StatusBadge ok={health.ai_enabled} label="AI Provider" />
          <StatusBadge
            ok={health.kubectl_available}
            label={health.kubectl_context ?? "kubectl"}
            tag={
              health.kubectl_available
                ? (health.kubectl_mode === "in_cluster" ? "in-cluster" : health.kubectl_mode === "kubeconfig" ? "kubeconfig" : null)
                : null
            }
          />
          {health.checks && Object.keys(health.checks).length > 0 && (
            <>
              <button
                onClick={() => setDiagOpen((v) => !v)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.25rem",
                  background: "none",
                  border: "none",
                  padding: 0,
                  cursor: "pointer",
                  fontSize: "0.6875rem",
                  color: "var(--ink-3)",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                }}
                aria-expanded={diagOpen}
                aria-controls="sidebar-diagnostics"
              >
                {diagOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Diagnostics
                {health.status === "degraded" && (
                  <span style={{ marginLeft: "0.25rem", color: "var(--warning)" }} aria-label="degraded">•</span>
                )}
              </button>
              {diagOpen && (
                <ul
                  id="sidebar-diagnostics"
                  style={{ listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: "0.25rem" }}
                >
                  {Object.entries(health.checks).map(([name, c]) => (
                    <li
                      key={name}
                      title={c.detail ?? undefined}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "0.5rem",
                        fontSize: "0.6875rem",
                        color: "var(--ink-3)",
                      }}
                    >
                      <span style={{ display: "flex", alignItems: "center", gap: "0.375rem", minWidth: 0 }}>
                        <span
                          style={{
                            width: "0.375rem",
                            height: "0.375rem",
                            borderRadius: "50%",
                            flexShrink: 0,
                            backgroundColor:
                              c.status === "ok" ? "var(--green)" :
                              c.status === "degraded" ? "var(--warning)" :
                              "var(--red)",
                          }}
                        />
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
                      </span>
                      {typeof c.duration_ms === "number" && (
                        <span style={{ fontFamily: "var(--font-mono, monospace)", color: "var(--ink-4, var(--ink-3))" }}>
                          {Math.round(c.duration_ms)}ms
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      )}

      {/* Nav */}
      <nav style={{ flex: 1, padding: "0.75rem 0.5rem", display: "flex", flexDirection: "column", gap: "0.125rem" }}>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => onChange(t.id)}
            style={{
              width: "100%",
              textAlign: "left",
              padding: "0.75rem",
              borderRadius: "0.5rem",
              transition: "background-color 0.15s, color 0.15s",
              display: "flex",
              alignItems: "flex-start",
              gap: "0.75rem",
              cursor: "pointer",
              border: "none",
              backgroundColor: active === t.id ? "var(--brand)" : "transparent",
              color: active === t.id ? "var(--paper)" : "var(--ink-2)",
            }}
            onMouseEnter={(e) => {
              if (active !== t.id) {
                e.currentTarget.style.backgroundColor = "var(--paper-3)";
                e.currentTarget.style.color = "var(--ink)";
              }
            }}
            onMouseLeave={(e) => {
              if (active !== t.id) {
                e.currentTarget.style.backgroundColor = "transparent";
                e.currentTarget.style.color = "var(--ink-2)";
              }
            }}
          >
            <span style={{ marginTop: "0.125rem", flexShrink: 0 }}>{t.icon}</span>
            <div>
              <div style={{ fontSize: "0.875rem", fontWeight: 500, lineHeight: 1.25 }}>{t.label}</div>
              <div
                style={{
                  fontSize: "0.75rem",
                  lineHeight: 1.25,
                  marginTop: "0.125rem",
                  color: active === t.id ? "var(--paper-2)" : "var(--ink-3)",
                }}
              >
                {t.description}
              </div>
            </div>
          </button>
        ))}
      </nav>

      <div style={{ padding: "1rem 1.25rem", borderTop: "1px solid var(--rule)" }}>
        <p style={{ color: "var(--ink-3)", fontSize: "0.75rem" }}>mcp v1.0</p>
      </div>
    </aside>
  );
}

function StatusBadge({ ok, label, tag }: { ok: boolean; label: string; tag?: string | null }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
      <span
        style={{
          width: "0.5rem",
          height: "0.5rem",
          borderRadius: "50%",
          flexShrink: 0,
          backgroundColor: ok ? "var(--green)" : "var(--red)",
        }}
      />
      <span style={{ fontSize: "0.75rem", color: "var(--ink-3)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flex: 1 }}>
        {label}
      </span>
      {tag && (
        <span
          style={{
            fontSize: "0.625rem",
            fontFamily: "var(--font-mono, monospace)",
            color: "var(--ink-3)",
            border: "1px solid var(--rule)",
            borderRadius: "0.25rem",
            padding: "0 0.25rem",
            lineHeight: 1.4,
            flexShrink: 0,
          }}
        >
          {tag}
        </span>
      )}
    </div>
  );
}
