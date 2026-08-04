"use client";

/**
 * STATIC hero-screenshot page for W1 marketing (kubeastra.io Reddit / HN
 * posts). No real backend calls — everything is fixture data. Sets
 * data-theme="mission-control" on mount so the whole page renders with
 * the cosmic dark palette.
 *
 * Route: /mission-control-preview
 * Use case: 1200×630 OG images + Twitter 16:9 crops.
 */

import React, { useEffect, useMemo, useState } from "react";
import { MissionControlHeader } from "../../components/MissionControlHeader";
import { MissionControlLeftRail } from "../../components/MissionControlLeftRail";
import { ReasoningStream } from "../../components/ReasoningStream";
import { ToolCard, type ToolCardStep } from "../../components/ToolCard";
import {
  MissionControlDiagnosis,
  InlineToken,
  type DiagnosisMetric,
  type DiagnosisDiffLine,
} from "../../components/MissionControlDiagnosis";
import { CommandBar } from "../../components/CommandBar";
import { MissionControlApprovalOverlay } from "../../components/MissionControlApprovalOverlay";
import type { ClusterStatus } from "../../lib/api";

const CLUSTER: ClusterStatus = {
  connected: true,
  mode: "autodetect",
  cluster_name: "gke-prod-east-1",
  context_name: "prod/default",
  namespace: "prod",
};

const SESSIONS = [
  { id: "s-1", title: "api-gateway CrashLoop investigation", timestamp: Date.now() - 1000 * 60 * 6 },
  { id: "s-2", title: "orders-svc p99 spike", timestamp: Date.now() - 1000 * 60 * 60 * 3 },
  { id: "s-3", title: "checkout PVC pending", timestamp: Date.now() - 1000 * 60 * 60 * 26 },
  { id: "s-4", title: "prom rules audit", timestamp: Date.now() - 1000 * 60 * 60 * 72 },
];

const REASONING_TOKENS = [
  "Pod is restarting frequently — checking restart count and termination state.",
  "Memory usage exceeds limit; correlating prometheus metrics with kubectl state.",
  "OOMKilled pattern is consistent across recent events. Looking for config drift.",
  "configmap/api-config last modified 92 days ago — predates current traffic levels.",
  "Concluding: insufficient memory limit, not application leak. Confidence high.",
];

const STEPS: ToolCardStep[] = [
  {
    tool: "kubectl",
    cmd: "kubectl describe pod api-gateway-7d9f8b-xkp2q -n prod",
    output: [
      "Status:        Running",
      "Restart Count: 12",
      "Last State:    Terminated",
      "  Reason:      OOMKilled",
      "  Exit Code:   137",
    ],
    summary: "CrashLoopBackOff · 12 restarts · OOMKilled",
    duration: "0.41s",
    status: "done",
  },
  {
    tool: "prometheus",
    cmd: 'promql: container_memory_rss{pod="api-gateway-7d9f8b-xkp2q"}',
    output: [
      "p50 = 118.2 MiB",
      "p95 = 138.9 MiB",
      "p99 = 142.3 MiB    <-- exceeds limit (128 MiB)",
      "OOMKill events: 12 in last 2h",
    ],
    summary: "RSS p99 142Mi · limit 128Mi · breach",
    duration: "0.62s",
    status: "done",
  },
  {
    tool: "logs",
    cmd: "kubectl logs api-gateway-7d9f8b-xkp2q --previous --tail=50",
    output: [
      "fatal: runtime: out of memory",
      "runtime: GC assist wait: cannot allocate",
      "signal: killed (exit 137)",
      "... 11 prior occurrences",
    ],
    summary: "OOMKilled · signal 9 · exit 137",
    duration: "0.28s",
    status: "done",
  },
  {
    tool: "events",
    cmd: "kubectl get events --field-selector reason=OOMKilling",
    output: [
      "09:08:14  Warning  OOMKilling  pod api-gateway-7d9f8b-xkp2q",
      "09:12:51  Warning  OOMKilling  pod api-gateway-7d9f8b-xkp2q",
      "09:16:33  Warning  OOMKilling  pod api-gateway-7d9f8b-xkp2q",
    ],
    summary: "3× OOMKilling in last 8 min",
    duration: "0.19s",
    status: "done",
  },
  {
    tool: "topology",
    cmd: "astra:trace --service=api-gateway --depth=2",
    output: [
      "api-gateway → orders-svc       (HTTP, 2.1k rps)",
      "api-gateway → checkout-svc     (HTTP, 0.9k rps)",
      "orders-svc → postgres-orders   (TCP, healthy)",
      "blast radius: 2 dependent services",
    ],
    summary: "Blast radius: 2 services downstream",
    duration: "0.83s",
    status: "done",
  },
];

const METRICS: DiagnosisMetric[] = [
  { label: "Restarts", value: "12", tone: "critical" },
  { label: "Peak RSS", value: "142Mi", tone: "warn" },
  { label: "Limit", value: "128Mi" },
  { label: "Drift", value: "92d" },
];

const DIAGNOSIS_DIFF: DiagnosisDiffLine[] = [
  { kind: "remove", text: 'memory: "128Mi"' },
  { kind: "add", text: 'memory: "256Mi"', comment: "double the headroom" },
];

const APPROVAL_DIFF: DiagnosisDiffLine[] = [
  { kind: "context", text: "@@ -12,7 +12,7 @@ spec.template.spec.containers" },
  { kind: "context", text: "            resources:" },
  { kind: "context", text: "              limits:" },
  { kind: "remove", text: '                memory: "128Mi"' },
  { kind: "add", text: '                memory: "256Mi"' },
  { kind: "context", text: '                cpu: "500m"' },
  { kind: "context", text: "@@ -28,5 +28,5 @@ configmap.data" },
  { kind: "context", text: '   APP_PORT: "8080"' },
  { kind: "remove", text: '   memoryLimit: "128Mi"' },
  { kind: "add", text: '   memoryLimit: "256Mi"' },
  { kind: "context", text: '   cacheSize: "40000"' },
];

export default function MissionControlPreviewPage() {
  const [expandedTool, setExpandedTool] = useState<number | null>(0);
  const [showOverlay, setShowOverlay] = useState(false);
  const [executed, setExecuted] = useState(false);

  useEffect(() => {
    const prev = document.documentElement.getAttribute("data-theme");
    document.documentElement.setAttribute("data-theme", "mission-control");
    return () => {
      document.documentElement.setAttribute("data-theme", prev ?? "light");
    };
  }, []);

  const summary = useMemo(
    () => (
      <>
        Pod <InlineToken variant="red">api-gateway-7d9f8b-xkp2q</InlineToken> is being{" "}
        <InlineToken variant="red">OOMKilled</InlineToken> every ~4 minutes. The cap of{" "}
        <InlineToken variant="red">128Mi</InlineToken> in{" "}
        <InlineToken variant="cyan">configmap/api-config</InlineToken> was set 92 days ago —
        before a 3× traffic increase. Peak RSS now reaches{" "}
        <InlineToken variant="amber">142Mi</InlineToken>, triggering CrashLoopBackOff.
      </>
    ),
    [],
  );

  return (
    <div
      style={{
        width: "100vw",
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        background: "var(--bg-0, var(--paper))",
        color: "var(--ink, var(--fg-0))",
      }}
    >
      <MissionControlHeader clusterStatus={CLUSTER} busy={false} version="v3.2.1" />

      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        <MissionControlLeftRail
          sessions={SESSIONS}
          currentSessionId="s-1"
          onSelectSession={() => {}}
          onNewSession={() => {}}
          onDeleteSession={() => {}}
        />

        <main
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "24px 28px",
            display: "flex",
            flexDirection: "column",
            gap: 22,
          }}
        >
          <UserQueryBubble text="Why is api-gateway crashing in prod?" time="09:16:52" />

          <div>
            <ReasoningStream tokens={REASONING_TOKENS} active={false} />

            <div style={{ marginBottom: 14 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                <span
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: 9,
                    textTransform: "uppercase",
                    letterSpacing: "0.10em",
                    color: "var(--cyan, var(--brand))",
                  }}
                >
                  Tool dispatch
                </span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 9, color: "var(--ink-3, var(--fg-3))" }}>
                  {STEPS.length}/{STEPS.length}
                </span>
                <div
                  style={{
                    flex: 1,
                    height: 2,
                    background: "var(--line, var(--rule))",
                    borderRadius: 1,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      height: "100%",
                      width: "100%",
                      background: "var(--cyan, var(--brand))",
                      boxShadow: "0 0 8px var(--cyan, var(--brand))",
                    }}
                  />
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {STEPS.map((s, i) => (
                  <ToolCard
                    key={s.tool}
                    step={s}
                    idx={i}
                    expanded={expandedTool === i}
                    onToggle={() => setExpandedTool(expandedTool === i ? null : i)}
                  />
                ))}
              </div>
            </div>

            {!executed && (
              <MissionControlDiagnosis
                severity="sev-1"
                title="Memory limit too low for current traffic"
                summary={summary}
                confidence={0.97}
                metrics={METRICS}
                diff={DIAGNOSIS_DIFF}
                diffMeta="2 manifests · 4 lines"
                onAuthorize={() => setShowOverlay(true)}
              />
            )}

            {executed && (
              <div
                style={{
                  padding: "10px 14px",
                  background: "var(--green-bg)",
                  border: "1px solid var(--green-bd)",
                  borderLeft: "2px solid var(--green)",
                  borderRadius: 5,
                  fontFamily: "var(--sans)",
                  fontSize: 12.5,
                  color: "var(--ink-2, var(--fg-1))",
                  lineHeight: 1.55,
                }}
              >
                Fix dispatched. Watching api-gateway rollout — pod restart count steady at 12,
                memory now under 256Mi ceiling. I&apos;ll page you if restarts resume in the next 10
                minutes.
              </div>
            )}
          </div>
        </main>
      </div>

      <CommandBar
        onSend={() => {}}
        busy={false}
        clusterLabel="prod-east-1"
        clusterConnected
        version="v3.2.1"
        toolCount={32}
        apiLatencyMs={12}
      />

      {showOverlay && (
        <MissionControlApprovalOverlay
          title="Raise api-gateway memory ceiling"
          coordinates={[
            { label: "cluster", value: "gke-prod-east-1" },
            { label: "scope", value: "1 deploy · 1 cm" },
            { label: "rollout", value: "~45s" },
          ]}
          impacts={[
            { label: "Memory limit", before: "128Mi", after: "256Mi" },
            { label: "Affected workload", after: "1 pod · rolling restart" },
          ]}
          preflightChecks={[
            "dry-run validated · schema clean",
            "node allocatable: 3.8Gi free (256Mi fits)",
            "PodDisruptionBudget allows 1 disruption",
            "rollback manifest saved to audit log",
          ]}
          diffFileHeader="deployment/api-gateway.yaml + configmap/api-config.yaml"
          diffLines={APPROVAL_DIFF}
          executionCommand="kubectl rollout status deployment/api-gateway"
          onClose={() => setShowOverlay(false)}
          onConfirm={() => {
            setExecuted(true);
            setShowOverlay(false);
          }}
        />
      )}
    </div>
  );
}

function UserQueryBubble({ text, time }: { text: string; time: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "flex-end" }}>
      <div
        style={{
          display: "inline-flex",
          flexDirection: "column",
          alignItems: "flex-end",
          maxWidth: "72%",
        }}
      >
        <div
          style={{
            padding: "10px 14px",
            background: "var(--cyan-bg, var(--brand-bg))",
            border: "1px solid var(--cyan-bd, var(--brand-bd))",
            borderRadius: 6,
            fontFamily: "var(--mono)",
            fontSize: 12,
            color: "var(--ink, var(--fg-0))",
            lineHeight: 1.5,
          }}
        >
          <span aria-hidden="true" style={{ color: "var(--cyan, var(--brand))", marginRight: 6 }}>
            you›
          </span>
          {text}
        </div>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 9,
            color: "var(--ink-3, var(--fg-3))",
            marginTop: 4,
          }}
        >
          {time} UTC
        </span>
      </div>
    </div>
  );
}
