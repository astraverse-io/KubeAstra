"use client";

/**
 * The header's primary element: which cluster this session is aimed at.
 *
 * It replaced a row of same-sized buttons — "Connect Cluster", "SSH Cluster",
 * a context pill, "Switch", "Disconnect", plus a "no cluster" health dot — six
 * controls describing one fact. Which cluster you are pointed at is the most
 * consequential thing in this app: every answer the assistant gives is wrong,
 * or dangerous, if the target is wrong. It should not be a peer of the theme
 * toggle.
 *
 * So the target is state, rendered large, and clicking it is how you change it
 * — the same affordance a browser address bar has. "Connect" and "over SSH"
 * become two routes inside one popover rather than two sibling buttons.
 */

import React from "react";
import { AstraGlyph } from "./AstraGlyph";

export type TargetBarProps = {
  /** Cluster context this session is bound to, if any. */
  contextName?: string | null;
  /** Namespace within that context. */
  namespace?: string | null;
  /** SSH host, when the cluster is reached over SSH rather than kubeconfig. */
  sshHost?: string | null;
  /** How the target was reached — shown as the second line. */
  mode?: string | null;
  /** False while the health check is still in flight. */
  loaded?: boolean;
  /** Whether kubectl can currently reach the target. */
  reachable?: boolean;
  onClick: () => void;
  expanded?: boolean;
};

export default function TargetBar({
  contextName,
  namespace,
  sshHost,
  mode,
  loaded = true,
  reachable = false,
  onClick,
  expanded = false,
}: TargetBarProps) {
  const target = sshHost || contextName || null;
  const aimed = Boolean(target);

  // Three states, not two: "still checking" must not look like "nothing here",
  // or the bar flashes an invitation to connect on every page load.
  const detail = !loaded
    ? "checking…"
    : sshHost
      ? "over SSH"
      : reachable
        ? mode === "in_cluster" ? "in-cluster" : "local kubeconfig"
        : "not reachable";

  const pip = !loaded
    ? "var(--rule-2)"
    : aimed && reachable
      ? "var(--green)"
      : "var(--amber)";

  return (
    <button
      type="button"
      onClick={onClick}
      className="ka-target"
      aria-expanded={expanded}
      aria-label={
        aimed
          ? `Aimed at ${target}${namespace ? `, namespace ${namespace}` : ""}. Change target.`
          : "No cluster selected. Choose one."
      }
      title={aimed ? "Change which cluster this session targets" : "Choose a cluster"}
    >
      <span className="ka-target-glyph">
        <AstraGlyph size={22} />
      </span>
      {/* The aim line. Ties the mark to the target and is the one place the
          brand colour carries weight in the header. */}
      <span className={`ka-aimline${aimed ? "" : " ka-aimline-idle"}`} aria-hidden="true" />
      <span className="ka-target-body">
        <span className="ka-target-label">{aimed ? "Aimed at" : "No target"}</span>
        <span className={`ka-target-ctx${aimed ? "" : " ka-target-ctx-empty"}`}>
          {aimed ? target : "Choose a cluster to aim at"}
          {aimed && namespace && <span className="ka-target-ns"> · {namespace}</span>}
        </span>
        <span className="ka-target-live">
          <span className="ka-pip" style={{ background: pip }} aria-hidden="true" />
          {detail}
        </span>
      </span>
    </button>
  );
}
