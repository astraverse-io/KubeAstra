"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import InvestigationViewer from "@/components/InvestigationViewer";

// Status strings the backend uses for in-flight investigations. Anything
// outside this set is treated as terminal (completed / failed / unknown).
const RUNNING_STATUSES = new Set(["received", "classified", "running"]);
const POLL_INTERVAL_MS = 5000;
// localStorage key for the user's namespace-filter selection. Persisted
// across reloads so the filter survives both Refresh button + auto-poll
// without the user re-picking namespaces on every visit.
const NAMESPACE_FILTER_KEY = "k8s_alerts_namespace_filter";

type AlertRow = {
  id: string;
  namespace: string | null;
  severity: string | null;
  source: string | null;
  status: string | null;
  created_at: string;
  document: Record<string, unknown>;
};

function severityTokens(sev: string | null | undefined): { fg: string; bg: string; bd: string } {
  const s = (sev || "").toLowerCase();
  if (s === "critical" || s === "high") return { fg: "var(--red)", bg: "var(--red-bg)", bd: "var(--red-bd)" };
  if (s === "warning" || s === "medium") return { fg: "var(--amber)", bg: "var(--amber-bg)", bd: "var(--amber-bd)" };
  return { fg: "var(--ink-3)", bg: "var(--paper-3)", bd: "var(--rule)" };
}

function statusTokens(st: string | null | undefined): { fg: string; bg: string; bd: string; label: string } {
  const s = (st || "").toLowerCase();
  if (s === "completed") return { fg: "var(--green)", bg: "var(--green-bg)", bd: "var(--green-bd)", label: "Completed" };
  if (s === "failed") return { fg: "var(--red)", bg: "var(--red-bg)", bd: "var(--red-bd)", label: "Failed" };
  if (s === "running") return { fg: "var(--brand)", bg: "var(--brand-bg)", bd: "var(--brand-bd)", label: "Running" };
  return { fg: "var(--ink-3)", bg: "var(--paper-3)", bd: "var(--rule)", label: s || "Unknown" };
}

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [selected, setSelected] = useState<AlertRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null);

  // Single fetch path used by both the initial load, the manual Refresh
  // button, and the poll-while-running interval. Preserves the user's
  // current selection across refreshes so polling never yanks the open
  // investigation out from under them.
  const loadAlerts = useCallback(async (): Promise<void> => {
    setRefreshing(true);
    try {
      const res = await fetch("/api/v1/alerts");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const rows: AlertRow[] = data.alerts || [];
      setAlerts(rows);
      setError(null);
      setSelected((prev) => {
        if (prev) {
          const matched = rows.find((r) => r.id === prev.id);
          if (matched) return matched;
        }
        return rows[0] || null;
      });
      setLastUpdatedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAlerts();
  }, [loadAlerts]);

  // Auto-poll while any visible alert is still in flight. Stops as soon as
  // every row is in a terminal state — no perpetual background traffic.
  useEffect(() => {
    const hasInflight = alerts.some((a) =>
      RUNNING_STATUSES.has((a.status || "").toLowerCase())
    );
    if (!hasInflight) return;
    const id = window.setInterval(() => {
      void loadAlerts();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [alerts, loadAlerts]);

  // Namespace filter. null = no filter (show everything); a Set means show
  // only those namespaces. Hydrate from localStorage on first render so a
  // user who picked "only kube-system + jenkins-legacy" yesterday still
  // sees that filter today.
  const [selectedNamespaces, setSelectedNamespaces] = useState<Set<string> | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      const stored = window.localStorage.getItem(NAMESPACE_FILTER_KEY);
      if (!stored) return null;
      const parsed = JSON.parse(stored);
      if (!Array.isArray(parsed) || parsed.length === 0) return null;
      return new Set(parsed as string[]);
    } catch {
      return null;
    }
  });

  // Persist filter changes to localStorage. Clears the key entirely when the
  // user reverts to "no filter" so we don't leave stale state behind.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (selectedNamespaces === null) {
      window.localStorage.removeItem(NAMESPACE_FILTER_KEY);
    } else {
      window.localStorage.setItem(
        NAMESPACE_FILTER_KEY,
        JSON.stringify([...selectedNamespaces]),
      );
    }
  }, [selectedNamespaces]);

  // All namespaces present in the currently-loaded alerts, sorted. Recomputes
  // when polling brings in alerts from a new namespace — the chip row picks
  // it up automatically without a page reload.
  const availableNamespaces = useMemo(() => {
    const set = new Set<string>();
    for (const a of alerts) {
      if (a.namespace) set.add(a.namespace);
    }
    return [...set].sort();
  }, [alerts]);

  // The list rendered in the sidebar. When the filter is active, drop rows
  // whose namespace isn't selected.
  const filteredAlerts = useMemo(() => {
    if (!selectedNamespaces) return alerts;
    return alerts.filter((a) => a.namespace && selectedNamespaces.has(a.namespace));
  }, [alerts, selectedNamespaces]);

  // Toggle one namespace in the visible set. "Show all" / "show none" both
  // collapse to null (no filter) — explicit empty-set filtering is more
  // confusing than useful.
  const toggleNamespace = useCallback(
    (ns: string) => {
      setSelectedNamespaces((prev) => {
        const base = prev ?? new Set(availableNamespaces);
        const next = new Set(base);
        if (next.has(ns)) {
          next.delete(ns);
        } else {
          next.add(ns);
        }
        if (next.size === 0 || next.size === availableNamespaces.length) return null;
        return next;
      });
    },
    [availableNamespaces],
  );

  const clearNamespaceFilter = useCallback(() => setSelectedNamespaces(null), []);

  // If the user's selection gets filtered out (e.g. they just added a
  // filter that excludes the currently-open investigation), jump to the
  // first visible row so the detail pane and the list stay consistent.
  useEffect(() => {
    if (!selected) return;
    if (filteredAlerts.some((a) => a.id === selected.id)) return;
    setSelected(filteredAlerts[0] || null);
  }, [filteredAlerts, selected]);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "var(--paper)",
        color: "var(--ink)",
        fontFamily: "var(--sans)",
      }}
    >
      {/* Header bar — mirrors the chat page brand strip */}
      <div
        style={{
          borderBottom: "1px solid var(--rule)",
          padding: "0.875rem 1.25rem",
          background: "var(--paper-2)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "0.625rem" }}>
          <span
            style={{
              width: "0.5rem",
              height: "0.5rem",
              borderRadius: "50%",
              background: "var(--brand)",
            }}
          />
          <h1 style={{ margin: 0, fontSize: "1rem", fontWeight: 700 }}>Alerts &amp; Investigations</h1>
        </div>
        <button
          onClick={() => {
            // Return to the session the user came from (set by the chat page's
            // "Alerts" button). The /chat/[sessionId] route stores the id in
            // sessionStorage and redirects back into the main chat with that
            // session restored.
            let returnSession: string | null = null;
            if (typeof window !== "undefined") {
              returnSession = sessionStorage.getItem("k8s_chat_return_session");
            }
            // `/chat?session=<id>`, never `/chat/<id>`. The path form is a
            // dynamic route, and a dynamic route cannot exist in the desktop
            // static export — session ids are not knowable at build time, so
            // generateStaticParams() has nothing to enumerate. The server
            // build has a back-compat page that forwards the old shape, but
            // build-desktop.mjs stashes that page aside, so in the desktop app
            // this landed on a 404 with the chat still sitting there behind it.
            window.location.href = returnSession
              ? `/chat?session=${encodeURIComponent(returnSession)}`
              : "/chat";
          }}
          style={{
            fontSize: "0.8125rem",
            color: "var(--ink-3)",
            textDecoration: "none",
            background: "transparent",
            border: "1px solid var(--rule)",
            padding: "0.375rem 0.625rem",
            borderRadius: "0.5rem",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          ← Back to chat
        </button>
      </div>

      {/* Two-pane layout */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "20rem 1fr",
          gap: "1rem",
          padding: "1rem",
          height: "calc(100vh - 3.5rem)",
          boxSizing: "border-box",
        }}
      >
        {/* Sidebar — recent alerts */}
        <aside
          style={{
            background: "var(--paper-2)",
            border: "1px solid var(--rule)",
            borderRadius: "0.75rem",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          <div
            style={{
              padding: "0.75rem 1rem",
              borderBottom: "1px solid var(--rule)",
              fontSize: "0.75rem",
              letterSpacing: "0.04em",
              textTransform: "uppercase",
              color: "var(--ink-3)",
              fontWeight: 600,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "0.5rem",
            }}
          >
            <span>
              Recent (
              {selectedNamespaces
                ? `${filteredAlerts.length} of ${alerts.length}`
                : alerts.length}
              )
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              {lastUpdatedAt !== null && (
                <span
                  style={{
                    fontFamily: "var(--mono)",
                    fontSize: "0.625rem",
                    textTransform: "none",
                    letterSpacing: "0",
                    color: "var(--ink-3)",
                  }}
                  title={new Date(lastUpdatedAt).toLocaleString()}
                >
                  {refreshing ? "Updating…" : `Updated ${new Date(lastUpdatedAt).toLocaleTimeString()}`}
                </span>
              )}
              <button
                onClick={() => {
                  void loadAlerts();
                }}
                disabled={refreshing}
                title="Refresh now"
                style={{
                  background: "transparent",
                  border: "1px solid var(--rule)",
                  borderRadius: "0.375rem",
                  padding: "0.25rem 0.5rem",
                  fontSize: "0.6875rem",
                  color: "var(--ink-2)",
                  cursor: refreshing ? "default" : "pointer",
                  opacity: refreshing ? 0.6 : 1,
                  fontFamily: "inherit",
                  letterSpacing: "0",
                  textTransform: "none",
                }}
              >
                {refreshing ? "…" : "↻ Refresh"}
              </button>
            </div>
          </div>
          {/* Namespace filter chip row. Renders only when there's more than
              one namespace to choose between — otherwise it's just visual
              clutter for single-namespace setups. */}
          {availableNamespaces.length > 1 && (
            <div
              style={{
                padding: "0.5rem 0.75rem",
                borderBottom: "1px solid var(--rule)",
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: "0.25rem",
                background: "var(--paper)",
              }}
            >
              <span
                style={{
                  fontSize: "0.625rem",
                  fontWeight: 600,
                  letterSpacing: "0.04em",
                  textTransform: "uppercase",
                  color: "var(--ink-3)",
                  marginRight: "0.25rem",
                }}
              >
                Namespaces
              </span>
              {availableNamespaces.map((ns) => {
                const isShown = !selectedNamespaces || selectedNamespaces.has(ns);
                return (
                  <button
                    key={ns}
                    onClick={() => toggleNamespace(ns)}
                    title={isShown ? `Hide ${ns}` : `Show ${ns}`}
                    style={{
                      fontSize: "0.6875rem",
                      fontFamily: "var(--mono)",
                      padding: "0.125rem 0.5rem",
                      borderRadius: "999px",
                      border: `1px solid ${isShown ? "var(--brand-bd)" : "var(--rule)"}`,
                      background: isShown ? "var(--brand-bg)" : "transparent",
                      color: isShown ? "var(--brand)" : "var(--ink-3)",
                      cursor: "pointer",
                      letterSpacing: "0",
                      textTransform: "none",
                    }}
                  >
                    {ns}
                  </button>
                );
              })}
              {selectedNamespaces && (
                <button
                  onClick={clearNamespaceFilter}
                  title="Clear filter — show all namespaces"
                  style={{
                    fontSize: "0.6875rem",
                    padding: "0.125rem 0.5rem",
                    borderRadius: "0.375rem",
                    border: "1px solid var(--rule)",
                    background: "transparent",
                    color: "var(--ink-3)",
                    cursor: "pointer",
                    fontFamily: "inherit",
                    letterSpacing: "0",
                    textTransform: "none",
                    marginLeft: "auto",
                  }}
                >
                  Clear
                </button>
              )}
            </div>
          )}
          <div style={{ overflowY: "auto", padding: "0.5rem" }}>
            {loading && (
              <p style={{ color: "var(--ink-3)", fontSize: "0.875rem", padding: "1rem", textAlign: "center" }}>
                Loading…
              </p>
            )}
            {error && (
              <p style={{ color: "var(--red)", fontSize: "0.875rem", padding: "1rem" }}>
                Failed to load: {error}
              </p>
            )}
            {!loading && !error && alerts.length === 0 && (
              <p style={{ color: "var(--ink-3)", fontSize: "0.875rem", padding: "1rem", textAlign: "center" }}>
                No investigations yet. Trigger one via the webhook or <code>/rca</code> in chat.
              </p>
            )}
            {!loading && !error && alerts.length > 0 && filteredAlerts.length === 0 && (
              <p style={{ color: "var(--ink-3)", fontSize: "0.875rem", padding: "1rem", textAlign: "center" }}>
                No alerts match the current namespace filter.{" "}
                <button
                  onClick={clearNamespaceFilter}
                  style={{
                    background: "transparent",
                    border: "none",
                    padding: 0,
                    color: "var(--brand)",
                    textDecoration: "underline",
                    cursor: "pointer",
                    font: "inherit",
                  }}
                >
                  Clear filter
                </button>
              </p>
            )}
            {filteredAlerts.map((a) => {
              const isSel = selected?.id === a.id;
              const sev = severityTokens(a.severity);
              const st = statusTokens(a.status);
              const alertName =
                (a.document as { alert?: { name?: string } })?.alert?.name || a.source || "alert";
              return (
                <button
                  key={a.id}
                  onClick={() => setSelected(a)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    background: isSel ? "var(--brand-bg)" : "transparent",
                    border: `1px solid ${isSel ? "var(--brand-bd)" : "var(--rule)"}`,
                    borderRadius: "0.5rem",
                    padding: "0.75rem",
                    marginBottom: "0.5rem",
                    cursor: "pointer",
                    color: "var(--ink)",
                    fontFamily: "var(--sans)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.375rem",
                      gap: "0.5rem",
                    }}
                  >
                    <span
                      style={{
                        fontWeight: 600,
                        fontSize: "0.875rem",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {alertName}
                    </span>
                    <span
                      style={{
                        fontSize: "0.625rem",
                        fontWeight: 700,
                        letterSpacing: "0.05em",
                        textTransform: "uppercase",
                        padding: "0.125rem 0.375rem",
                        borderRadius: "0.25rem",
                        background: sev.bg,
                        color: sev.fg,
                        border: `1px solid ${sev.bd}`,
                        flexShrink: 0,
                      }}
                    >
                      {a.severity || "info"}
                    </span>
                  </div>
                  <div
                    style={{
                      fontFamily: "var(--mono)",
                      fontSize: "0.6875rem",
                      color: "var(--ink-3)",
                      marginBottom: "0.5rem",
                    }}
                  >
                    {a.namespace || "—"} · {a.source || "—"}
                  </div>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      fontSize: "0.6875rem",
                      color: "var(--ink-3)",
                    }}
                  >
                    <span>{new Date(a.created_at).toLocaleString()}</span>
                    <span
                      style={{
                        padding: "0.0625rem 0.375rem",
                        borderRadius: "999px",
                        background: st.bg,
                        color: st.fg,
                        border: `1px solid ${st.bd}`,
                        fontWeight: 600,
                      }}
                    >
                      {st.label}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* Detail pane */}
        <main
          style={{
            background: "var(--paper-2)",
            border: "1px solid var(--rule)",
            borderRadius: "0.75rem",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
            minHeight: 0,
          }}
        >
          {selected ? (
            <InvestigationViewer
              document={selected.document as unknown as React.ComponentProps<typeof InvestigationViewer>["document"]}
            />
          ) : (
            <div
              style={{
                flex: 1,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--ink-3)",
                fontSize: "0.875rem",
                padding: "2rem",
                textAlign: "center",
              }}
            >
              Select an investigation from the left to view its details.
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
