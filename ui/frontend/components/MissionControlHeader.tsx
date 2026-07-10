"use client";

import React, { useEffect, useState } from "react";
import { AstraGlyph } from "./AstraGlyph";
import type { ClusterStatus } from "../lib/api";

type MissionControlHeaderProps = {
  clusterStatus: ClusterStatus | null;
  busy?: boolean;
  version?: string;
  /**
   * Optional slot for right-aligned live stats (pods, alerts, sev-1).
   * Left as a prop so this component stays free of new backend calls —
   * the parent supplies whatever it already knows.
   */
  rightSlot?: React.ReactNode;
};

function useClock() {
  const [t, setT] = useState<string>("");
  useEffect(() => {
    const tick = () => setT(new Date().toISOString().slice(11, 19));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);
  return t;
}

type PillProps = {
  label: string;
  value: string;
  dotColor?: string;
  valueColor?: string;
};

function Pill({ label, value, dotColor, valueColor }: PillProps) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        background: "var(--bg-2, var(--paper-2))",
        border: "1px solid var(--line-2, var(--rule-2))",
        borderRadius: 4,
      }}
    >
      {dotColor && (
        <span
          aria-hidden="true"
          style={{
            width: 6,
            height: 6,
            borderRadius: 3,
            background: dotColor,
            boxShadow: `0 0 6px ${dotColor}`,
          }}
        />
      )}
      <span
        style={{
          fontFamily: "var(--mono)",
          fontSize: 8,
          textTransform: "uppercase",
          letterSpacing: "0.10em",
          color: "var(--ink-3, var(--fg-3))",
        }}
      >
        {label}
      </span>
      <span
        style={{
          fontFamily: "var(--mono)",
          fontSize: 10,
          color: valueColor ?? "var(--ink-2, var(--fg-2))",
        }}
      >
        {value}
      </span>
    </div>
  );
}

export function MissionControlHeader({
  clusterStatus,
  busy = false,
  version,
  rightSlot,
}: MissionControlHeaderProps) {
  const clock = useClock();
  const connected = !!clusterStatus?.connected;
  const clusterName = clusterStatus?.cluster_name ?? "not connected";
  const contextName = clusterStatus?.context_name ?? "—";
  const ns = clusterStatus?.namespace ?? "default";

  return (
    <header
      role="banner"
      style={{
        flexShrink: 0,
        height: 52,
        borderBottom: "1px solid var(--line, var(--rule))",
        background: "var(--bg-1, var(--paper-2))",
        display: "flex",
        alignItems: "center",
        padding: "0 18px",
        gap: 14,
        position: "relative",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <AstraGlyph size={22} animate={busy} />
        <div>
          <div style={{ display: "flex", alignItems: "baseline", gap: 6, lineHeight: 1 }}>
            <span
              style={{
                fontFamily: "var(--sans)",
                fontSize: 16,
                fontWeight: 700,
                color: "var(--ink, var(--fg-0))",
                letterSpacing: "-0.02em",
              }}
            >
              Kube<span style={{ color: "var(--cyan, var(--brand))" }}>Astra</span>
            </span>
            {version && (
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 9,
                  color: "var(--ink-4, var(--fg-4))",
                }}
              >
                {version}
              </span>
            )}
          </div>
          <span
            style={{
              display: "block",
              fontFamily: "var(--mono)",
              fontSize: 9,
              color: "var(--ink-3, var(--fg-3))",
              letterSpacing: "0.12em",
              marginTop: 3,
            }}
          >
            MISSION CONTROL
          </span>
        </div>
      </div>

      <div
        aria-hidden="true"
        style={{ width: 1, height: 22, background: "var(--line-2, var(--rule-2))" }}
      />

      <div style={{ display: "flex", gap: 8 }}>
        <Pill
          label="cluster"
          value={clusterName}
          dotColor={connected ? "var(--green)" : "var(--ink-4, var(--fg-4))"}
          valueColor={connected ? "var(--green)" : "var(--ink-3, var(--fg-3))"}
        />
        <Pill label="context" value={contextName} />
        <Pill label="ns" value={ns} />
      </div>

      <div style={{ flex: 1 }} />

      {rightSlot}

      {rightSlot && (
        <div
          aria-hidden="true"
          style={{ width: 1, height: 22, background: "var(--line-2, var(--rule-2))" }}
        />
      )}

      <span
        className="tab"
        style={{
          fontFamily: "var(--mono)",
          fontSize: 11,
          color: "var(--ink-2, var(--fg-2))",
          fontVariantNumeric: "tabular-nums",
          minWidth: 76,
          textAlign: "right",
        }}
        aria-label="UTC time"
      >
        {clock} UTC
      </span>

      <div
        aria-hidden="true"
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: -1,
          height: 1,
          background:
            "linear-gradient(90deg, transparent, var(--cyan, var(--brand)), transparent)",
          opacity: 0.3,
        }}
      />
    </header>
  );
}

export default MissionControlHeader;
