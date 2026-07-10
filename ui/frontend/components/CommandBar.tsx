"use client";

import React, { useCallback, useRef, useState } from "react";

export type QuickProbe = {
  icon: string;
  text: string;
};

type CommandBarProps = {
  onSend: (text: string) => void;
  busy?: boolean;
  clusterLabel?: string;
  clusterConnected?: boolean;
  version?: string;
  toolCount?: number;
  quickProbes?: QuickProbe[];
  apiLatencyMs?: number | null;
  placeholder?: string;
};

const DEFAULT_PROBES: QuickProbe[] = [
  { icon: "⚠", text: "Why is api-gateway crashing?" },
  { icon: "📊", text: "Show high-memory pods" },
  { icon: "⏱", text: "Last night's deploy failures" },
  { icon: "🔍", text: "Diff last 24h configmap changes" },
];

export function CommandBar({
  onSend,
  busy = false,
  clusterLabel,
  clusterConnected = false,
  version,
  toolCount,
  quickProbes = DEFAULT_PROBES,
  apiLatencyMs = null,
  placeholder,
}: CommandBarProps) {
  const [val, setVal] = useState("");
  const [focused, setFocused] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const submit = useCallback(() => {
    const trimmed = val.trim();
    if (!trimmed || busy) return;
    onSend(trimmed);
    setVal("");
  }, [busy, onSend, val]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    },
    [submit],
  );

  const canSend = val.trim().length > 0 && !busy;

  return (
    <div
      style={{
        flexShrink: 0,
        borderTop: "1px solid var(--line, var(--rule))",
        background: "var(--bg-1, var(--paper-2))",
      }}
    >
      {quickProbes.length > 0 && (
        <div
          style={{
            padding: "8px 18px",
            borderBottom: "1px solid var(--line, var(--rule))",
            display: "flex",
            alignItems: "center",
            gap: 8,
            overflowX: "auto",
          }}
        >
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 9,
              textTransform: "uppercase",
              letterSpacing: "0.10em",
              color: "var(--ink-3, var(--fg-3))",
              flexShrink: 0,
            }}
          >
            Quick probes
          </span>
          {quickProbes.map((q) => (
            <button
              key={q.text}
              type="button"
              onClick={() => !busy && onSend(q.text)}
              disabled={busy}
              className="mc-probe-chip"
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                background: "var(--bg-2, var(--paper-3))",
                border: "1px solid var(--line-2, var(--rule-2))",
                borderRadius: 12,
                cursor: busy ? "default" : "pointer",
                fontFamily: "var(--mono)",
                fontSize: 10,
                color: "var(--ink-2, var(--fg-2))",
                whiteSpace: "nowrap",
                flexShrink: 0,
                opacity: busy ? 0.5 : 1,
                transition: "border-color 0.15s, color 0.15s",
              }}
            >
              <span aria-hidden="true" style={{ fontFamily: "sans-serif", filter: "grayscale(0.4)" }}>
                {q.icon}
              </span>
              {q.text}
            </button>
          ))}
        </div>
      )}

      <div
        style={{
          padding: "14px 18px",
          display: "flex",
          alignItems: "center",
          gap: 12,
          background: "var(--bg-1, var(--paper-2))",
        }}
      >
        {clusterLabel && (
          <div
            style={{
              padding: "6px 10px",
              background: "var(--bg-0, var(--paper))",
              border: "1px solid var(--cyan-bd, var(--brand-bd))",
              borderRadius: 5,
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexShrink: 0,
            }}
            aria-label={`Active cluster: ${clusterLabel}`}
          >
            <span
              aria-hidden="true"
              style={{
                width: 5,
                height: 5,
                borderRadius: "50%",
                background: clusterConnected ? "var(--green)" : "var(--ink-4, var(--fg-4))",
                animation: clusterConnected ? "pulseRing 2s ease-in-out infinite" : "none",
              }}
            />
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 10,
                fontWeight: 600,
                color: "var(--cyan, var(--brand))",
              }}
            >
              {clusterLabel}
            </span>
          </div>
        )}

        <label
          style={{
            flex: 1,
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 12px",
            background: "var(--bg-0, var(--paper))",
            border: `1px solid ${focused ? "var(--cyan, var(--brand))" : "var(--line-2, var(--rule-2))"}`,
            borderRadius: 6,
            boxShadow: focused ? "0 0 0 3px var(--cyan-bg, var(--brand-bg))" : "none",
            transition: "border-color 0.2s, box-shadow 0.2s",
          }}
        >
          <span
            aria-hidden="true"
            style={{
              fontFamily: "var(--mono)",
              fontSize: 13,
              color: focused ? "var(--cyan, var(--brand))" : "var(--ink-3, var(--fg-3))",
              flexShrink: 0,
            }}
          >
            astra›
          </span>
          <input
            ref={inputRef}
            value={val}
            onChange={(e) => setVal(e.target.value)}
            onKeyDown={onKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            placeholder={busy ? "investigating…" : (placeholder ?? "ask anything · describe an incident · paste a manifest…")}
            disabled={busy}
            aria-label="Ask KubeAstra"
            style={{
              flex: 1,
              background: "transparent",
              border: "none",
              outline: "none",
              fontFamily: "var(--mono)",
              fontSize: 12,
              color: "var(--ink, var(--fg-0))",
            }}
          />
          {busy && (
            <span style={{ display: "flex", gap: 3 }} aria-hidden="true">
              {[0, 1, 2].map((d) => (
                <span
                  key={d}
                  style={{
                    width: 3,
                    height: 3,
                    borderRadius: "50%",
                    background: "var(--cyan, var(--brand))",
                    animation: `dotBounce 1.1s ${d * 0.18}s ease-in-out infinite`,
                  }}
                />
              ))}
            </span>
          )}
        </label>

        <button
          type="button"
          onClick={submit}
          disabled={!canSend}
          style={{
            padding: "10px 16px",
            background: canSend ? "var(--cyan, var(--brand))" : "var(--bg-2, var(--paper-3))",
            color: canSend ? "var(--bg-0, var(--paper))" : "var(--ink-3, var(--fg-3))",
            border: "none",
            borderRadius: 5,
            cursor: canSend ? "pointer" : "default",
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontFamily: "var(--mono)",
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.1em",
            transition: "background 0.15s, color 0.15s",
          }}
        >
          DISPATCH
          <span aria-hidden="true">↵</span>
        </button>
      </div>

      <div
        style={{
          padding: "0 18px 10px",
          display: "flex",
          alignItems: "center",
          gap: 14,
          fontFamily: "var(--mono)",
          fontSize: 9,
          color: "var(--ink-3, var(--fg-3))",
          flexWrap: "wrap",
        }}
      >
        {version && (
          <>
            <span>{version}</span>
            <span aria-hidden="true">·</span>
          </>
        )}
        {typeof toolCount === "number" && (
          <>
            <span>{toolCount} tools online</span>
            <span aria-hidden="true">·</span>
          </>
        )}
        <span>read-only by default — mutations require approval</span>
        <span style={{ flex: 1 }} />
        {apiLatencyMs != null && (
          <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span
              aria-hidden="true"
              style={{
                width: 4,
                height: 4,
                background: apiLatencyMs < 200 ? "var(--green)" : "var(--amber)",
                borderRadius: "50%",
              }}
            />
            api {apiLatencyMs < 200 ? "healthy" : "slow"} · {apiLatencyMs}ms
          </span>
        )}
      </div>
    </div>
  );
}

export default CommandBar;
