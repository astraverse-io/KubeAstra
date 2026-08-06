import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import MissionControlTopologyMap, { orderNodes } from "../MissionControlTopologyMap";
import type { TopologyEdge, TopologyNode } from "../../lib/api";

const fetchClusterTopology = vi.fn();

vi.mock("../../lib/api", () => ({
  fetchClusterTopology: (id: string, scope: string) =>
    fetchClusterTopology(id, scope),
}));

function node(
  name: string,
  health: TopologyNode["health"],
  ready = 0,
  desired = 1,
): TopologyNode {
  return {
    id: `payments/Deployment/${name}`,
    kind: "Deployment",
    namespace: "payments",
    name,
    health,
    replicas: { ready, desired },
  };
}

beforeEach(() => {
  fetchClusterTopology.mockReset();
});

describe("MissionControlTopologyMap", () => {
  it("draws a row per unhealthy workload", async () => {
    fetchClusterTopology.mockResolvedValue({
      nodes: [node("api", "red"), node("worker", "amber", 1, 3)],
      edges: [],
      generated_at: "2026-08-06T00:00:00Z",
    });

    render(<MissionControlTopologyMap sessionId="sess-1" />);

    expect(await screen.findByText("api")).toBeInTheDocument();
    expect(screen.getByText("worker")).toBeInTheDocument();
    // Replica counts are the reason to look: "amber" alone does not say
    // whether one pod is down or nine.
    expect(screen.getByText("deployment · 1/3")).toBeInTheDocument();
  });

  it("says all is well rather than showing an empty box", async () => {
    // The default scope hides healthy workloads, so no nodes is good news.
    // An empty frame reads as a failed fetch.
    fetchClusterTopology.mockResolvedValue({
      nodes: [],
      edges: [],
      generated_at: "2026-08-06T00:00:00Z",
    });

    render(<MissionControlTopologyMap sessionId="sess-1" />);

    expect(await screen.findByText("all workloads healthy")).toBeInTheDocument();
  });

  it("does not poll while the panel is collapsed", () => {
    render(<MissionControlTopologyMap sessionId="sess-1" enabled={false} />);

    expect(fetchClusterTopology).not.toHaveBeenCalled();
  });

  it("does not poll without a session", () => {
    render(<MissionControlTopologyMap sessionId={null} />);

    expect(fetchClusterTopology).not.toHaveBeenCalled();
  });

  it("requests the alerting scope by default", async () => {
    fetchClusterTopology.mockResolvedValue({ nodes: [], edges: [], generated_at: "" });

    render(<MissionControlTopologyMap sessionId="sess-1" />);

    await waitFor(() =>
      expect(fetchClusterTopology).toHaveBeenCalledWith("sess-1", "alerting"),
    );
  });

  it("labels the graph for screen readers", async () => {
    fetchClusterTopology.mockResolvedValue({
      nodes: [node("api", "red"), node("worker", "amber")],
      edges: [],
      generated_at: "",
    });

    render(<MissionControlTopologyMap sessionId="sess-1" />);

    expect(
      await screen.findByLabelText("2 workloads needing attention"),
    ).toBeInTheDocument();
  });

  it("truncates a long workload name rather than overflowing the rail", async () => {
    fetchClusterTopology.mockResolvedValue({
      nodes: [node("a-very-long-workload-name-indeed", "red")],
      edges: [],
      generated_at: "",
    });

    render(<MissionControlTopologyMap sessionId="sess-1" />);

    const label = await screen.findByText(/^a-very-long/);
    expect(label.textContent!.length).toBeLessThanOrEqual(22);
    expect(label.textContent).toContain("…");
  });
});

describe("orderNodes", () => {
  it("puts the worst first", () => {
    // The rail scrolls. Whatever is red must be visible without scrolling,
    // because that is the whole reason to open this panel.
    const ordered = orderNodes(
      [node("healthy", "green"), node("down", "red"), node("degraded", "amber")],
      [],
    );

    expect(ordered.map((n) => n.name)).toEqual(["down", "degraded", "healthy"]);
  });

  it("puts a likely root cause above what depends on it", () => {
    const upstream = node("database", "red");
    const downstream = node("api", "red");
    const edges: TopologyEdge[] = [
      { source: upstream.id, target: downstream.id, kind: "http", rate_rps: 4 },
    ];

    const ordered = orderNodes([downstream, upstream], edges);

    expect(ordered.map((n) => n.name)).toEqual(["database", "api"]);
  });

  it("falls back to name order so the list does not shuffle between polls", () => {
    const ordered = orderNodes([node("zebra", "red"), node("alpha", "red")], []);

    expect(ordered.map((n) => n.name)).toEqual(["alpha", "zebra"]);
  });

  it("does not mutate what it was given", () => {
    const nodes = [node("b", "green"), node("a", "red")];
    orderNodes(nodes, []);

    expect(nodes.map((n) => n.name)).toEqual(["b", "a"]);
  });
});
