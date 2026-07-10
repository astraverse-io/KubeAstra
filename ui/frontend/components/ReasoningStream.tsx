"use client";

import React from "react";

type ReasoningStreamProps = {
  tokens: string[];
  active?: boolean;
};

/**
 * Terminal-style reasoning trace panel. Tokens arrive from the parent —
 * this component is presentation-only. Each token animates in via
 * mcStreamIn with a small stagger; the parent controls arrival timing
 * (drive from real SSE events, not setTimeout).
 */
export function ReasoningStream({ tokens, active = false }: ReasoningStreamProps) {
  if (tokens.length === 0 && !active) return null;

  return (
    <div
      style={{
        borderLeft: "2px solid var(--mag-bd, var(--brand-bd))",
        paddingLeft: 14,
        marginBottom: 14,
      }}
      aria-live="polite"
      aria-atomic="false"
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="var(--mag, var(--brand))"
          strokeWidth={1.8}
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="3" />
          <path d="M12 1v6m0 10v6m-9-9H9m6 0h6M5.6 5.6l4.2 4.2m4.4 4.4l4.2 4.2M5.6 18.4l4.2-4.2m4.4-4.4l4.2-4.2" />
        </svg>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 9,
            textTransform: "uppercase",
            letterSpacing: "0.10em",
            color: "var(--mag, var(--brand))",
          }}
        >
          Astra · reasoning trace
        </span>
        {active && (
          <span style={{ display: "flex", gap: 3, marginLeft: 4 }} aria-hidden="true">
            {[0, 1, 2].map((d) => (
              <span
                key={d}
                style={{
                  width: 3,
                  height: 3,
                  borderRadius: "50%",
                  background: "var(--mag, var(--brand))",
                  animation: `dotBounce 1.1s ${d * 0.18}s ease-in-out infinite`,
                }}
              />
            ))}
          </span>
        )}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {tokens.map((t, i) => (
          <div
            key={i}
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              lineHeight: 1.55,
              color: "var(--ink-2, var(--fg-2))",
              fontStyle: "italic",
              animation: `mcStreamIn 0.4s ${Math.min(i, 8) * 60}ms ease both`,
            }}
          >
            <span
              aria-hidden="true"
              style={{ color: "var(--mag, var(--brand))", marginRight: 6 }}
            >
              ›
            </span>
            {t}
          </div>
        ))}
      </div>
    </div>
  );
}

export default ReasoningStream;
