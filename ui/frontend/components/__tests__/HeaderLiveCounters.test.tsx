import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import HeaderLiveCounters from "../HeaderLiveCounters";
import type { ClusterSummary } from "../../lib/api";

const fetchClusterSummary = vi.fn();

vi.mock("../../lib/api", () => ({
  fetchClusterSummary: (id: string) => fetchClusterSummary(id),
}));

function summary(overrides: Partial<ClusterSummary> = {}): ClusterSummary {
  return {
    cluster: "prod",
    context: "prod-ctx",
    namespace: "payments",
    counters: {
      pods_ready: 8,
      pods_total: 8,
      workloads_degraded: 0,
      alerts_active: 0,
      alerts_sev1: 0,
    },
    generated_at: "2026-08-06T00:00:00Z",
    cache_age_seconds: 0,
    reason: null,
    ...overrides,
  };
}

beforeEach(() => {
  fetchClusterSummary.mockReset();
});

describe("HeaderLiveCounters", () => {
  it("shows pods, alerts and sev-1 once loaded", async () => {
    fetchClusterSummary.mockResolvedValue(
      summary({
        counters: {
          pods_ready: 6,
          pods_total: 8,
          workloads_degraded: 1,
          alerts_active: 3,
          alerts_sev1: 1,
        },
      }),
    );

    render(<HeaderLiveCounters sessionId="sess-1" />);

    expect(await screen.findByText("6/8")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("renders nothing when no cluster is connected", async () => {
    // A row of dashes is indistinguishable from a broken panel. The header
    // has other things in it; an empty gap is quieter and more honest.
    fetchClusterSummary.mockResolvedValue(
      summary({ counters: null, reason: "no_cluster" }),
    );

    const { container } = render(<HeaderLiveCounters sessionId="sess-1" />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("says so when the credential cannot read the namespace", async () => {
    fetchClusterSummary.mockResolvedValue(
      summary({ counters: null, reason: "insufficient_rbac" }),
    );

    render(<HeaderLiveCounters sessionId="sess-1" />);

    expect(await screen.findByText("no read access")).toBeInTheDocument();
  });

  it("colours sev-1 red only when there is one", async () => {
    fetchClusterSummary.mockResolvedValue(
      summary({
        counters: {
          pods_ready: 8,
          pods_total: 8,
          workloads_degraded: 0,
          alerts_active: 2,
          alerts_sev1: 2,
        },
      }),
    );

    render(<HeaderLiveCounters sessionId="sess-1" />);

    const sevLabel = await screen.findByText("sev-1");
    const value = sevLabel.previousElementSibling as HTMLElement;
    // Asserting the resolved custom property, not the literal string: a
    // token that does not exist resolves to nothing and renders unstyled,
    // silently, which is exactly how --warning/--success shipped once before.
    expect(value.style.color).toBe("var(--red)");
  });

  it("does not fetch without a session", () => {
    render(<HeaderLiveCounters sessionId={null} />);

    expect(fetchClusterSummary).not.toHaveBeenCalled();
  });

  it("keeps the last good numbers when a refresh fails", async () => {
    fetchClusterSummary
      .mockResolvedValueOnce(summary())
      .mockRejectedValue(new Error("network down"));

    render(<HeaderLiveCounters sessionId="sess-1" />);
    expect(await screen.findByText("8/8")).toBeInTheDocument();

    // A transient failure should cost freshness, not the numbers.
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(screen.getByText("8/8")).toBeInTheDocument();
  });

  it("marks the row stale when the server's cache is old", async () => {
    fetchClusterSummary.mockResolvedValue(summary({ cache_age_seconds: 95 }));

    render(<HeaderLiveCounters sessionId="sess-1" />);

    const row = await screen.findByRole("group");
    expect(row).toHaveAttribute("data-stale", "true");
    expect(row).toHaveAttribute("title", "Last read 95s ago");
  });

  it("gives screen readers one sentence rather than three numbers", async () => {
    fetchClusterSummary.mockResolvedValue(summary());

    render(<HeaderLiveCounters sessionId="sess-1" />);

    expect(
      await screen.findByLabelText(
        "8 of 8 pods ready, 0 alerts, 0 critical",
      ),
    ).toBeInTheDocument();
  });
});
