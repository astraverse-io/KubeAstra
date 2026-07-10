import { describe, expect, it } from "vitest";
import {
  reactStepsToToolCards,
  resultToMissionControlDiagnosis,
} from "../missionControlAdapters";

describe("reactStepsToToolCards", () => {
  it("returns [] on empty input", () => {
    expect(reactStepsToToolCards([], false)).toEqual([]);
  });

  it("marks the last step running when thinking=true and rest done", () => {
    const steps = [
      { action: "kubectl_describe_pod", params: { name: "p1", namespace: "prod" }, duration_ms: 410 },
      { action: "prometheus_query", params: { promql: "up" }, duration_ms: 620 },
    ];
    const cards = reactStepsToToolCards(steps, true);
    expect(cards[0].status).toBe("done");
    expect(cards[1].status).toBe("running");
    expect(cards[1].tool).toBe("prometheus");
  });

  it("uses thought as summary when present, action as fallback", () => {
    const withThought = reactStepsToToolCards(
      [{ action: "kubectl_get_pods", thought: "checking pod list first" }],
      false,
    );
    expect(withThought[0].summary).toBe("checking pod list first");

    const noThought = reactStepsToToolCards(
      [{ action: "kubectl_get_pods" }],
      false,
    );
    expect(noThought[0].summary).toBe("kubectl_get_pods");
  });

  it("formats duration to human-readable string", () => {
    const cards = reactStepsToToolCards(
      [{ action: "kubectl_get_pods", duration_ms: 415 }],
      false,
    );
    expect(cards[0].duration).toBe("415ms");

    const seconds = reactStepsToToolCards(
      [{ action: "kubectl_get_pods", duration_ms: 2340 }],
      false,
    );
    expect(seconds[0].duration).toBe("2.34s");
  });

  it("builds kubectl-style cmd for kubectl_ actions", () => {
    const cards = reactStepsToToolCards(
      [{ action: "kubectl_describe_pod", params: { name: "gateway-1", namespace: "prod" } }],
      false,
    );
    expect(cards[0].cmd).toBe("kubectl describe pod gateway-1 -n prod");
  });
});

describe("resultToMissionControlDiagnosis", () => {
  it("returns null for null input", () => {
    expect(resultToMissionControlDiagnosis(null)).toBeNull();
    expect(resultToMissionControlDiagnosis(undefined)).toBeNull();
  });

  it("returns null when no root cause is present", () => {
    expect(resultToMissionControlDiagnosis({})).toBeNull();
    expect(resultToMissionControlDiagnosis({ ai: { ai_analysis: {} } })).toBeNull();
  });

  it("extracts fields from root_cause_summary shape", () => {
    const out = resultToMissionControlDiagnosis({
      root_cause_summary: {
        root_cause: "OOMKilled loop",
        suggested_fix: "raise memory ceiling",
        severity: "critical",
        confidence: 0.94,
      },
    });
    expect(out).toEqual({
      severity: "sev-1",
      title: "OOMKilled loop",
      summary: "raise memory ceiling",
      confidence: 0.94,
      metrics: undefined,
    });
  });

  it("normalizes percentage confidence (0-100) to fraction (0-1)", () => {
    const out = resultToMissionControlDiagnosis({
      root_cause_summary: { root_cause: "x", suggested_fix: "y", severity: "warning", confidence: 87 },
    });
    expect(out?.confidence).toBeCloseTo(0.87, 2);
  });

  it("maps warning severity to sev-2", () => {
    const out = resultToMissionControlDiagnosis({
      root_cause_summary: { root_cause: "x", suggested_fix: "y", severity: "warning" },
    });
    expect(out?.severity).toBe("sev-2");
  });

  it("extracts metrics when present on root_cause_summary", () => {
    const out = resultToMissionControlDiagnosis({
      root_cause_summary: {
        root_cause: "x",
        suggested_fix: "y",
        severity: "info",
        metrics: [
          { label: "Restarts", value: "12", status: "critical" },
          { label: "Drift", value: "92d", status: "normal" },
        ],
      },
    });
    expect(out?.metrics).toEqual([
      { label: "Restarts", value: "12", tone: "critical" },
      { label: "Drift", value: "92d", tone: "neutral" },
    ]);
  });
});
