"use client";

import React from "react";

import type { TopologyEdge, TopologyNode } from "../lib/api";
import { useClusterTopology } from "../lib/useClusterTopology";

const HEALTH_COLOUR: Record<TopologyNode["health"], string> = {
  red: "var(--red)",
  amber: "var(--amber)",
  green: "var(--green)",
  idle: "var(--fg-4, var(--fg-3))",
};

const ROW_HEIGHT = 34;
const DOT_X = 14;
const LABEL_X = 28;

interface Props {
  sessionId: string | null | undefined;
  /** False while the accordion is collapsed — no point polling a hidden view. */
  enabled?: boolean;
  scope?: "all" | "alerting";
}

/**
 * Workloads that need attention, as a compact SVG column.
 *
 * Deliberately not a React Flow canvas. This lives in a ~240px rail, where a
 * pannable graph is unusable and the default scope returns only the unhealthy
 * workloads — usually a handful, with no edges unless Prometheus traffic
 * metrics are switched on. A column reads faster at that size and costs no
 * layout library. Edges are drawn when they exist.
 */
export default function MissionControlTopologyMap({
  sessionId,
  enabled = true,
  scope = "alerting",
}: Props) {
  const { topology, loading } = useClusterTopology(sessionId, scope, enabled);

  if (!sessionId) return null;

  if (loading && !topology) {
    return <Note>reading workloads…</Note>;
  }

  if (!topology) return null;

  if (topology.nodes.length === 0) {
    // The default scope hides healthy workloads, so an empty graph is good
    // news. Saying so beats an empty box that reads as a failed fetch.
    return (
      <Note>
        {scope === "alerting" ? "all workloads healthy" : "no workloads found"}
      </Note>
    );
  }

  const ordered = orderNodes(topology.nodes, topology.edges);
  const positions = new Map(ordered.map((n, i) => [n.id, i]));
  const height = ordered.length * ROW_HEIGHT + 8;

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 240 ${height}`}
      preserveAspectRatio="xMinYMin meet"
      role="img"
      aria-label={`${ordered.length} workloads needing attention`}
      style={{ display: "block" }}
    >
      {topology.edges.map((edge) => {
        const from = positions.get(edge.source);
        const to = positions.get(edge.target);
        if (from === undefined || to === undefined) return null;
        return (
          <path
            key={`${edge.source}->${edge.target}`}
            d={curve(rowY(from), rowY(to))}
            fill="none"
            stroke="var(--line-2, var(--rule-2))"
            strokeWidth={1}
          />
        );
      })}

      {ordered.map((node, index) => (
        <g key={node.id} transform={`translate(0, ${rowY(index)})`}>
          <title>
            {`${node.kind} ${node.namespace}/${node.name} — ` +
              `${node.replicas.ready}/${node.replicas.desired} ready`}
          </title>
          <circle cx={DOT_X} cy={0} r={4} fill={HEALTH_COLOUR[node.health]} />
          <text
            x={LABEL_X}
            y={3}
            style={{
              font: "500 11px/1 var(--mono)",
              fill: "var(--fg-1, var(--ink))",
            }}
          >
            {truncate(node.name, 22)}
          </text>
          <text
            x={LABEL_X}
            y={15}
            style={{
              font: "400 9px/1 var(--mono)",
              fill: "var(--fg-3)",
              letterSpacing: "0.04em",
            }}
          >
            {`${node.kind.toLowerCase()} · ${node.replicas.ready}/${node.replicas.desired}`}
          </text>
        </g>
      ))}
    </svg>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        padding: "8px 14px 12px",
        font: "400 11px/1.4 var(--mono)",
        color: "var(--fg-3)",
      }}
    >
      {children}
    </div>
  );
}

function rowY(index: number): number {
  return index * ROW_HEIGHT + 18;
}

function curve(fromY: number, toY: number): string {
  // Left-hand bracket rather than a straight line: a vertical segment between
  // adjacent rows would sit under the dots and read as a border.
  const bend = 6;
  return `M ${DOT_X} ${fromY} C ${DOT_X - bend} ${fromY}, ${DOT_X - bend} ${toY}, ${DOT_X} ${toY}`;
}

/**
 * Worst-first, then dependency order within a health band.
 *
 * The rail is short and scrolls; whatever is red should be visible without
 * scrolling, because that is the entire reason to look at this panel.
 */
export function orderNodes(
  nodes: TopologyNode[],
  edges: TopologyEdge[],
): TopologyNode[] {
  const severity: Record<TopologyNode["health"], number> = {
    red: 0,
    amber: 1,
    green: 2,
    idle: 3,
  };

  const upstreamCount = new Map<string, number>();
  for (const edge of edges) {
    upstreamCount.set(edge.target, (upstreamCount.get(edge.target) ?? 0) + 1);
  }

  return [...nodes].sort((a, b) => {
    const bySeverity = severity[a.health] - severity[b.health];
    if (bySeverity !== 0) return bySeverity;
    // A workload nothing depends on is a likelier root cause than one that
    // is merely downstream of it.
    const byDepth = (upstreamCount.get(a.id) ?? 0) - (upstreamCount.get(b.id) ?? 0);
    if (byDepth !== 0) return byDepth;
    return a.name.localeCompare(b.name);
  });
}

function truncate(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max - 1)}…`;
}
