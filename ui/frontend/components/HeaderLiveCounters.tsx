"use client";

import { useClusterSummary } from "../lib/useClusterSummary";

interface Props {
  sessionId: string | null | undefined;
}

/**
 * Pods, alerts and sev-1 in the header, refreshed every 30 seconds.
 *
 * Renders nothing at all when there is nothing to say. A row of dashes is
 * indistinguishable from a broken panel, and the header has other things in
 * it — an empty gap is honest and quiet.
 */
export default function HeaderLiveCounters({ sessionId }: Props) {
  const { summary, loading } = useClusterSummary(sessionId);

  if (loading && !summary) {
    return (
      <div style={rowStyle} aria-busy="true">
        <span style={{ ...labelStyle, color: "var(--fg-3)" }}>reading cluster…</span>
      </div>
    );
  }

  if (!summary || summary.reason === "no_cluster") return null;

  if (summary.reason === "insufficient_rbac") {
    return (
      <div style={rowStyle}>
        <span
          style={{ ...labelStyle, color: "var(--amber)" }}
          title={`This credential cannot list pods in ${summary.namespace ?? "the namespace"}.`}
        >
          no read access
        </span>
      </div>
    );
  }

  const counters = summary.counters;
  if (!counters) return null;

  const podsHealthy = counters.pods_ready === counters.pods_total;
  const stale = summary.cache_age_seconds >= 60;

  return (
    <div
      style={rowStyle}
      // One label for the group: three separate numbers read out
      // individually are noise to a screen reader.
      role="group"
      aria-label={
        `${counters.pods_ready} of ${counters.pods_total} pods ready, ` +
        `${counters.alerts_active} alerts, ${counters.alerts_sev1} critical`
      }
      title={stale ? `Last read ${summary.cache_age_seconds}s ago` : undefined}
      data-stale={stale ? "true" : undefined}
    >
      <Counter
        label="pods"
        value={`${counters.pods_ready}/${counters.pods_total}`}
        tone={podsHealthy ? "normal" : "warn"}
      />
      <Counter
        label="alerts"
        value={counters.alerts_active}
        tone={counters.alerts_active > 0 ? "warn" : "normal"}
      />
      <Counter
        label="sev-1"
        value={counters.alerts_sev1}
        tone={counters.alerts_sev1 > 0 ? "bad" : "normal"}
      />
    </div>
  );
}

function Counter({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone: "normal" | "warn" | "bad";
}) {
  const color =
    tone === "bad" ? "var(--red)" : tone === "warn" ? "var(--amber)" : "var(--fg-2)";

  return (
    <span style={{ display: "inline-flex", alignItems: "baseline", gap: 4 }}>
      <span
        style={{
          font: "600 12px/1 var(--mono)",
          color,
          // Tabular figures: without them the header jitters sideways every
          // time a count crosses a digit boundary.
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </span>
      <span style={labelStyle}>{label}</span>
    </span>
  );
}

const rowStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 12,
};

const labelStyle: React.CSSProperties = {
  font: "500 10px/1 var(--mono)",
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  color: "var(--fg-3)",
};
