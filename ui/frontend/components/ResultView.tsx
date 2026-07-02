"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Code, Copy, Check } from "lucide-react";
import CommandCard from "./CommandCard";
import { copyToClipboard } from "../lib/clipboard";

const SEVERITY_COLORS: Record<string, string> = {
  critical: "bg-red-900/50 border-red-700 text-red-300",
  high: "bg-orange-900/50 border-orange-700 text-orange-300",
  medium: "bg-yellow-900/50 border-yellow-700 text-yellow-300",
  low: "bg-green-900/50 border-green-700 text-green-300",
  unknown: "bg-gray-800 border-gray-600 text-gray-300",
};

type Classification = {
  source?: string;
  deterministic_category?: string;
  llm_category?: string | null;
};

function asClassification(v: unknown): Classification | null {
  if (!v || typeof v !== "object") return null;
  return v as Classification;
}

function classificationDisagreed(v: unknown): boolean {
  const c = asClassification(v);
  if (!c) return false;
  const d = c.deterministic_category;
  const l = c.llm_category;
  return !!d && !!l && d !== l;
}

function classificationTitle(v: unknown): string | undefined {
  const c = asClassification(v);
  if (!c) return undefined;
  const parts: string[] = [];
  if (c.source) parts.push(`source: ${c.source}`);
  if (c.deterministic_category) parts.push(`deterministic: ${c.deterministic_category}`);
  if (c.llm_category) parts.push(`LLM: ${c.llm_category}`);
  return parts.length > 0 ? parts.join(" • ") : undefined;
}

interface Props {
  result: Record<string, unknown> | null;
  loading: boolean;
  error: string | null;
}

export default function ResultView({ result, loading, error }: Props) {
  const [showRaw, setShowRaw] = useState(false);
  const [copied, setCopied] = useState(false);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-500">
        <div className="text-center space-y-3">
          <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin mx-auto" />
          <p className="text-sm">Analyzing...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-red-800 bg-red-900/20 p-4">
        <p className="text-red-400 text-sm font-medium mb-1">Error</p>
        <p className="text-red-300 text-sm">{error}</p>
      </div>
    );
  }

  if (!result) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-600">
        <p className="text-sm">Results will appear here</p>
      </div>
    );
  }

  const copyJson = async () => {
    await copyToClipboard(JSON.stringify(result, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const severity = (result.severity as string) ?? "unknown";
  const commands = result.commands as Array<{ cmd: string; description?: string }> | undefined;
  const steps = result.steps as string[] | undefined;
  const similarCases = result.similar_cases as Array<{
    error: string; solution: string; similarity: string; success_rate: string;
  }> | undefined;

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-gray-500 uppercase tracking-wider">Result</span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowRaw(!showRaw)}
            className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-gray-800 transition-colors"
          >
            <Code size={12} />
            {showRaw ? "Hide JSON" : "Raw JSON"}
          </button>
          <button
            onClick={copyJson}
            className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-gray-800 transition-colors"
          >
            {copied ? <Check size={12} className="text-green-400" /> : <Copy size={12} />}
            Copy
          </button>
        </div>
      </div>

      {/* Severity + confidence */}
      {!!(result.severity || result.confidence) && (
        <div className="flex items-center gap-3">
          {!!result.severity && (
            <span
              className={`text-xs font-medium px-2.5 py-1 rounded border capitalize ${SEVERITY_COLORS[severity] ?? SEVERITY_COLORS.unknown}`}
            >
              {severity}
            </span>
          )}
          {result.confidence !== undefined && (
            <span className="text-xs text-gray-400">
              {Math.round((result.confidence as number) * 100)}% confidence
            </span>
          )}
          {!!result.category && (
            <span
              className="text-xs text-gray-500 font-mono"
              title={classificationTitle(result.classification)}
            >
              {String(result.category)}
              {classificationDisagreed(result.classification) && (
                <span className="ml-1 text-yellow-400" aria-label="classification override">*</span>
              )}
            </span>
          )}
        </div>
      )}

      {/* Root cause */}
      {result.root_cause ? (
        <Card title="Root Cause">
          <p className="text-sm text-gray-300 leading-relaxed">{String(result.root_cause)}</p>
        </Card>
      ) : null}

      {/* Solution */}
      {result.solution ? (
        <Card title="Solution">
          <p className="text-sm text-gray-300 leading-relaxed">{String(result.solution)}</p>
        </Card>
      ) : null}

      {Array.isArray(steps) && steps.length > 0 ? (
        <Card title="Steps">
          <ol className="space-y-2">
            {steps.map((s, i) => (
              <li key={i} className="flex items-start gap-3 text-sm text-gray-300">
                <span className="shrink-0 w-5 h-5 rounded-full bg-blue-700 text-white text-xs flex items-center justify-center mt-0.5">
                  {i + 1}
                </span>
                <span className="leading-relaxed">{s}</span>
              </li>
            ))}
          </ol>
        </Card>
      ) : null}

      {/* Commands */}
      {Array.isArray(commands) && commands.length > 0 ? (
        <CommandCard commands={commands} title="Fix Commands" />
      ) : null}

      {/* Prevention */}
      {result.prevention ? (
        <Card title="Prevention">
          <p className="text-sm text-gray-400 leading-relaxed">{String(result.prevention)}</p>
        </Card>
      ) : null}

      {/* Runbook markdown */}
      {result.runbook ? (
        <Card title="Generated Runbook">
          <pre className="text-xs text-gray-300 whitespace-pre-wrap leading-relaxed font-mono bg-gray-900 rounded p-3 overflow-auto max-h-96">
            {String(result.runbook)}
          </pre>
        </Card>
      ) : null}

      {/* Similar cases */}
      {Array.isArray(similarCases) && similarCases.length > 0 ? (
        <Collapsible title={`Similar Cases (${similarCases.length})`}>
          <div className="space-y-3">
            {similarCases.map((c, i) => (
              <div key={i} className="rounded border border-gray-700 p-3 bg-gray-900 space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-blue-400 font-medium">{c.similarity}</span>
                  <span className="text-xs text-gray-500">match</span>
                  <span className="text-xs text-gray-500">·</span>
                  <span className="text-xs text-green-400">{c.success_rate}</span>
                  <span className="text-xs text-gray-500">success</span>
                </div>
                <p className="text-xs text-gray-400 line-clamp-2">{c.error}</p>
                <p className="text-xs text-gray-500 line-clamp-2">{c.solution}</p>
              </div>
            ))}
          </div>
        </Collapsible>
      ) : null}

      {/* AI analysis inside investigate_pod result */}
      {result.ai && typeof result.ai === "object" && (result.ai as Record<string, unknown>).ai_analysis ? (
        <Card title="AI Diagnosis (Live Data)">
          <ResultView
            result={(result.ai as Record<string, unknown>).ai_analysis as Record<string, unknown>}
            loading={false}
            error={null}
          />
        </Card>
      ) : null}

      {/* Generic fields */}
      {result.description ? (
        <Card title="Description">
          <p className="text-sm text-gray-300">{String(result.description)}</p>
        </Card>
      ) : null}

      {result.ai_summary ? (
        <Card title="AI Summary">
          <p className="text-sm text-gray-300 leading-relaxed whitespace-pre-wrap">
            {String(result.ai_summary)}
          </p>
        </Card>
      ) : null}

      {/* Raw JSON */}
      {showRaw ? (
        <div className="rounded-lg border border-gray-700 overflow-hidden">
          <div className="px-4 py-2 bg-gray-800 border-b border-gray-700 text-xs text-gray-400">
            Raw JSON
          </div>
          <pre className="p-4 text-xs text-gray-400 overflow-auto max-h-96 bg-gray-900 font-mono">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-gray-700 overflow-hidden">
      <div className="px-4 py-2.5 bg-gray-800 border-b border-gray-700">
        <span className="text-xs font-medium text-gray-300 uppercase tracking-wide">{title}</span>
      </div>
      <div className="px-4 py-3 bg-gray-900">{children}</div>
    </div>
  );
}

function Collapsible({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-gray-700 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-2.5 bg-gray-800 hover:bg-gray-750 transition-colors"
      >
        <span className="text-xs font-medium text-gray-300 uppercase tracking-wide">{title}</span>
        {open ? <ChevronDown size={14} className="text-gray-400" /> : <ChevronRight size={14} className="text-gray-400" />}
      </button>
      {open && <div className="px-4 py-3 bg-gray-900">{children}</div>}
    </div>
  );
}
