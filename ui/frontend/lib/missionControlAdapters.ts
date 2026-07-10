import type { ReactStep } from "../components/InvestigationTrail";
import type { ToolCardStep, ToolCardStatus } from "../components/ToolCard";
import type {
  DiagnosisSeverity,
  DiagnosisMetric,
  DiagnosisDiffLine,
} from "../components/MissionControlDiagnosis";
import { extractRootCause } from "../components/RootCauseCard";

function formatDurationMs(ms?: number): string | undefined {
  if (!ms || !Number.isFinite(ms)) return undefined;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function shortToolName(action: string): string {
  const first = action.split(/[_.:\s]/)[0];
  return first || action;
}

function formatKubectlish(action: string, params: Record<string, unknown>): string {
  const parts: string[] = [];
  const verb = action.split(/_/).slice(1).join(" ") || action;
  parts.push(shortToolName(action), verb);
  const name = params.name ?? params.pod ?? params.workload ?? params.resource;
  if (name) parts.push(String(name));
  const namespace = params.namespace ?? params.ns;
  if (namespace) parts.push("-n", String(namespace));
  const container = params.container;
  if (container) parts.push("-c", String(container));
  return parts.join(" ");
}

function buildCommandString(action: string, params?: Record<string, unknown>): string {
  const p = params ?? {};
  if (action.startsWith("kubectl") || action.startsWith("k_")) {
    return formatKubectlish(action, p);
  }
  if (Object.keys(p).length === 0) return action;
  const argStr = Object.entries(p)
    .map(([k, v]) => {
      const val = typeof v === "string" || typeof v === "number" || typeof v === "boolean"
        ? String(v)
        : JSON.stringify(v);
      return `${k}=${val}`;
    })
    .join(" ");
  return `${action} ${argStr}`;
}

function summarize(step: ReactStep): string {
  if (step.thought && step.thought.trim()) {
    const s = step.thought.trim();
    return s.length > 140 ? s.slice(0, 137) + "…" : s;
  }
  return step.action;
}

export function reactStepToToolCard(step: ReactStep, opts: { status?: ToolCardStatus } = {}): ToolCardStep {
  return {
    tool: shortToolName(step.action),
    cmd: buildCommandString(step.action, step.params),
    summary: summarize(step),
    duration: formatDurationMs(step.duration_ms),
    status: opts.status ?? "done",
  };
}

export function reactStepsToToolCards(steps: ReactStep[], thinking: boolean): ToolCardStep[] {
  if (steps.length === 0) return [];
  const last = steps.length - 1;
  return steps.map((s, i) => {
    const status: ToolCardStatus = thinking && i === last ? "running" : "done";
    return reactStepToToolCard(s, { status });
  });
}

/**
 * Extract diagnosis-ready fields from a tool result. Reuses the existing
 * extractRootCause helper (single source of truth for the shape). Returns
 * null when the result doesn't have a rootcause worth surfacing.
 */
type RootCauseShape = {
  rootCause: string;
  solution: string;
  severity: string;
  confidence: unknown;
};

function normalizeSeverity(sev: string): DiagnosisSeverity {
  const s = sev.toLowerCase();
  if (s === "critical" || s === "sev-1" || s === "sev1" || s === "high") return "sev-1";
  if (s === "warning" || s === "warn" || s === "sev-2" || s === "sev2" || s === "medium") return "sev-2";
  if (s === "info" || s === "informational") return "info";
  return "sev-3";
}

function normalizeConfidence(raw: unknown): number | undefined {
  if (typeof raw === "number" && Number.isFinite(raw)) {
    return raw > 1 ? Math.min(raw / 100, 1) : raw;
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim().replace(/%$/, "");
    const parsed = Number.parseFloat(trimmed);
    if (Number.isFinite(parsed)) return parsed > 1 ? Math.min(parsed / 100, 1) : parsed;
  }
  return undefined;
}

export type MissionControlDiagnosisMapped = {
  severity: DiagnosisSeverity;
  title: string;
  summary: string;
  confidence?: number;
  metrics?: DiagnosisMetric[];
  diff?: DiagnosisDiffLine[];
  diffMeta?: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractMetrics(result: Record<string, unknown>): DiagnosisMetric[] | undefined {
  const summary = isRecord(result.root_cause_summary) ? result.root_cause_summary : null;
  const metricsRaw = summary?.metrics ?? result.metrics;
  if (!Array.isArray(metricsRaw)) return undefined;
  const out: DiagnosisMetric[] = metricsRaw
    .filter(isRecord)
    .slice(0, 4)
    .map((m) => {
      const status = String(m.status ?? "").toLowerCase();
      const tone: DiagnosisMetric["tone"] =
        status === "critical" || status === "error" ? "critical"
        : status === "warning" || status === "warn" ? "warn"
        : "neutral";
      return {
        label: String(m.label ?? ""),
        value: String(m.value ?? ""),
        tone,
      };
    })
    .filter((m) => m.label && m.value);
  return out.length > 0 ? out : undefined;
}

export function resultToMissionControlDiagnosis(
  result: Record<string, unknown> | null | undefined,
): MissionControlDiagnosisMapped | null {
  const root = extractRootCause(result) as RootCauseShape | null;
  if (!root || !root.rootCause) return null;
  return {
    severity: normalizeSeverity(root.severity),
    title: root.rootCause,
    summary: root.solution || root.rootCause,
    confidence: normalizeConfidence(root.confidence),
    metrics: result ? extractMetrics(result) : undefined,
  };
}
