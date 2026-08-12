"use client";

/**
 * Audit trail — dashboard and replay in one route.
 *
 * Replay is `/audit?session=<id>`, NOT `/audit/session/<id>` as the roadmap
 * sketches it. A path segment there is a dynamic route, and a dynamic route
 * cannot exist in the desktop static export: session ids are not knowable at
 * build time, so generateStaticParams has nothing to enumerate. That exact
 * mistake shipped in 0.2.1 and 0.2.2 for the alerts page — a 404 first, then
 * a hang. The query form serves both builds from one static page.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";

import AuditEventTable from "@/components/AuditEventTable";
import AuditReplayTimeline from "@/components/AuditReplayTimeline";
import {
  auditExportUrl,
  getAuditEvents,
  getAuditEventTypes,
  getAuditReplay,
  verifyAuditChain,
  type AuditEvent,
} from "@/lib/api";

type Verification = Awaited<ReturnType<typeof verifyAuditChain>>;

export default function AuditPage() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [session, setSession] = useState<string | null>(null);
  const [replayEvents, setReplayEvents] = useState<AuditEvent[]>([]);

  const [eventType, setEventType] = useState("");
  const [cluster, setCluster] = useState("");
  const [severity, setSeverity] = useState("");

  // Read the session from the query on mount. window.location rather than
  // useSearchParams keeps this out of a Suspense boundary, which the static
  // export would otherwise require.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    setSession(params.get("session"));
  }, []);

  const openSession = useCallback((sessionId: string) => {
    setSession(sessionId);
    const url = `${window.location.pathname}?session=${encodeURIComponent(sessionId)}`;
    window.history.pushState(null, "", url);
  }, []);

  const closeSession = useCallback(() => {
    setSession(null);
    window.history.pushState(null, "", window.location.pathname);
  }, []);

  // Back and forward must move between the list and a replay, or the browser
  // controls silently do nothing on a route that looks like two pages.
  useEffect(() => {
    const onPop = () => {
      const params = new URLSearchParams(window.location.search);
      setSession(params.get("session"));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const filters = useMemo(
    () => ({ eventType: eventType || undefined, cluster: cluster || undefined, severity: severity || undefined }),
    [eventType, cluster, severity],
  );

  useEffect(() => {
    if (session) return;
    let cancelled = false;
    setLoading(true);
    getAuditEvents(filters)
      .then((body) => {
        if (!cancelled) {
          setEvents(body.events);
          setError(null);
        }
      })
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters, session]);

  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    setLoading(true);
    getAuditReplay(session)
      .then((body) => !cancelled && setReplayEvents(body.events))
      .catch((e) => !cancelled && setError(String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [session]);

  useEffect(() => {
    getAuditEventTypes().then(setEventTypes).catch(() => setEventTypes([]));
    verifyAuditChain().then(setVerification).catch(() => setVerification(null));
  }, []);

  return (
    <main style={{ padding: "1.5rem", maxWidth: "72rem", margin: "0 auto" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "1rem",
        }}
      >
        <h1 style={{ fontSize: "1.125rem", margin: 0 }}>
          {session ? "Session replay" : "Audit trail"}
        </h1>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button
            type="button"
            onClick={() => {
              // `/chat?session=<id>`, never `/chat/<id>` — the path form is a
              // dynamic route and 404s in the desktop static export. The
              // alerts page shipped that bug twice this week.
              let ret: string | null = null;
              try {
                ret = sessionStorage.getItem("k8s_chat_return_session");
              } catch {
                // Private browsing can refuse sessionStorage; falling back to
                // the chat index is better than not navigating at all.
              }
              window.location.href = ret
                ? `/chat?session=${encodeURIComponent(ret)}`
                : "/chat";
            }}
            style={buttonStyle}
          >
            ← Back to chat
          </button>
          {session && (
            <button type="button" onClick={closeSession} style={buttonStyle}>
              ← All events
            </button>
          )}
          <a href={auditExportUrl(filters)} style={{ ...buttonStyle, textDecoration: "none" }}>
            Export JSONL
          </a>
        </div>
      </header>

      {/* The chain result is a claim about whether this page can be trusted,
          so it belongs above the data rather than in a corner. */}
      {verification && (
        <div
          style={{
            marginBottom: "1rem",
            padding: "0.5rem 0.75rem",
            borderRadius: "0.5rem",
            fontSize: "0.8125rem",
            border: "1px solid var(--rule)",
            color: verification.ok ? "var(--ink-2)" : "var(--danger, #b3261e)",
          }}
        >
          {verification.ok
            ? `Chain intact — ${verification.checked} events verified.`
            : `Chain broken at #${verification.broken_at}: ${verification.reason}`}
          {!verification.ok && verification.note && (
            <div style={{ color: "var(--ink-3)", marginTop: "0.25rem" }}>
              {verification.note}
            </div>
          )}
        </div>
      )}

      {!session && (
        <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1rem", flexWrap: "wrap" }}>
          <select value={eventType} onChange={(e) => setEventType(e.target.value)} style={inputStyle}>
            <option value="">All event types</option>
            {eventTypes.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
          <select value={severity} onChange={(e) => setSeverity(e.target.value)} style={inputStyle}>
            <option value="">All severities</option>
            <option value="info">info</option>
            <option value="warn">warn</option>
            <option value="critical">critical</option>
          </select>
          <input
            value={cluster}
            onChange={(e) => setCluster(e.target.value)}
            placeholder="cluster"
            style={inputStyle}
          />
        </div>
      )}

      {error && (
        <p style={{ color: "var(--danger, #b3261e)", fontSize: "0.875rem" }}>{error}</p>
      )}

      {loading ? (
        <p style={{ color: "var(--ink-3)", fontSize: "0.875rem" }}>Loading…</p>
      ) : session ? (
        <AuditReplayTimeline events={replayEvents} />
      ) : (
        <AuditEventTable events={events} onSelectSession={openSession} />
      )}
    </main>
  );
}

const buttonStyle: React.CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--ink-3)",
  background: "transparent",
  border: "1px solid var(--rule)",
  padding: "0.375rem 0.625rem",
  borderRadius: "0.5rem",
  cursor: "pointer",
  fontFamily: "inherit",
};

const inputStyle: React.CSSProperties = {
  fontSize: "0.8125rem",
  padding: "0.375rem 0.5rem",
  borderRadius: "0.5rem",
  border: "1px solid var(--rule)",
  background: "transparent",
  color: "inherit",
  fontFamily: "inherit",
};
