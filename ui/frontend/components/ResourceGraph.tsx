"use client";

import { useMemo, useState } from "react";
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "dagre";

type ResourceType = "ingress" | "service" | "deployment" | "pod" | string;
type HealthStatus = "healthy" | "degraded" | "unknown" | string;

interface GraphNode {
  id: string;
  label: string;
  type: ResourceType;
  status?: HealthStatus;
  meta?: Record<string, unknown>;
}

interface GraphEdge {
  source: string;
  target: string;
  kind?: string;
}

interface ResourceGraphProps {
  data: {
    namespace?: string;
    nodes?: GraphNode[];
    edges?: GraphEdge[];
    summary?: Record<string, number>;
  };
}

const NODE_WIDTH = 240;
const NODE_HEIGHT = 96;
const GRAPH_PADDING_X = 220;
const GRAPH_PADDING_Y = 180;
const GRAPH_MIN_WIDTH = 520;
const GRAPH_MAX_WIDTH = 1400;
const GRAPH_MIN_HEIGHT = 280;
const GRAPH_MAX_HEIGHT = 760;

const TYPE_COLORS: Record<string, string> = {
  ingress: "#a855f7",
  service: "#3b82f6",
  deployment: "#f97316",
  pod: "var(--brand)",
};

const EDGE_LABELS: Record<string, string> = {
  "ingress->service": "routes",
  "service->pod": "selects",
  "deployment->pod": "manages",
};

function layout(nodes: Node[], edges: Edge[], direction = "TB") {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: direction, nodesep: 80, ranksep: 90 });

  nodes.forEach((node) => graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  edges.forEach((edge) => graph.setEdge(edge.source, edge.target));
  dagre.layout(graph);

  return {
    nodes: nodes.map((node) => {
      const point = graph.node(node.id);
      return {
        ...node,
        position: {
          x: point.x - NODE_WIDTH / 2,
          y: point.y - NODE_HEIGHT / 2,
        },
      };
    }),
    edges,
  };
}

function statusColor(status?: HealthStatus) {
  if (status === "healthy" || status === "Running") return "var(--success)";
  if (status === "degraded" || status === "Pending" || status === "Failed") return "var(--danger)";
  return "var(--border)";
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function graphCanvasSize(nodes: Node[]) {
  if (nodes.length === 0) {
    return { width: GRAPH_MIN_WIDTH, height: GRAPH_MIN_HEIGHT };
  }

  const bounds = nodes.reduce(
    (acc, node) => ({
      minX: Math.min(acc.minX, node.position.x),
      minY: Math.min(acc.minY, node.position.y),
      maxX: Math.max(acc.maxX, node.position.x + NODE_WIDTH),
      maxY: Math.max(acc.maxY, node.position.y + NODE_HEIGHT),
    }),
    { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity },
  );

  return {
    width: clamp(bounds.maxX - bounds.minX + GRAPH_PADDING_X, GRAPH_MIN_WIDTH, GRAPH_MAX_WIDTH),
    height: clamp(bounds.maxY - bounds.minY + GRAPH_PADDING_Y, GRAPH_MIN_HEIGHT, GRAPH_MAX_HEIGHT),
  };
}

function compactMeta(meta?: Record<string, unknown>) {
  if (!meta) return "";
  const parts = [];
  if (meta.phase) parts.push(`phase: ${String(meta.phase)}`);
  if (meta.restarts !== undefined) parts.push(`restarts: ${String(meta.restarts)}`);
  if (meta.ready_replicas !== undefined || meta.replicas !== undefined) {
    parts.push(`ready: ${String(meta.ready_replicas ?? 0)}/${String(meta.replicas ?? "?")}`);
  }
  if (meta.service_type) parts.push(`type: ${String(meta.service_type)}`);
  if (meta.hosts) parts.push(`hosts: ${Array.isArray(meta.hosts) ? meta.hosts.join(", ") : String(meta.hosts)}`);
  return parts.slice(0, 2).join(" | ");
}

export default function ResourceGraph({ data }: ResourceGraphProps) {
  const [selected, setSelected] = useState<GraphNode | null>(null);

  const { nodes, edges } = useMemo(() => {
    const graphNodes: Node[] = (data.nodes ?? []).map((node) => {
      const typeColor = TYPE_COLORS[node.type] ?? "var(--text-muted)";
      const borderColor = statusColor(node.status);
      const isDegraded = borderColor === "var(--danger)";
      return {
        id: node.id,
        data: {
          label: (
            <div style={{ display: "flex", height: "100%", width: "100%", flexDirection: "column", justifyContent: "center", gap: "0.25rem" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem" }}>
                <span style={{ fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.16em", color: typeColor }}>
                  {node.type}
                </span>
                <span style={{ height: "0.5rem", width: "0.5rem", borderRadius: "50%", background: borderColor }} />
              </div>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "var(--mono)", fontSize: "0.875rem" }} title={node.label}>
                {node.label}
              </span>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: "11px", color: "var(--ink-3)" }}>
                {compactMeta(node.meta) || node.status || "unknown"}
              </span>
            </div>
          ),
          raw: node,
        },
        position: { x: 0, y: 0 },
        style: {
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          padding: 12,
          borderRadius: 12,
          color: "var(--ink)",
          background: "var(--paper-2)",
          border: `2px solid ${borderColor}`,
          borderLeft: `5px solid ${typeColor}`,
          boxShadow: isDegraded ? "0 0 20px rgba(239, 68, 68, 0.25)" : "0 8px 24px rgba(0,0,0,0.16)",
        },
      };
    });

    const graphEdges: Edge[] = (data.edges ?? []).map((edge, index) => ({
      id: `edge-${index}-${edge.source}-${edge.target}`,
      source: edge.source,
      target: edge.target,
      label: edge.kind ? EDGE_LABELS[edge.kind] ?? edge.kind : undefined,
      animated: true,
      style: { stroke: "var(--ink-3)", strokeWidth: 1.5 },
      labelStyle: { fill: "var(--ink-2)", fontSize: 11 },
      markerEnd: { type: MarkerType.ArrowClosed, color: "var(--ink-3)" },
    }));

    return layout(graphNodes, graphEdges);
  }, [data.edges, data.nodes]);

  const canvasSize = useMemo(() => graphCanvasSize(nodes), [nodes]);

  return (
    <div style={{ marginTop: "0.75rem", width: "100%", overflowX: "auto" }}>
      <div
        style={{
          position: "relative",
          overflow: "hidden",
          borderRadius: "0.75rem",
          width: "100%",
          minWidth: `${canvasSize.width}px`,
          height: `${canvasSize.height}px`,
          border: "1px solid var(--rule)",
          background: "var(--paper)",
        }}
      >
        <div style={{ position: "absolute", left: "0.75rem", top: "0.75rem", zIndex: 10, borderRadius: "0.5rem", padding: "0.5rem 0.75rem", fontSize: "0.75rem", background: "var(--paper-2)", border: "1px solid var(--rule)" }}>
          <span style={{ color: "var(--ink-3)" }}>Namespace</span>{" "}
          <span style={{ fontFamily: "var(--mono)", color: "var(--ink)" }}>{data.namespace ?? "unknown"}</span>
          {data.summary && (
            <span style={{ marginLeft: "0.5rem", color: "var(--ink-3)" }}>
              {Object.entries(data.summary).map(([key, value]) => `${key}:${value}`).join(" ")}
            </span>
          )}
        </div>

        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodeClick={(_, node) => setSelected((node.data as { raw?: GraphNode }).raw ?? null)}
          fitView
          fitViewOptions={{ padding: 0.18, minZoom: 0.2, maxZoom: 1.15 }}
          colorMode="dark"
          attributionPosition="bottom-right"
        >
          <Background gap={18} size={1} color="var(--rule)" />
          <Controls />
          <MiniMap
            pannable
            zoomable
            nodeColor={(node) => statusColor((node.data as { raw?: GraphNode }).raw?.status)}
            style={{ background: "var(--paper-2)", border: "1px solid var(--rule)" }}
          />
        </ReactFlow>

        {selected && (
          <div
            style={{
              position: "absolute", right: "0.75rem", top: "0.75rem", zIndex: 20, maxHeight: "calc(100% - 24px)",
              width: "20rem", overflowY: "auto", borderRadius: "0.75rem", padding: "1rem", fontSize: "0.75rem",
              background: "var(--paper-2)", border: "1px solid var(--brand-bd)", color: "var(--ink-2)"
            }}
          >
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "0.75rem" }}>
              <div>
                <div style={{ textTransform: "uppercase", letterSpacing: "0.16em", color: TYPE_COLORS[selected.type] ?? "var(--brand)" }}>
                  {selected.type}
                </div>
                <div style={{ marginTop: "0.25rem", fontFamily: "var(--mono)", fontSize: "0.875rem", color: "var(--ink)" }}>{selected.label}</div>
              </div>
              <button onClick={() => setSelected(null)} style={{ color: "var(--ink-3)", background: "none", border: "none", fontSize: "1.25rem", cursor: "pointer", padding: 0 }}>
                &times;
              </button>
            </div>
            <pre style={{ marginTop: "0.75rem", whiteSpace: "pre-wrap", borderRadius: "0.5rem", padding: "0.75rem", background: "var(--paper-3)", color: "var(--ink-2)" }}>
              {JSON.stringify(selected.meta ?? {}, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
