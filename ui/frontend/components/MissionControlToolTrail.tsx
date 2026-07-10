"use client";

import React, { useState } from "react";
import { ReasoningStream } from "./ReasoningStream";
import { ToolCard } from "./ToolCard";
import type { ReactStep } from "./InvestigationTrail";
import { reactStepsToToolCards } from "../lib/missionControlAdapters";

type MissionControlToolTrailProps = {
  steps: ReactStep[];
  thinking: boolean;
};

/**
 * Mission-control version of InvestigationTrail — same input shape,
 * different rendering: a reasoning stream (built from step thoughts)
 * on top, then progress-meter + ToolCard list underneath.
 */
export function MissionControlToolTrail({ steps, thinking }: MissionControlToolTrailProps) {
  const [expanded, setExpanded] = useState<number | null>(steps.length > 0 ? 0 : null);
  const toolCards = reactStepsToToolCards(steps, thinking);
  const thoughts = steps
    .map((s) => (s.thought ?? "").trim())
    .filter((t) => t.length > 0);
  const doneCount = toolCards.filter((s) => s.status === "done").length;
  const total = toolCards.length;

  if (total === 0 && !thinking) return null;

  return (
    <div style={{ width: "100%", maxWidth: "100%" }}>
      {thoughts.length > 0 && <ReasoningStream tokens={thoughts} active={thinking} />}

      {total > 0 && (
        <div>
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
              {doneCount}/{total}
            </span>
            <div
              style={{
                flex: 1,
                height: 2,
                background: "var(--line, var(--rule))",
                borderRadius: 1,
                overflow: "hidden",
              }}
              aria-hidden="true"
            >
              <div
                style={{
                  height: "100%",
                  width: `${(doneCount / total) * 100}%`,
                  background: "var(--cyan, var(--brand))",
                  transition: "width 0.4s ease",
                  boxShadow: "0 0 8px var(--cyan, var(--brand))",
                }}
              />
            </div>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {toolCards.map((step, i) => (
              <ToolCard
                key={`${step.tool}-${i}`}
                step={step}
                idx={i}
                expanded={expanded === i}
                onToggle={() => setExpanded(expanded === i ? null : i)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default MissionControlToolTrail;
